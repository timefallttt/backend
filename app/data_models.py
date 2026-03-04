from pydantic import BaseModel
from typing import List

# 请求模型
class SearchRequest(BaseModel):
    query: str
    top_k: int
    threshold: float

# 响应模型 - 单个搜索结果
class SearchResult(BaseModel):
    snippet_id: str
    code: str
    filename: str
    start_line: int
    end_line: int
    similarity: float
    commit_message: str

# 响应模型 - 完整搜索响应
class SearchResponse(BaseModel):
    latency_ms: float
    total_found: int
    results: List[SearchResult]

# 状态码
class Status(BaseModel):
    status: str
    message: str