import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Dict, List, Protocol

from app.config import EXTERNAL_WORKITEM_CONNECTOR_PATH, EXTERNAL_WORKITEM_DATA_PATH
from app.services.consistency.schemas import (
    CandidateSnippet,
    GraphEvidenceBundle,
    GraphEvidencePathInput,
    GraphEvidenceStepInput,
    GraphEvidenceSummaryInput,
    ReviewTaskDetail,
    ReviewTaskCreateRequest,
)
from app.services.consistency.service import ConsistencyService
from app.services.indexing.query_service import GraphQueryService
from app.services.indexing.schemas import GraphEvidenceQueryRequest, GraphSeedQuery, IndexJobDetail
from app.services.indexing.service import OfflineIndexingService
from app.services.workitems.schemas import (
    WorkItemCodeSeed,
    WorkItemConnectorListResponse,
    WorkItemConnectorSummary,
    WorkItemDetail,
    WorkItemDiffHunk,
    WorkItemImportRequest,
    WorkItemListResponse,
    WorkItemSummary,
)


class WorkItemConnector(Protocol):
    @property
    def summary(self) -> WorkItemConnectorSummary: ...

    def list_items(self) -> List[WorkItemSummary]: ...

    def get_item(self, item_id: str) -> WorkItemDetail: ...


class ExternalModuleConnector:
    def __init__(self, module: ModuleType) -> None:
        self._module = module
        self._summary = WorkItemConnectorSummary(**module.connector_summary())

    @property
    def summary(self) -> WorkItemConnectorSummary:
        return self._summary

    def list_items(self) -> List[WorkItemSummary]:
        return [WorkItemSummary(**item) for item in self._module.list_items()]

    def get_item(self, item_id: str) -> WorkItemDetail:
        return WorkItemDetail(**self._module.get_item(item_id))


