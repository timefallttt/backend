from typing import List, Literal

from pydantic import BaseModel, Field, HttpUrl


IndexJobStatus = Literal[
    "queued",
    "running",
    "completed",
    "completed_with_warnings",
    "failed",
]
ParserMode = Literal["arkanalyzer", "placeholder"]
GraphStoreStatus = Literal["loaded", "pending_setup", "failed", "not_attempted"]
NodeType = Literal["Repository", "File", "Class", "Function"]
EdgeType = Literal["CONTAINS", "CALLS"]
GraphQuerySource = Literal["neo4j", "artifact"]


class RepositoryIndexRequest(BaseModel):
    repo_url: HttpUrl | str = Field(..., description="Git repository URL")
    branch: str = Field("main", min_length=1)
    repo_name: str | None = None
    auto_run: bool = True


class RepoSnapshot(BaseModel):
    repo_url: str
    branch: str
    repo_name: str
    local_path: str = ""
    commit_hash: str = ""


class GraphNode(BaseModel):
    node_id: str
    node_type: NodeType
    name: str
    path: str = ""
    start_line: int | None = None
    end_line: int | None = None
    signature: str = ""


class GraphEdge(BaseModel):
    edge_type: EdgeType
    source_id: str
    target_id: str
    detail: str = ""


class GraphBuildStats(BaseModel):
    file_count: int = 0
    class_count: int = 0
    function_count: int = 0
    edge_count: int = 0


class GraphArtifact(BaseModel):
    nodes: List[GraphNode] = Field(default_factory=list)
    edges: List[GraphEdge] = Field(default_factory=list)
    stats: GraphBuildStats = Field(default_factory=GraphBuildStats)


class IndexJobSummary(BaseModel):
    job_id: str
    status: IndexJobStatus
    repo_name: str
    branch: str
    parser_mode: ParserMode
    graph_store_status: GraphStoreStatus
    created_at: str
    updated_at: str


class IndexJobDetail(BaseModel):
    summary: IndexJobSummary
    snapshot: RepoSnapshot
    current_step: str
    logs: List[str] = Field(default_factory=list)
    artifact_dir: str = ""
    parser_output_path: str = ""
    graph_artifact_path: str = ""
    graph_stats: GraphBuildStats = Field(default_factory=GraphBuildStats)
    setup_hints: List[str] = Field(default_factory=list)


class IndexJobListResponse(BaseModel):
    jobs: List[IndexJobSummary] = Field(default_factory=list)


class GraphSeedQuery(BaseModel):
    node_id: str = ""
    name: str = ""
    path: str = ""
    signature: str = ""
    max_matches: int = Field(5, ge=1, le=20)


class GraphEvidenceQueryRequest(BaseModel):
    seeds: List[GraphSeedQuery] = Field(default_factory=list)
    max_hops: int = Field(2, ge=1, le=3)
    max_paths: int = Field(20, ge=1, le=100)
    edge_types: List[EdgeType] = Field(default_factory=lambda: ["CALLS", "CONTAINS"])


class GraphSeedMatch(BaseModel):
    seed: GraphSeedQuery
    matched_nodes: List[GraphNode] = Field(default_factory=list)


class GraphPathStep(BaseModel):
    node_id: str
    node_type: NodeType
    name: str
    path: str = ""
    relation_from_prev: EdgeType | None = None


class GraphEvidencePath(BaseModel):
    path_id: str
    hop_count: int = Field(..., ge=0)
    nodes: List[GraphPathStep] = Field(default_factory=list)


class GraphEvidenceSummary(BaseModel):
    matched_seed_count: int = 0
    expanded_node_count: int = 0
    expanded_edge_count: int = 0
    evidence_path_count: int = 0


class GraphEvidenceQueryResponse(BaseModel):
    job_id: str
    snapshot_id: str
    source: GraphQuerySource
    seed_matches: List[GraphSeedMatch] = Field(default_factory=list)
    nodes: List[GraphNode] = Field(default_factory=list)
    edges: List[GraphEdge] = Field(default_factory=list)
    paths: List[GraphEvidencePath] = Field(default_factory=list)
    summary: GraphEvidenceSummary = Field(default_factory=GraphEvidenceSummary)
    hints: List[str] = Field(default_factory=list)
