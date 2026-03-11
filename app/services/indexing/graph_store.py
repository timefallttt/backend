from pathlib import Path

from app.config import NEO4J_DATABASE, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USERNAME

from .schemas import GraphArtifact, GraphEdge, GraphNode, GraphSeedMatch, GraphSeedQuery


class Neo4jGraphStore:
    def is_configured(self) -> bool:
        return bool(NEO4J_URI and NEO4J_USERNAME and NEO4J_PASSWORD)

    def write_graph(self, snapshot_id: str, artifact: GraphArtifact) -> tuple[str, list[str]]:
        if not self.is_configured():
            return "pending_setup", [
                "未检测到完整的 Neo4j 连接配置，图工件已生成但尚未写入图库。",
                "请检查 NEO4J_URI、NEO4J_USERNAME、NEO4J_PASSWORD 的配置。",
            ]

        try:
            driver = self._create_driver()
        except ModuleNotFoundError:
            return "pending_setup", [
                "未安装 neo4j Python 驱动，暂时无法写入图数据库。",
                "请先执行 pip install neo4j，然后重新运行建库任务。",
            ]

        try:
            with driver.session(database=NEO4J_DATABASE) as session:
                session.run(
                    "MERGE (r:RepositorySnapshot {snapshot_id: $snapshot_id})",
                    snapshot_id=snapshot_id,
                )
                for node in artifact.nodes:
                    session.run(
                        """
                        MATCH (r:RepositorySnapshot {snapshot_id: $snapshot_id})
                        MERGE (n:CodeNode {node_id: $node_id})
                        SET n.node_type = $node_type,
                            n.name = $name,
                            n.path = $path,
                            n.start_line = $start_line,
                            n.end_line = $end_line,
                            n.signature = $signature
                        MERGE (r)-[:CONTAINS]->(n)
                        """,
                        snapshot_id=snapshot_id,
                        node_id=node.node_id,
                        node_type=node.node_type,
                        name=node.name,
                        path=node.path,
                        start_line=node.start_line,
                        end_line=node.end_line,
                        signature=node.signature,
                    )
                for edge in artifact.edges:
                    session.run(
                        """
                        MATCH (a:CodeNode {node_id: $source_id})
                        MATCH (b:CodeNode {node_id: $target_id})
                        MERGE (a)-[r:RELATES_TO {edge_type: $edge_type, target_id: $target_id}]->(b)
                        SET r.detail = $detail
                        """,
                        source_id=edge.source_id,
                        target_id=edge.target_id,
                        edge_type=edge.edge_type,
                        detail=edge.detail,
                    )
        except Exception as exc:
            return "failed", [
                f"Neo4j 写入失败：{exc}",
                "请确认 Neo4j 已通过 neo4j console 启动，并检查账号密码是否正确。",
            ]
        finally:
            driver.close()

        return "loaded", [
            "图数据已写入 Neo4j，可继续实现图查询和证据扩展接口。",
        ]

    def query_subgraph(
        self,
        snapshot_id: str,
        seeds: list[GraphSeedQuery],
        max_hops: int,
        max_paths: int,
        edge_types: list[str],
    ) -> tuple[list[GraphSeedMatch], list[GraphNode], list[GraphEdge]]:
        driver = self._create_driver()
        try:
            with driver.session(database=NEO4J_DATABASE) as session:
                seed_matches = [
                    GraphSeedMatch(seed=seed, matched_nodes=self._match_seed_nodes(session, snapshot_id, seed))
                    for seed in seeds
                ]
                seed_node_ids = [
                    node.node_id
                    for match in seed_matches
                    for node in match.matched_nodes
                ]
                if not seed_node_ids:
                    return seed_matches, [], []

                nodes = {node.node_id: node for match in seed_matches for node in match.matched_nodes}
                edges: dict[str, GraphEdge] = {}

                for query in (
                    self._build_path_query(max_hops, "outgoing"),
                    self._build_path_query(max_hops, "incoming"),
                ):
                    result = session.run(
                        query,
                        snapshot_id=snapshot_id,
                        seed_ids=seed_node_ids,
                        edge_types=edge_types,
                        limit=max_paths,
                    )
                    for record in result:
                        path = record["p"]
                        for node in path.nodes:
                            graph_node = self._graph_node_from_neo4j(node)
                            nodes[graph_node.node_id] = graph_node
                        for index, rel in enumerate(path.relationships):
                            edge = GraphEdge(
                                edge_type=rel.get("edge_type", "CALLS"),
                                source_id=path.nodes[index].get("node_id", ""),
                                target_id=path.nodes[index + 1].get("node_id", ""),
                                detail=rel.get("detail", ""),
                            )
                            edges[self._edge_key(edge)] = edge

                return seed_matches, list(nodes.values()), list(edges.values())
        finally:
            driver.close()

    def _create_driver(self):
        from neo4j import GraphDatabase

        return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))

    def _match_seed_nodes(self, session, snapshot_id: str, seed: GraphSeedQuery) -> list[GraphNode]:
        filters = []
        parameters = {
            "snapshot_id": snapshot_id,
            "limit": seed.max_matches,
            "node_id": seed.node_id,
            "name": seed.name,
            "path": seed.path.replace("\\", "/"),
            "signature": seed.signature,
        }

        if seed.node_id:
            filters.append("n.node_id = $node_id")
        if seed.path:
            filters.append("toLower(n.path) = toLower($path)")
        if seed.name:
            filters.append("toLower(n.name) CONTAINS toLower($name)")
        if seed.signature:
            filters.append("n.signature CONTAINS $signature")

        if not filters:
            return []

        query = f"""
            MATCH (snap:RepositorySnapshot {{snapshot_id: $snapshot_id}})-[:CONTAINS]->(n:CodeNode)
            WHERE {' OR '.join(filters)}
            RETURN DISTINCT n
            LIMIT $limit
        """
        result = session.run(query, parameters)
        return [self._graph_node_from_neo4j(record["n"]) for record in result]

    def _build_path_query(self, max_hops: int, direction: str) -> str:
        if direction == "outgoing":
            pattern = f"(seed)-[rels:RELATES_TO*1..{max_hops}]->(other:CodeNode)"
        else:
            pattern = f"(other:CodeNode)-[rels:RELATES_TO*1..{max_hops}]->(seed)"

        return f"""
            MATCH (snap:RepositorySnapshot {{snapshot_id: $snapshot_id}})-[:CONTAINS]->(seed:CodeNode)
            WHERE seed.node_id IN $seed_ids
            MATCH p={pattern}
            WHERE (snap)-[:CONTAINS]->(other)
              AND ALL(rel IN rels WHERE rel.edge_type IN $edge_types)
            RETURN DISTINCT p
            LIMIT $limit
        """

    def _graph_node_from_neo4j(self, node) -> GraphNode:
        return GraphNode(
            node_id=node.get("node_id", ""),
            node_type=node.get("node_type", "Function"),
            name=node.get("name", ""),
            path=node.get("path", ""),
            start_line=node.get("start_line"),
            end_line=node.get("end_line"),
            signature=node.get("signature", ""),
        )

    def _edge_key(self, edge: GraphEdge) -> str:
        return f"{edge.edge_type}:{edge.source_id}:{edge.target_id}:{edge.detail}"
def write_graph_artifact_file(graph_path: Path, artifact: GraphArtifact) -> None:
    graph_path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
