from fastapi import APIRouter, HTTPException

from app.modules.consistency.schemas import (
    ConsistencyAnalyzeRequest,
    ConsistencyAnalyzeResponse,
)
from app.modules.consistency.service import ConsistencyService

router = APIRouter()
consistency_service = ConsistencyService()


@router.post("/analyze", response_model=ConsistencyAnalyzeResponse)
async def analyze_requirement_consistency(
    request: ConsistencyAnalyzeRequest,
) -> ConsistencyAnalyzeResponse:
    try:
        return consistency_service.analyze(request)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
