from typing import List, Literal

from pydantic import BaseModel, Field


JudgementStatus = Literal["satisfied", "partially_satisfied", "not_satisfied"]
TaskStatus = Literal["draft", "ready", "completed", "needs_review"]
FeedbackDecision = Literal["agree", "question", "misjudged"]
NodeType = Literal["requirement", "file", "class", "function", "constraint", "ui", "service"]


class CandidateSnippet(BaseModel):
    snippet_id: str = Field(..., description="Snippet identifier")
    filename: str = Field(..., description="Source file path")
    code: str = Field(..., description="Code content used for analysis")
    start_line: int = Field(1, ge=1)
    end_line: int = Field(1, ge=1)
    recall_reason: str = ""
    source: str = "retrieval"
    selected: bool = True


class AnalyzeOptions(BaseModel):
    top_k: int = Field(10, ge=1, le=50)
    keyword_min_overlap: float = Field(0.2, ge=0, le=1)
    enable_tool_evidence: bool = True


class GraphEvidenceStepInput(BaseModel):
    node_id: str
    node_type: str
    name: str
    path: str = ""
    start_line: int | None = None
    end_line: int | None = None
    signature: str = ""
    code_excerpt: str = ""
    relation_from_prev: str | None = None


class GraphEvidencePathInput(BaseModel):
    path_id: str
    hop_count: int = Field(0, ge=0)
    nodes: List[GraphEvidenceStepInput] = Field(default_factory=list)


class GraphEvidenceSummaryInput(BaseModel):
    matched_seed_count: int = 0
    expanded_node_count: int = 0
    expanded_edge_count: int = 0
    evidence_path_count: int = 0


class GraphEvidenceBundle(BaseModel):
    source: str = "artifact"
    hints: List[str] = Field(default_factory=list)
    paths: List[GraphEvidencePathInput] = Field(default_factory=list)
    summary: GraphEvidenceSummaryInput = Field(default_factory=GraphEvidenceSummaryInput)


class RequirementSpec(BaseModel):
    intents: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    exceptions: List[str] = Field(default_factory=list)


class EvidenceRef(BaseModel):
    snippet_id: str
    filename: str
    start_line: int
    end_line: int
    reason: str


class EvidencePathNode(BaseModel):
    node_id: str
    label: str
    node_type: NodeType
    relation_from_prev: str | None = None
    detail: str = ""


class EvidencePath(BaseModel):
    path_id: str
    title: str
    summary: str
    supports_items: List[str] = Field(default_factory=list)
    nodes: List[EvidencePathNode] = Field(default_factory=list)


class ItemJudgement(BaseModel):
    item: str
    status: JudgementStatus
    score: float = Field(..., ge=0, le=1)
    confidence: float = Field(..., ge=0, le=1)
    evidence: List[EvidenceRef] = Field(default_factory=list)
    notes: str = ""


class ToolFinding(BaseModel):
    level: Literal["info", "warning", "error"]
    message: str
    related_item: str | None = None


class LlmEvidenceSnippet(BaseModel):
    snippet_id: str
    source: Literal["candidate", "diff_seed"]
    filename: str
    start_line: int
    end_line: int
    code: str
    reason: str = ""


class LlmEvidenceGraphNode(BaseModel):
    node_id: str
    label: str
    node_type: NodeType
    path: str = ""
    start_line: int | None = None
    end_line: int | None = None
    signature: str = ""
    code_excerpt: str = ""
    relation_from_prev: str | None = None


class LlmEvidencePath(BaseModel):
    path_id: str
    title: str
    summary: str
    supports_items: List[str] = Field(default_factory=list)
    nodes: List[LlmEvidenceGraphNode] = Field(default_factory=list)


class LlmEvidenceRequirementItem(BaseModel):
    item: str
    status_hint: JudgementStatus
    snippet_ids: List[str] = Field(default_factory=list)
    path_ids: List[str] = Field(default_factory=list)
    negative_signals: List[str] = Field(default_factory=list)


