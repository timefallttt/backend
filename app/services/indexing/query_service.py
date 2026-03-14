from collections import deque
from typing import Iterable
from pathlib import Path

from .graph_store import Neo4jGraphStore
from .schemas import (
    GraphArtifact,
    GraphEdge,
    GraphEvidencePath,
    GraphEvidenceQueryRequest,
    GraphEvidenceQueryResponse,
    GraphEvidenceSummary,
    GraphNode,
    GraphPathStep,
    GraphSeedMatch,
    GraphSeedQuery,
    IndexJobDetail,
)


class GraphQueryService:
    def __init__(self) -> None:
        self._graph_store = Neo4jGraphStore()

    def query_job_evidence(
        self,
        job: IndexJobDetail,
        request: GraphEvidenceQueryRequest,
    ) -> GraphEvidenceQueryResponse:
        if not request.seeds:
            raise ValueError("at least one seed is required")

        snapshot_id = self._build_snapshot_id(job)
        hints: list[str] = []
        source = "artifact"
        seed_matches: list[GraphSeedMatch] = []
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []

        if job.summary.graph_store_status == "loaded":
            try:
                seed_matches, nodes, edges = self._graph_store.query_subgraph(
                    snapshot_id=snapshot_id,
                    seeds=request.seeds,
                    max_hops=request.max_hops,
                    max_paths=request.max_paths,
                    edge_types=request.edge_types,
                )
                source = "neo4j"
            except ModuleNotFoundError:
                hints.append("未安装 neo4j Python 驱动，图查询已回退到本地图工件。")
            except Exception as exc:
                hints.append(f"Neo4j 图查询失败，已回退到本地图工件：{exc}")

        if source != "neo4j":
            artifact = self._load_artifact(job)
            seed_matches, nodes, edges = self._query_artifact(artifact, request)
            if job.summary.graph_store_status != "loaded":
                hints.append("当前结果来自本地图工件，尚未直接从 Neo4j 查询。")

        paths = self._build_paths(
            seed_matches=seed_matches,
            nodes=nodes,
            edges=edges,
            max_hops=request.max_hops,
            max_paths=request.max_paths,
        )

        summary = GraphEvidenceSummary(
            matched_seed_count=sum(1 for item in seed_matches if item.matched_nodes),
            expanded_node_count=len(nodes),
            expanded_edge_count=len(edges),
            evidence_path_count=len(paths),
        )

        return GraphEvidenceQueryResponse(
            job_id=job.summary.job_id,
            snapshot_id=snapshot_id,
            source=source,
            seed_matches=seed_matches,
            nodes=sorted(nodes, key=lambda item: (item.node_type, item.path, item.name)),
            edges=sorted(edges, key=lambda item: (item.edge_type, item.source_id, item.target_id)),
            paths=paths,
            summary=summary,
            hints=hints,
        )

    def _load_artifact(self, job: IndexJobDetail) -> GraphArtifact:
        artifact_path = Path(job.graph_artifact_path)
        if not artifact_path.exists():
            raise ValueError(f"graph artifact not found for job {job.summary.job_id}")
        return GraphArtifact.model_validate_json(artifact_path.read_text(encoding="utf-8"))

    def _query_artifact(
        self,
        artifact: GraphArtifact,
        request: GraphEvidenceQueryRequest,
    ) -> tuple[list[GraphSeedMatch], list[GraphNode], list[GraphEdge]]:
        nodes_by_id = {node.node_id: node for node in artifact.nodes}
        edges = [edge for edge in artifact.edges if edge.edge_type in request.edge_types]
        seed_matches = [
            GraphSeedMatch(
                seed=seed,
                matched_nodes=self._match_nodes(nodes_by_id.values(), seed),
            )
            for seed in request.seeds
        ]

        seed_node_ids = {
            node.node_id
            for match in seed_matches
            for node in match.matched_nodes
        }
        if not seed_node_ids:
            return seed_matches, [], []

        adjacency: dict[str, list[tuple[str, GraphEdge]]] = {}
        for edge in edges:
            adjacency.setdefault(edge.source_id, []).append((edge.target_id, edge))
            adjacency.setdefault(edge.target_id, []).append((edge.source_id, edge))

        visited = {node_id: 0 for node_id in seed_node_ids}
        queue = deque((node_id, 0) for node_id in seed_node_ids)
        kept_edges: dict[str, GraphEdge] = {}

        while queue:
            current_id, depth = queue.popleft()
            if depth >= request.max_hops:
                continue

            for neighbor_id, edge in adjacency.get(current_id, []):
                next_depth = depth + 1
                if neighbor_id not in visited or next_depth < visited[neighbor_id]:
                    visited[neighbor_id] = next_depth
                    queue.append((neighbor_id, next_depth))
                if visited.get(current_id, depth) < request.max_hops and neighbor_id in nodes_by_id:
                    kept_edges[self._edge_key(edge)] = edge

        kept_node_ids = set(visited.keys())
        kept_nodes = [nodes_by_id[node_id] for node_id in kept_node_ids if node_id in nodes_by_id]
        kept_edge_list = [
            edge
            for edge in kept_edges.values()
            if edge.source_id in kept_node_ids and edge.target_id in kept_node_ids
        ]
        return seed_matches, kept_nodes, kept_edge_list

    def _match_nodes(self, nodes: Iterable[GraphNode], seed: GraphSeedQuery) -> list[GraphNode]:
        result: list[GraphNode] = []
        normalized_path = seed.path.replace("\\", "/").lower()
        normalized_name = seed.name.lower()
        for node in nodes:
            if seed.node_id and node.node_id == seed.node_id:
                result.append(node)
                continue
            if seed.path and node.path.lower() == normalized_path:
                result.append(node)
                continue
            if seed.name and normalized_name in node.name.lower():
                result.append(node)
                continue
            if seed.signature and seed.signature in node.signature:
                result.append(node)
        return result[: seed.max_matches]

    def _build_paths(
        self,
        seed_matches: list[GraphSeedMatch],
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        max_hops: int,
        max_paths: int,
    ) -> list[GraphEvidencePath]:
        nodes_by_id = {node.node_id: node for node in nodes}
        adjacency: dict[str, list[tuple[str, GraphEdge]]] = {}
        for edge in edges:
            adjacency.setdefault(edge.source_id, []).append((edge.target_id, edge))
            adjacency.setdefault(edge.target_id, []).append((edge.source_id, edge))

        paths: list[GraphEvidencePath] = []
        seen_path_ids: set[str] = set()
        for match in seed_matches:
            for seed_node in match.matched_nodes:
                queue = deque([(seed_node.node_id, 0)])
                parents: dict[str, tuple[str | None, GraphEdge | None]] = {
                    seed_node.node_id: (None, None)
                }
                while queue and len(paths) < max_paths:
                    current_id, depth = queue.popleft()
                    if depth >= max_hops:
                        continue
                    for neighbor_id, edge in adjacency.get(current_id, []):
                        if neighbor_id in parents:
                            continue
                        parents[neighbor_id] = (current_id, edge)
                        queue.append((neighbor_id, depth + 1))

                for target_id, (parent_id, _) in parents.items():
                    if target_id == seed_node.node_id or parent_id is None:
                        continue
                    path_id = f"{seed_node.node_id}->{target_id}"
                    if path_id in seen_path_ids:
                        continue
                    path = self._trace_path(target_id, parents, nodes_by_id)
                    if len(path.nodes) < 2:
                        continue
                    seen_path_ids.add(path_id)
                    paths.append(path)
                    if len(paths) >= max_paths:
                        return paths
        return paths

    def _trace_path(
        self,
        target_id: str,
        parents: dict[str, tuple[str | None, GraphEdge | None]],
        nodes_by_id: dict[str, GraphNode],
    ) -> GraphEvidencePath:
        chain: list[tuple[str, GraphEdge | None]] = []
        current_id = target_id
        while current_id in parents:
            parent_id, edge = parents[current_id]
            chain.append((current_id, edge))
            if parent_id is None:
                break
            current_id = parent_id

        chain.reverse()
        steps: list[GraphPathStep] = []
        for node_id, edge in chain:
            node = nodes_by_id.get(node_id)
            if not node:
                continue
            steps.append(
                GraphPathStep(
                    node_id=node.node_id,
                    node_type=node.node_type,
                    name=node.name,
                    path=node.path,
                    start_line=node.start_line,
                    end_line=node.end_line,
                    signature=node.signature,
                    relation_from_prev=edge.edge_type if edge else None,
                )
            )

        return GraphEvidencePath(
            path_id=f"path:{steps[0].node_id}:{steps[-1].node_id}",
            hop_count=max(len(steps) - 1, 0),
            nodes=steps,
        )

    def _build_snapshot_id(self, job: IndexJobDetail) -> str:
        return f"{job.snapshot.repo_name}@{job.snapshot.commit_hash or job.snapshot.branch}"

    def _edge_key(self, edge: GraphEdge) -> str:
        return f"{edge.edge_type}:{edge.source_id}:{edge.target_id}:{edge.detail}"
