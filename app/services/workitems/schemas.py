from typing import List, Literal

from pydantic import BaseModel, Field


ConnectorMode = Literal['demo', 'custom']
DiffChangeType = Literal['added', 'modified', 'deleted', 'renamed']


class WorkItemConnectorSummary(BaseModel):
    connector_key: str
    name: str
    description: str
    mode: ConnectorMode = 'demo'


class WorkItemCodeSeed(BaseModel):
    seed_id: str
    filename: str
    code: str
    start_line: int = Field(1, ge=1)
    end_line: int = Field(1, ge=1)
    recall_reason: str = ''
    source: str = 'workitem'


class WorkItemDiffHunk(BaseModel):
    hunk_id: str
    header: str = ''
    start_line: int = Field(1, ge=1)
    end_line: int = Field(1, ge=1)
    added_lines: List[str] = Field(default_factory=list)
    removed_lines: List[str] = Field(default_factory=list)
    context_lines: List[str] = Field(default_factory=list)


class WorkItemFileDiff(BaseModel):
    diff_id: str
    filename: str
    change_type: DiffChangeType = 'modified'
    additions: int = Field(0, ge=0)
    deletions: int = Field(0, ge=0)
    hunks: List[WorkItemDiffHunk] = Field(default_factory=list)


class WorkItemLinkedCommit(BaseModel):
    commit_id: str
    commit_hash: str
    title: str
    author: str = ''
    created_at: str = ''
    message: str = ''
    file_diffs: List[WorkItemFileDiff] = Field(default_factory=list)


class WorkItemSummary(BaseModel):
    connector_key: str
    item_id: str
    requirement_id: str
    title: str
    repo_name: str = ''
    business_tag: str = ''
    priority: Literal['high', 'medium', 'low'] = 'medium'
    status: str = 'open'
    updated_at: str


class WorkItemDetail(WorkItemSummary):
    requirement_text: str
    acceptance_criteria: List[str] = Field(default_factory=list)
    owner: str = ''
    notes: str = ''
    external_url: str = ''
    linked_commits: List[WorkItemLinkedCommit] = Field(default_factory=list)
    derived_seeds: List[WorkItemCodeSeed] = Field(default_factory=list)
    derived_seed_count: int = 0
    snapshot_hint: str = ''


class WorkItemConnectorListResponse(BaseModel):
    connectors: List[WorkItemConnectorSummary] = Field(default_factory=list)


class WorkItemListResponse(BaseModel):
    connector: WorkItemConnectorSummary
    items: List[WorkItemSummary] = Field(default_factory=list)


class WorkItemImportRequest(BaseModel):
    connector_key: str = Field(..., min_length=1)
    item_id: str = Field(..., min_length=1)
    index_job_id: str = ''
    repo_name: str = ''
    snapshot: str = ''
    auto_expand_graph_evidence: bool = True