class LlmEvidencePack(BaseModel):
    requirement_text: str
    acceptance_criteria: List[str] = Field(default_factory=list)
    snippets: List[LlmEvidenceSnippet] = Field(default_factory=list)
    graph_paths: List[LlmEvidencePath] = Field(default_factory=list)
    requirement_items: List[LlmEvidenceRequirementItem] = Field(default_factory=list)
    structural_gaps: List[str] = Field(default_factory=list)
    tool_findings: List[ToolFinding] = Field(default_factory=list)


class ReviewReport(BaseModel):
    overall_score: float = Field(..., ge=0, le=1)
    overall_confidence: float = Field(..., ge=0, le=1)
    status: Literal["completed", "needs_review"]
    requirement_spec: RequirementSpec
    judgements: List[ItemJudgement]
    missing_items: List[str] = Field(default_factory=list)
    tool_findings: List[ToolFinding] = Field(default_factory=list)
    evidence_paths: List[EvidencePath] = Field(default_factory=list)
    structural_gaps: List[str] = Field(default_factory=list)
    review_focuses: List[str] = Field(default_factory=list)
    evidence_pack: LlmEvidencePack | None = None
    summary: str


class ConsistencyAnalyzeRequest(BaseModel):
    requirement_text: str = Field(..., min_length=1)
    acceptance_criteria: List[str] = Field(default_factory=list)
    candidate_snippets: List[CandidateSnippet] = Field(default_factory=list)
    graph_evidence: GraphEvidenceBundle | None = None
    options: AnalyzeOptions = Field(default_factory=AnalyzeOptions)


class ConsistencyAnalyzeResponse(ReviewReport):
    pass


class ReviewTaskCreateRequest(BaseModel):
    requirement_id: str = Field(..., min_length=1)
    title: str = Field(..., min_length=1)
    requirement_text: str = Field(..., min_length=1)
    acceptance_criteria: List[str] = Field(default_factory=list)
    repo_name: str = Field(..., min_length=1)
    snapshot: str = Field(..., min_length=1)
    business_tag: str = ""
    priority: Literal["high", "medium", "low"] = "medium"
    owner: str = ""
    notes: str = ""
    candidate_snippets: List[CandidateSnippet] = Field(default_factory=list)
    graph_evidence: GraphEvidenceBundle | None = None
    options: AnalyzeOptions = Field(default_factory=AnalyzeOptions)


class ReviewTaskSummary(BaseModel):
    task_id: str
    requirement_id: str
    title: str
    repo_name: str
    snapshot: str
    business_tag: str
    priority: Literal["high", "medium", "low"]
    status: TaskStatus
    overall_score: float = Field(..., ge=0, le=1)
    updated_at: str


class ReviewFeedback(BaseModel):
    feedback_id: str
    judgement_item: str
    decision: FeedbackDecision
    comment: str
    reviewer: str
    created_at: str


class ReviewHistoryRecord(BaseModel):
    record_id: str
    label: str
    status: TaskStatus
    overall_score: float = Field(..., ge=0, le=1)
    summary: str
    changed_points: List[str] = Field(default_factory=list)
    created_at: str


class ReviewTaskDetail(BaseModel):
    task: ReviewTaskSummary
    requirement_text: str
    acceptance_criteria: List[str] = Field(default_factory=list)
    owner: str = ""
    notes: str = ""
    candidate_snippets: List[CandidateSnippet] = Field(default_factory=list)
    graph_evidence: GraphEvidenceBundle | None = None
    report: ReviewReport | None = None
    feedback_entries: List[ReviewFeedback] = Field(default_factory=list)


class ReviewTaskListResponse(BaseModel):
    tasks: List[ReviewTaskSummary] = Field(default_factory=list)


class ReviewHistoryResponse(BaseModel):
    task_id: str
    records: List[ReviewHistoryRecord] = Field(default_factory=list)


class ReviewDashboardStats(BaseModel):
    total_tasks: int
    needs_review_tasks: int
    completed_tasks: int
    avg_score: float = Field(..., ge=0, le=1)


class ReviewDashboardResponse(BaseModel):
    stats: ReviewDashboardStats
    tasks: List[ReviewTaskSummary] = Field(default_factory=list)


class ReviewFeedbackRequest(BaseModel):
    judgement_item: str = Field(..., min_length=1)
    decision: FeedbackDecision
    comment: str = ""
    reviewer: str = Field(..., min_length=1)
