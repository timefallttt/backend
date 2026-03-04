from fastapi import APIRouter, HTTPException
from ..data_models import SearchRequest, SearchResponse
from ..services.search_service import SearchService

router = APIRouter()
search_service = SearchService()

@router.post("/retrieve", response_model=SearchResponse)
async def retrieve_code(request: SearchRequest):
    """
    检索代码片段
    
    Args:
        request: 搜索请求参数
        
    Returns:
        SearchResponse: 搜索结果响应
    """
    try:
        result = await search_service.search_code(
            query=request.query,
            top_k=request.top_k,
            threshold=request.threshold
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))