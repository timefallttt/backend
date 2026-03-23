import hashlib
import json
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List
from uuid import uuid4

from app.config import ARTIFACT_STORAGE_DIR, INDEXING_JOBS_FILE, REPO_STORAGE_DIR

from .arkanalyzer_runner import ArkAnalyzerRunner, ParserRunResult
from .graph_store import Neo4jGraphStore, write_graph_artifact_file
from .repo_manager import GitRepoManager
from .schemas import (
    GraphArtifact,
    GraphBuildStats,
    GraphEdge,
    GraphNode,
    IndexJobDetail,
    IndexJobListResponse,
    IndexJobSummary,
    RepoSnapshot,
    RepositoryIndexRequest,
)


CALL_EXPR_TYPES = {'InstanceCallExpr', 'StaticCallExpr', 'PtrCallExpr'}


class OfflineIndexingService:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._repo_manager = GitRepoManager()
        self._parser_runner = ArkAnalyzerRunner()
        self._graph_store = Neo4jGraphStore()
        self._jobs: Dict[str, dict] = {}
        self._ensure_storage()
        self._load_jobs()
        self._cleanup_orphaned_storage()

    def list_jobs(self) -> IndexJobListResponse:
        jobs = [self._build_summary(job) for job in self._jobs.values()]
        jobs.sort(key=lambda item: item.created_at, reverse=True)
        return IndexJobListResponse(jobs=jobs)

    def get_job(self, job_id: str) -> IndexJobDetail:
        job = self._get_job_or_raise(job_id)
        return self._build_detail(job)

    def create_job(self, request: RepositoryIndexRequest) -> IndexJobDetail:
        repo_name = request.repo_name or self._repo_manager.repo_slug(str(request.repo_url))
        now = self._now()
        job_id = f'index-{uuid4().hex[:8]}'
        artifact_dir = ARTIFACT_STORAGE_DIR / job_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        repo_dir = REPO_STORAGE_DIR / f'{repo_name}__{job_id}'

        job = {
            'job_id': job_id,
            'status': 'queued',
            'repo_name': repo_name,
            'branch': request.branch,
            'parser_mode': 'arkanalyzer',
            'graph_store_status': 'not_attempted',
            'created_at': now,
            'updated_at': now,
            'current_step': '等待执行',
            'logs': [f'[{now}] 已创建离线建库任务。'],
            'snapshot': {
                'repo_url': str(request.repo_url),
                'branch': request.branch,
                'repo_name': repo_name,
                'local_path': str(repo_dir),
                'commit_hash': '',
            },
            'artifact_dir': str(artifact_dir),
            'parser_output_path': str(artifact_dir / 'parser-manifest.json'),
            'graph_artifact_path': str(artifact_dir / 'graph-artifact.json'),
            'graph_stats': GraphBuildStats().model_dump(),
            'setup_hints': [],
        }
        with self._lock:
            self._jobs[job_id] = job
            self._save_jobs()
        return self._build_detail(job)

    def run_job(self, job_id: str) -> IndexJobDetail:
        with self._lock:
            job = self._get_job_or_raise(job_id)
            job['status'] = 'running'
            job['current_step'] = '同步代码仓库'
            job['updated_at'] = self._now()
            self._append_log(job, '开始执行离线建库任务。')
            self._save_jobs()

        try:
            snapshot = RepoSnapshot(**job['snapshot'])
            commit_hash, effective_branch = self._repo_manager.sync_repo(
                repo_url=snapshot.repo_url,
                branch=snapshot.branch,
                target_dir=Path(snapshot.local_path),
            )
            snapshot.commit_hash = commit_hash
            snapshot.branch = effective_branch
            job['snapshot'] = snapshot.model_dump()
            self._append_log(job, f'仓库同步完成，分支 {effective_branch}，当前提交 {commit_hash[:10]}。')

            artifact_dir = Path(job['artifact_dir'])
            parser_manifest_path = Path(job['parser_output_path'])

            job['current_step'] = '执行代码解析'
            parser_result = self._run_parser(snapshot, artifact_dir, parser_manifest_path)
            job['parser_mode'] = parser_result.parser_mode
            if parser_result.logs:
                job['logs'].extend(f'[{self._now()}] {line}' for line in parser_result.logs)
            job['setup_hints'] = parser_result.setup_hints
            self._append_log(job, 'ArkAnalyzer 解析完成。')

            job['current_step'] = '构建图工件'
            artifact = self._build_graph_artifact(snapshot, parser_manifest_path)
            write_graph_artifact_file(Path(job['graph_artifact_path']), artifact)
            job['graph_stats'] = artifact.stats.model_dump()
            self._append_log(job, f'图工件已生成，节点 {len(artifact.nodes)} 个，边 {len(artifact.edges)} 条。')

            job['current_step'] = '写入图数据库'
            graph_store_status, graph_hints = self._graph_store.write_graph(
                snapshot_id=f'{snapshot.repo_name}@{snapshot.commit_hash or snapshot.branch}',
                artifact=artifact,
            )
            job['graph_store_status'] = graph_store_status
            job['setup_hints'] = self._merge_hints(job['setup_hints'], graph_hints)
            if graph_store_status == 'loaded':
                job['status'] = 'completed'
                self._append_log(job, 'Neo4j 写入完成。')
            else:
                job['status'] = 'completed_with_warnings'
                self._append_log(job, '图工件已生成，但 Neo4j 未成功写入。')

            job['current_step'] = '完成'
            job['updated_at'] = self._now()
        except Exception as exc:
            job['status'] = 'failed'
            job['current_step'] = '失败'
            job['updated_at'] = self._now()
            if not job['setup_hints']:
                job['setup_hints'] = self._parser_runner.build_setup_hints(str(exc))
            self._append_log(job, f'任务失败：{exc}')
        finally:
            with self._lock:
                self._save_jobs()

        return self._build_detail(job)

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            job = self._get_job_or_raise(job_id)
            if job['status'] in {'queued', 'running'}:
                raise ValueError('cannot delete a queued or running indexing job')
            snapshot = RepoSnapshot(**job['snapshot'])
            snapshot_id = f"{snapshot.repo_name}@{snapshot.commit_hash or snapshot.branch}"
            artifact_dir = Path(job['artifact_dir'])
            repo_dir = Path(snapshot.local_path) if snapshot.local_path else None

            deleted, hints = self._graph_store.delete_snapshot(snapshot_id)
            if hints:
                job['setup_hints'] = self._merge_hints(job.get('setup_hints', []), hints)
            del self._jobs[job_id]
            self._save_jobs()

        if artifact_dir.exists():
            shutil.rmtree(artifact_dir, ignore_errors=True)
        if repo_dir and repo_dir.exists() and not self._is_repo_path_referenced(str(repo_dir)):
            shutil.rmtree(repo_dir, ignore_errors=True)
        self._cleanup_orphaned_storage()
        if hints and not deleted:
            raise RuntimeError('\n'.join(hints))

    def _run_parser(
        self,
        snapshot: RepoSnapshot,
        artifact_dir: Path,
        parser_manifest_path: Path,
    ) -> ParserRunResult:
        parser_result = self._parser_runner.run(Path(snapshot.local_path), artifact_dir)
        parser_manifest_path.write_text(
            json.dumps(parser_result.manifest, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        return parser_result

    def _build_graph_artifact(self, snapshot: RepoSnapshot, parser_manifest_path: Path) -> GraphArtifact:
        if not parser_manifest_path.exists():
            raise RuntimeError('ArkAnalyzer 解析清单不存在，无法构建图工件。')

        manifest = json.loads(parser_manifest_path.read_text(encoding='utf-8'))
        output_dir = Path(manifest.get('output_dir', ''))
        if not output_dir.exists():
            raise RuntimeError('ArkAnalyzer 输出目录不存在，无法构建图工件。')
        return self._build_arkanalyzer_graph(snapshot, output_dir)

    def _build_arkanalyzer_graph(self, snapshot: RepoSnapshot, output_dir: Path) -> GraphArtifact:
        nodes, edges, stats = self._init_graph(snapshot)
        method_index: Dict[str, str] = {}
        pending_calls: List[tuple[str, str]] = []
        repo_path = Path(snapshot.local_path)

        for json_path in sorted(output_dir.rglob('*.json')):
            payload = json.loads(json_path.read_text(encoding='utf-8'))
            file_path = self._extract_file_path(payload, json_path, repo_path)
            file_id = f'file:{file_path}'
            if file_id not in nodes:
                self._upsert_node(
                    nodes,
                    GraphNode(
                        node_id=file_id,
                        node_type='File',
                        name=Path(file_path).name,
                        path=file_path,
                    ),
                )
                self._add_edge(edges, 'CONTAINS', f'repo:{snapshot.repo_name}', file_id)
                stats.file_count += 1

            for class_payload, namespace_chain in self._iter_class_payloads(payload):
                class_signature = class_payload.get('signature', {})
                class_name = class_signature.get('name', 'AnonymousClass')
                display_name = '.'.join([*namespace_chain, class_name]) if namespace_chain else class_name
                class_id = f'class:{file_path}:{display_name}'
                if class_id not in nodes:
                    self._upsert_node(
                        nodes,
                        GraphNode(
                            node_id=class_id,
                            node_type='Class',
                            name=display_name,
                            path=file_path,
                            signature=json.dumps(class_signature, ensure_ascii=False, sort_keys=True),
                        ),
                    )
                    self._add_edge(edges, 'CONTAINS', file_id, class_id)
                    stats.class_count += 1

                for method_payload in class_payload.get('methods', []):
                    method_signature = method_payload.get('signature', {})
                    signature_key = self._normalize_signature(method_signature)
                    method_id = f"func:{hashlib.sha1(signature_key.encode('utf-8')).hexdigest()[:16]}"
                    method_name = method_signature.get('name', 'anonymous')
                    signature_text = json.dumps(method_signature, ensure_ascii=False, sort_keys=True)

                    if method_id not in nodes:
                        self._upsert_node(
                            nodes,
                            GraphNode(
                                node_id=method_id,
                                node_type='Function',
                                name=f'{display_name}.{method_name}',
                                path=file_path,
                                signature=signature_text,
                            ),
                        )
                        self._add_edge(edges, 'CONTAINS', class_id, method_id)
                        stats.function_count += 1

                    method_index[signature_key] = method_id
                    pending_calls.extend(
                        (method_id, self._normalize_signature(call_sig))
                        for call_sig in self._extract_call_signatures(method_payload)
                    )

        for source_id, target_signature in pending_calls:
            target_id = method_index.get(target_signature)
            if target_id:
                self._add_edge(edges, 'CALLS', source_id, target_id, 'arkanalyzer-call')

        stats.edge_count = len(edges)
        return GraphArtifact(nodes=list(nodes.values()), edges=list(edges.values()), stats=stats)

    def _iter_class_payloads(self, payload: dict, namespace_chain: List[str] | None = None) -> Iterable[tuple[dict, List[str]]]:
        chain = namespace_chain or []
        for class_payload in payload.get('classes', []):
            yield class_payload, chain
        for namespace_payload in payload.get('namespaces', []):
            namespace_name = (namespace_payload.get('signature', {}) or {}).get('name', 'namespace')
            next_chain = [*chain, namespace_name]
            yield from self._iter_class_payloads(namespace_payload, next_chain)

    def _extract_call_signatures(self, method_payload: dict) -> List[dict]:
        signatures: List[dict] = []
        self._walk_for_call_signatures(method_payload.get('body'), signatures)
        return signatures

    def _walk_for_call_signatures(self, value: object, signatures: List[dict]) -> None:
        if isinstance(value, dict):
            if value.get('_') in CALL_EXPR_TYPES and isinstance(value.get('method'), dict):
                signatures.append(value['method'])
            for nested in value.values():
                self._walk_for_call_signatures(nested, signatures)
        elif isinstance(value, list):
            for item in value:
                self._walk_for_call_signatures(item, signatures)

    def _extract_file_path(self, payload: dict, json_path: Path, repo_path: Path) -> str:
        file_name = (payload.get('signature', {}) or {}).get('fileName')
        if isinstance(file_name, str) and file_name:
            return file_name.replace('\\', '/')
        try:
            return json_path.relative_to(repo_path).with_suffix('').as_posix()
        except ValueError:
            return json_path.stem

    def _normalize_signature(self, signature: dict) -> str:
        return json.dumps(signature, ensure_ascii=False, sort_keys=True)

    def _init_graph(self, snapshot: RepoSnapshot) -> tuple[dict[str, GraphNode], dict[str, GraphEdge], GraphBuildStats]:
        repo_node = GraphNode(
            node_id=f'repo:{snapshot.repo_name}',
            node_type='Repository',
            name=snapshot.repo_name,
            path=snapshot.local_path,
        )
        return {repo_node.node_id: repo_node}, {}, GraphBuildStats()

    def _upsert_node(self, nodes: dict[str, GraphNode], node: GraphNode) -> None:
        nodes[node.node_id] = node

    def _add_edge(
        self,
        edges: dict[str, GraphEdge],
        edge_type: str,
        source_id: str,
        target_id: str,
        detail: str = '',
    ) -> None:
        key = f'{edge_type}:{source_id}:{target_id}:{detail}'
        edges[key] = GraphEdge(
            edge_type=edge_type,
            source_id=source_id,
            target_id=target_id,
            detail=detail,
        )

    def _merge_hints(self, left: List[str], right: List[str]) -> List[str]:
        merged: List[str] = []
        for hint in [*left, *right]:
            if hint and hint not in merged:
                merged.append(hint)
        return merged

    def _ensure_storage(self) -> None:
        REPO_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        ARTIFACT_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
        INDEXING_JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not INDEXING_JOBS_FILE.exists():
            INDEXING_JOBS_FILE.write_text('{}', encoding='utf-8')

    def _load_jobs(self) -> None:
        try:
            self._jobs = json.loads(INDEXING_JOBS_FILE.read_text(encoding='utf-8'))
        except json.JSONDecodeError:
            self._jobs = {}

    def _save_jobs(self) -> None:
        INDEXING_JOBS_FILE.write_text(
            json.dumps(self._jobs, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )

    def _append_log(self, job: dict, message: str) -> None:
        timestamp = self._now()
        job['logs'].append(f'[{timestamp}] {message}')

    def _build_summary(self, job: dict) -> IndexJobSummary:
        return IndexJobSummary(
            job_id=job['job_id'],
            status=job['status'],
            repo_name=job['repo_name'],
            branch=job['branch'],
            parser_mode=job['parser_mode'],
            graph_store_status=job['graph_store_status'],
            created_at=job['created_at'],
            updated_at=job['updated_at'],
        )

    def _build_detail(self, job: dict) -> IndexJobDetail:
        return IndexJobDetail(
            summary=self._build_summary(job),
            snapshot=RepoSnapshot(**job['snapshot']),
            current_step=job['current_step'],
            logs=job['logs'],
            artifact_dir=job['artifact_dir'],
            parser_output_path=job['parser_output_path'],
            graph_artifact_path=job['graph_artifact_path'],
            graph_stats=GraphBuildStats(**job['graph_stats']),
            setup_hints=job['setup_hints'],
        )

    def _get_job_or_raise(self, job_id: str) -> dict:
        job = self._jobs.get(job_id)
        if not job:
            raise ValueError(f'index job {job_id} not found')
        return job

    def _is_repo_path_referenced(self, repo_path: str) -> bool:
        return any(
            item.get('snapshot', {}).get('local_path') == repo_path
            for item in self._jobs.values()
        )

    def _cleanup_orphaned_storage(self) -> None:
        referenced_artifacts = {
            Path(item['artifact_dir']).resolve()
            for item in self._jobs.values()
            if item.get('artifact_dir')
        }
        for artifact_dir in ARTIFACT_STORAGE_DIR.iterdir():
            if artifact_dir.resolve() not in referenced_artifacts:
                shutil.rmtree(artifact_dir, ignore_errors=True)

        referenced_repos = {
            Path(item.get('snapshot', {}).get('local_path', '')).resolve()
            for item in self._jobs.values()
            if item.get('snapshot', {}).get('local_path')
        }
        for repo_dir in REPO_STORAGE_DIR.iterdir():
            if repo_dir.resolve() not in referenced_repos:
                shutil.rmtree(repo_dir, ignore_errors=True)

    def _now(self) -> str:
        return datetime.now().replace(microsecond=0).isoformat()
