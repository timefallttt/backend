import re
from pathlib import Path
from typing import Dict, List, Protocol

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
from app.services.workitems.connectors.demo import DemoWorkItemConnector
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
        self._connectors: Dict[str, WorkItemConnector] = {
            DemoWorkItemConnector.key: DemoWorkItemConnector(),
        }

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
                    snippet_lines = self._build_snippet_lines(hunk)
                    if not snippet_lines:
                        continue
                    seeds.append(
                        WorkItemCodeSeed(
                            seed_id=f'{commit.commit_id}-{file_diff.diff_id}-{index}',
                            filename=file_diff.filename,
                            code='\n'.join(snippet_lines),
                            start_line=hunk.start_line,
                            end_line=max(hunk.end_line, hunk.start_line + len(snippet_lines) - 1),
                            recall_reason=f'由 commit {short_hash} 的 diff 片段派生',
                            source='workitem_diff',
                        )
                    )
        return seeds

    def _build_snippet_lines(self, hunk: WorkItemDiffHunk) -> List[str]:
        added = [line for line in hunk.added_lines if line.strip()]
        if added:
            return added[:12]
        return [line for line in hunk.context_lines if line.strip()][:12]

    def _expand_graph_evidence(
        self,
        job: IndexJobDetail,
        item: WorkItemDetail,
    ) -> GraphEvidenceBundle | None:
        snippets = self._to_candidate_snippets(item)
        seeds = [
            GraphSeedQuery(
                path=snippet.filename,
                name=self._guess_seed_name(snippet),
                signature=(snippet.code.splitlines()[0][:120] if snippet.code else ''),
                max_matches=3,
            )
            for snippet in snippets
        ]
        if not any(seed.path or seed.name or seed.signature for seed in seeds):
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
        first_line = snippet.code.splitlines()[0] if snippet.code else ''
        match = re.search(r'(?:async\s+)?(?:function|class)?\s*([A-Za-z_][A-Za-z0-9_]*)', first_line)
        if match and match.group(1) not in {'async', 'function', 'class'}:
            return match.group(1)
        filename = snippet.filename.replace('\\', '/').split('/')[-1]
        return filename.rsplit('.', 1)[0]

    def _read_graph_node_excerpt(
        self,
        job: IndexJobDetail,
        relative_path: str,
        start_line: int | None,
        end_line: int | None,
    ) -> str:
        if not relative_path or not job.snapshot.local_path:
            return ''
        file_path = Path(job.snapshot.local_path) / relative_path
        if not file_path.exists() or not file_path.is_file():
            return ''
        try:
            lines = file_path.read_text(encoding='utf-8', errors='replace').splitlines()
        except OSError:
            return ''
        if start_line and end_line and start_line > 0 and end_line >= start_line:
            excerpt_lines = lines[start_line - 1:min(end_line, len(lines))]
        else:
            excerpt_lines = lines[:20]
        return '\n'.join(excerpt_lines[:40]).strip()
