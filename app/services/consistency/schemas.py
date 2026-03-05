from typing import List, Literal
from pydantic import BaseModel, Field


JudgementStatus = Literal["satisfied", "partially_satisfied", "not_satisfied"]


class CandidateSnippet(BaseModel):
    snippet_id: str = Field(..., description="Client-side snippet identifier")
    filename: str = Field(..., description="Source file path")
    code: str = Field(..., description="Code content used for analysis")
    start_line: int = Field(1, ge=1)
    end_line: int = Field(1, ge=1)


class AnalyzeOptions(BaseModel):
    top_k: int = Field(10, ge=1, le=50)
    keyword_min_overlap: float = Field(0.2, ge=0, le=1)
    enable_tool_evidence: bool = True


class ConsistencyAnalyzeRequest(BaseModel):
    requirement_text: str = Field(..., min_length=1)
    acceptance_criteria: List[str] = Field(default_factory=list)
    candidate_snippets: List[CandidateSnippet] = Field(default_factory=list)
    options: AnalyzeOptions = Field(default_factory=AnalyzeOptions)


class EvidenceRef(BaseModel):
    snippet_id: str
    filename: str
    start_line: int
    end_line: int
    reason: str


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


class RequirementSpec(BaseModel):
    intents: List[str] = Field(default_factory=list)
    constraints: List[str] = Field(default_factory=list)
    exceptions: List[str] = Field(default_factory=list)


class ConsistencyAnalyzeResponse(BaseModel):
    overall_score: float = Field(..., ge=0, le=1)
    overall_confidence: float = Field(..., ge=0, le=1)
    status: Literal["completed", "needs_review"]
    requirement_spec: RequirementSpec
    judgements: List[ItemJudgement]
    missing_items: List[str] = Field(default_factory=list)
    tool_findings: List[ToolFinding] = Field(default_factory=list)
    summary: str