class WorkItemService:
    def __init__(
        self,
        indexing_service: OfflineIndexingService,
        graph_query_service: GraphQueryService,
        consistency_service: ConsistencyService,
    ) -> None:
        self._indexing_service = indexing_service
        self._graph_query_service = graph_query_service
        self._consistency_service = consistency_service
        self._connectors: Dict[str, WorkItemConnector] = {}
        self._load_external_connector()

    def list_connectors(self) -> WorkItemConnectorListResponse:
        return WorkItemConnectorListResponse(
            connectors=[connector.summary for connector in self._connectors.values()]
        )

    def list_items(self, connector_key: str) -> WorkItemListResponse:
        connector = self._get_connector(connector_key)
        return WorkItemListResponse(
            connector=connector.summary,
            items=connector.list_items(),
        )

    def get_item(self, connector_key: str, item_id: str) -> WorkItemDetail:
        connector = self._get_connector(connector_key)
        item = connector.get_item(item_id)
        derived_seeds = self._derive_seed_previews(item)
        return item.model_copy(
            update={
                'derived_seeds': derived_seeds,
                'derived_seed_count': len(derived_seeds),
            }
        )

    def import_item(self, request: WorkItemImportRequest) -> ReviewTaskDetail:
        item = self.get_item(request.connector_key, request.item_id)
        repo_name = request.repo_name or item.repo_name
        snapshot = request.snapshot or item.snapshot_hint or 'manual-import'
        graph_evidence = None

        if request.index_job_id:
            job = self._indexing_service.get_job(request.index_job_id)
            repo_name = repo_name or job.snapshot.repo_name
            snapshot = request.snapshot or job.snapshot.commit_hash or job.snapshot.branch
            if request.auto_expand_graph_evidence:
                graph_evidence = self._expand_graph_evidence(job, item)

        if not repo_name:
            raise ValueError('repo_name is required when no indexing job is provided')

        task = ReviewTaskCreateRequest(
            requirement_id=item.requirement_id,
            title=item.title,
            requirement_text=item.requirement_text,
            acceptance_criteria=item.acceptance_criteria,
            repo_name=repo_name,
            snapshot=snapshot,
            business_tag=item.business_tag,
            priority=item.priority,
            owner=item.owner,
            notes=item.notes or f'Imported from connector {request.connector_key}:{request.item_id}',
            candidate_snippets=self._to_candidate_snippets(item),
            graph_evidence=graph_evidence,
        )
        return self._consistency_service.create_task(task)

    def _load_external_connector(self) -> None:
        connector_path = EXTERNAL_WORKITEM_CONNECTOR_PATH
        if not connector_path.exists():
            return

        spec = importlib.util.spec_from_file_location('external_workitem_connector', connector_path)
        if not spec or not spec.loader:
            raise RuntimeError(f'无法加载外部工单接入器：{connector_path}')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        required_functions = ('connector_summary', 'list_items', 'get_item')
        missing = [name for name in required_functions if not hasattr(module, name)]
        if missing:
            raise RuntimeError(f'外部工单接入器缺少必要函数：{", ".join(missing)}')

        if hasattr(module, 'configure'):
            module.configure(data_path=str(EXTERNAL_WORKITEM_DATA_PATH))

        connector = ExternalModuleConnector(module)
        self._connectors[connector.summary.connector_key] = connector

    def _get_connector(self, connector_key: str) -> WorkItemConnector:
        connector = self._connectors.get(connector_key)
        if not connector:
            raise ValueError(f'connector not found: {connector_key}')
        return connector

    def _to_candidate_snippets(self, item: WorkItemDetail) -> List[CandidateSnippet]:
        return [
            CandidateSnippet(
                snippet_id=seed.seed_id,
                filename=seed.filename,
                code=seed.code,
                start_line=seed.start_line,
                end_line=seed.end_line,
                recall_reason=seed.recall_reason or 'Imported from work item diff',
                source=seed.source,
                selected=True,
            )
            for seed in item.derived_seeds
        ]

    def _derive_seed_previews(self, item: WorkItemDetail) -> List[WorkItemCodeSeed]:
        seeds: List[WorkItemCodeSeed] = []
        for commit in item.linked_commits:
            short_hash = commit.commit_hash[:8] if commit.commit_hash else commit.commit_id
            for file_diff in commit.file_diffs:
                relevant_hunks = [hunk for hunk in file_diff.hunks if hunk.added_lines or hunk.context_lines]
                if not relevant_hunks:
                    continue
                for index, hunk in enumerate(relevant_hunks, start=1):
                    for segment_index, segment in enumerate(self._build_hunk_seed_segments(hunk), start=1):
                        seeds.append(
                            WorkItemCodeSeed(
                                seed_id=f'{commit.commit_id}-{file_diff.diff_id}-{index}-{segment_index}',
                                filename=file_diff.filename,
                                code=segment['code'],
                                start_line=segment['start_line'],
                                end_line=segment['end_line'],
                                recall_reason=f'由 commit {short_hash} 的 diff {segment["kind"]}派生',
                                source='workitem_diff',
                            )
                        )
        return seeds

    def _build_snippet_lines(self, hunk: WorkItemDiffHunk) -> List[str]:
        added = [line for line in hunk.added_lines if line.strip()]
        if added:
            return added[:12]
        return [line for line in hunk.context_lines if line.strip()][:12]

    def _build_hunk_seed_segments(self, hunk: WorkItemDiffHunk) -> List[dict]:
        indexed_lines = self._build_indexed_hunk_lines(hunk)
        if not indexed_lines:
            return []

        segments: List[dict] = []
        consumed_indexes: set[int] = set()
        cursor = 0

        while cursor < len(indexed_lines):
            line_no, line = indexed_lines[cursor]
            if not self._looks_like_named_function_signature(line):
                cursor += 1
                continue

            block_end = self._find_function_block_end(indexed_lines, cursor)
            excerpt = indexed_lines[cursor:block_end + 1]
            code_lines = [item[1] for item in excerpt]
            if code_lines:
                segments.append(
                    {
                        'kind': '函数体片段',
                        'code': '\n'.join(code_lines).strip(),
                        'start_line': line_no,
                        'end_line': excerpt[-1][0],
                    }
                )
                consumed_indexes.update(range(cursor, block_end + 1))
            cursor = block_end + 1

        fallback_chunks = self._build_behavior_chunks(indexed_lines, consumed_indexes)
        segments.extend(fallback_chunks)
        return [segment for segment in segments if segment.get('code')]

    def _build_indexed_hunk_lines(self, hunk: WorkItemDiffHunk) -> List[tuple[int, str]]:
        source_lines = hunk.added_lines if any(line.strip() for line in hunk.added_lines) else hunk.context_lines
        indexed: List[tuple[int, str]] = []
        for offset, line in enumerate(source_lines):
            if not line.strip():
                continue
            indexed.append((hunk.start_line + offset, line))
        return indexed

    def _looks_like_named_function_signature(self, line: str) -> bool:
        return bool(self._extract_named_function_signature_name(line))

    def _extract_named_function_signature_name(self, line: str) -> str:
        stripped = line.strip()
        patterns = [
            r'^(?:export\s+)?(?:public\s+|private\s+|protected\s+)?(?:static\s+)?(?:async\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\([^;]*\)\s*(?::\s*[^=]+)?\s*\{?$',
            r'^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\([^;]*\)\s*(?::\s*[^=]+)?\s*\{?$',
            r'^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(?:async\s+)?\([^;]*\)\s*=>?\s*\{?$',
        ]
        for pattern in patterns:
            match = re.search(pattern, stripped)
            if match:
                return match.group(1)
        return ''

    def _find_function_block_end(self, indexed_lines: List[tuple[int, str]], start_index: int) -> int:
        brace_depth = 0
        saw_open_brace = False
        for index in range(start_index, len(indexed_lines)):
            line = indexed_lines[index][1]
            open_count = line.count('{')
            close_count = line.count('}')
            if open_count:
                saw_open_brace = True
            brace_depth += open_count
            brace_depth -= close_count
            if saw_open_brace and brace_depth <= 0:
                return index
        return min(len(indexed_lines) - 1, start_index + 11)

    def _build_behavior_chunks(
        self,
        indexed_lines: List[tuple[int, str]],
        consumed_indexes: set[int],
    ) -> List[dict]:
        chunks: List[List[tuple[int, str]]] = []
        current: List[tuple[int, str]] = []

        for index, item in enumerate(indexed_lines):
            if index in consumed_indexes:
                if current:
                    chunks.append(current)
                    current = []
                continue
            current.append(item)

        if current:
            chunks.append(current)

        segments: List[dict] = []
        for chunk in chunks:
            code_lines = [line for _, line in chunk if line.strip()]
            if not code_lines:
                continue
            selected = code_lines[:12]
            segments.append(
                {
                    'kind': '行为片段',
                    'code': '\n'.join(selected).strip(),
                    'start_line': chunk[0][0],
                    'end_line': chunk[min(len(chunk), len(selected)) - 1][0],
                }
            )
        return segments

    def _expand_graph_evidence(
        self,
        job: IndexJobDetail,
        item: WorkItemDetail,
    ) -> GraphEvidenceBundle | None:
        snippets = self._to_candidate_snippets(item)
        seeds = self._build_graph_seed_queries(snippets)
        if not seeds:
            return None

        response = self._graph_query_service.query_job_evidence(
            job,
            GraphEvidenceQueryRequest(
                seeds=seeds,
                max_hops=2,
                max_paths=20,
                edge_types=['CALLS', 'CONTAINS'],
            ),
        )
        return GraphEvidenceBundle(
            source=response.source,
            hints=response.hints,
            summary=GraphEvidenceSummaryInput(**response.summary.model_dump()),
            paths=[
                GraphEvidencePathInput(
                    path_id=path.path_id,
                    hop_count=path.hop_count,
                    nodes=[
                        GraphEvidenceStepInput(
                            node_id=node.node_id,
                            node_type=node.node_type,
                            name=node.name,
                            path=node.path,
                            start_line=node.start_line,
                            end_line=node.end_line,
                            signature=node.signature,
                            code_excerpt=self._read_graph_node_excerpt(
                                job,
                                node.path,
                                node.start_line,
                                node.end_line,
                                node.node_type,
                                node.name,
                                self._find_fallback_snippet(node, snippets),
                            ),
                            relation_from_prev=node.relation_from_prev,
                        )
                        for node in path.nodes
                    ],
                )
                for path in response.paths
            ],
        )

    def _guess_seed_name(self, snippet: CandidateSnippet) -> str:
        first_line = snippet.code.splitlines()[0].strip() if snippet.code else ''
        named_function = self._extract_named_function_signature_name(first_line)
        if named_function:
            return named_function
        patterns = [
            r'^(?:export\s+)?class\s+([A-Za-z_][A-Za-z0-9_]*)\b',
        ]
        for pattern in patterns:
            match = re.search(pattern, first_line)
            if match:
                return match.group(1)
        return ''

    def _build_seed_query(self, snippet: CandidateSnippet) -> GraphSeedQuery:
        seed_name = self._guess_seed_name(snippet)
        signature = snippet.code.splitlines()[0][:120] if snippet.code else ''
        return GraphSeedQuery(
            path=snippet.filename if seed_name else '',
            name=seed_name,
            signature=signature if seed_name else '',
            max_matches=3,
        )

    def _build_graph_seed_queries(self, snippets: List[CandidateSnippet]) -> List[GraphSeedQuery]:
        seeds: List[GraphSeedQuery] = []
        seen: set[tuple[str, str, str]] = set()
        for snippet in snippets:
            primary = self._build_seed_query(snippet)
            self._append_seed_query(seeds, seen, primary)
            for call_seed in self._extract_call_seed_queries(snippet):
                self._append_seed_query(seeds, seen, call_seed)
        return seeds

    def _extract_call_seed_queries(self, snippet: CandidateSnippet) -> List[GraphSeedQuery]:
        call_names: List[str] = []
        seen: set[str] = set()
        for line in snippet.code.splitlines():
            if self._looks_like_named_function_signature(line):
                continue
            for call_name in self._extract_call_names(line):
                lowered = call_name.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                call_names.append(call_name)
        return [
            GraphSeedQuery(name=call_name, path='', signature='', max_matches=3)
            for call_name in call_names
        ]

    def _extract_call_names(self, line: str) -> List[str]:
        patterns = re.findall(r'([A-Za-z_][A-Za-z0-9_]*)\s*\(', line)
        skip = {
            'if', 'for', 'while', 'switch', 'catch', 'function', 'class'
        }
        result: List[str] = []
        for name in patterns:
            if name in skip:
                continue
            result.append(name)
        return result

    def _append_seed_query(
        self,
        seeds: List[GraphSeedQuery],
        seen: set[tuple[str, str, str]],
        seed: GraphSeedQuery,
    ) -> None:
        key = (seed.name, seed.path, seed.signature)
        if key in seen or not any(key):
            return
        seen.add(key)
        seeds.append(seed)

    def _find_fallback_snippet(
        self,
        node: GraphEvidenceStepInput,
        snippets: List[CandidateSnippet],
    ) -> str:
        normalized_path = node.path.replace('\\', '/').lower()
        same_file = [
            snippet for snippet in snippets
            if snippet.filename.replace('\\', '/').lower() == normalized_path
        ]
        if not same_file:
            return ''
        if node.node_type == 'Function':
            node_name = node.name.lower()
            node_tail = node_name.split('.')[-1]
            for snippet in same_file:
                seed_name = self._guess_seed_name(snippet).lower()
                if not seed_name:
                    continue
                if node_name == seed_name or node_name.endswith(f'.{seed_name}') or node_tail == seed_name:
                    return snippet.code.strip()
        return ''


    def _read_graph_node_excerpt(
        self,
        job: IndexJobDetail,
        relative_path: str,
        start_line: int | None,
        end_line: int | None,
        node_type: str,
        node_name: str,
        fallback_snippet: str,
    ) -> str:
        if not relative_path or not job.snapshot.local_path:
            return fallback_snippet
        file_path = Path(job.snapshot.local_path) / relative_path
        if not file_path.exists() or not file_path.is_file():
            return fallback_snippet
        try:
            lines = file_path.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            return fallback_snippet
        if start_line and end_line and start_line > 0 and end_line >= start_line:
            excerpt_lines = lines[start_line - 1:min(end_line, len(lines))]
            return '\n'.join(excerpt_lines[:40]).strip()
        inferred_excerpt = self._infer_symbol_excerpt(lines, node_type, node_name)
        if inferred_excerpt:
            return inferred_excerpt
        if fallback_snippet:
            return fallback_snippet
        if node_type == 'File':
            return ''
        return ''

    def _infer_symbol_excerpt(
        self,
        lines: List[str],
        node_type: str,
        node_name: str,
    ) -> str:
        symbol_name = node_name.split('.')[-1] if node_name else ''
        if not symbol_name:
            return ''
        if node_type == 'Function':
            return self._extract_function_excerpt(lines, symbol_name)
        if node_type == 'Class':
            return self._extract_class_excerpt(lines, symbol_name)
        return ''

    def _extract_function_excerpt(self, lines: List[str], symbol_name: str) -> str:
        if symbol_name.startswith('%'):
            return ''
        escaped_name = re.escape(symbol_name)
        patterns = [
            re.compile(rf'^\s*(?:export\s+)?(?:(?:public|private|protected)\s+)?(?:static\s+)?(?:async\s+)?{escaped_name}\s*\('),
            re.compile(rf'^\s*(?:export\s+)?(?:async\s+)?function\s+{escaped_name}\s*\('),
            re.compile(rf'^\s*{escaped_name}\s*:\s*(?:async\s+)?\('),
        ]
        if symbol_name == 'constructor':
            patterns.insert(0, re.compile(r'^\s*constructor\s*\('))

        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            if any(pattern.search(line) for pattern in patterns):
                return self._collect_block_excerpt(lines, index)
        return ''

    def _extract_class_excerpt(self, lines: List[str], symbol_name: str) -> str:
        if symbol_name.startswith('%'):
            return ''
        pattern = re.compile(rf'^\s*(?:export\s+)?class\s+{re.escape(symbol_name)}\b')
        for index, line in enumerate(lines):
            if pattern.search(line):
                return self._collect_block_excerpt(lines, index)
        return ''

    def _collect_block_excerpt(self, lines: List[str], start_index: int) -> str:
        excerpt: List[str] = []
        brace_depth = 0
        saw_open_brace = False
        for line in lines[start_index:]:
            excerpt.append(line)
            brace_depth += line.count('{')
            if line.count('{') > 0:
                saw_open_brace = True
            brace_depth -= line.count('}')
            if saw_open_brace and brace_depth <= 0:
                break
            if len(excerpt) >= 40:
                break
        return '\n'.join(excerpt).strip()
