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

