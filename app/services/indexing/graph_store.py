from pathlib import Path

from app.config import NEO4J_DATABASE, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USERNAME

from .schemas import GraphArtifact


class Neo4jGraphStore:
    def is_configured(self) -> bool:
        return bool(NEO4J_URI and NEO4J_USERNAME and NEO4J_PASSWORD)

    def write_graph(self, snapshot_id: str, artifact: GraphArtifact) -> tuple[str, list[str]]:
        if not self.is_configured():
            return "pending_setup", [
                "未检测到完整的 Neo4j 连接信息，图工件已生成但尚未写入图库。",
                "请检查 NEO4J_URI、NEO4J_USERNAME、NEO4J_PASSWORD 的配置。",
            ]

        try:
            from neo4j import GraphDatabase
        except ModuleNotFoundError:
            return "pending_setup", [
                "未安装 neo4j Python 驱动，暂时无法写入图数据库。",
                "请先执行 pip install neo4j，然后重新运行建库任务。",
            ]

        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
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
            "图数据已写入 Neo4j，可以继续实现图查询和证据扩展接口。",
        ]


def write_graph_artifact_file(graph_path: Path, artifact: GraphArtifact) -> None:
    graph_path.write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
