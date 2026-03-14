from fastapi import APIRouter, HTTPException

from app.services.consistency.runtime import consistency_service
from app.services.consistency.schemas import (
    ConsistencyAnalyzeRequest,
    ConsistencyAnalyzeResponse,
    ReviewDashboardResponse,
    ReviewFeedbackRequest,
    ReviewHistoryResponse,
    ReviewTaskCreateRequest,
    ReviewTaskDetail,
    ReviewTaskListResponse,
)

router = APIRouter()


@router.get("/dashboard", response_model=ReviewDashboardResponse)
async def get_review_dashboard() -> ReviewDashboardResponse:
    try:
        return consistency_service.get_dashboard()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/tasks", response_model=ReviewTaskListResponse)
async def list_review_tasks() -> ReviewTaskListResponse:
    try:
        return consistency_service.list_tasks()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/tasks", response_model=ReviewTaskDetail)
async def create_review_task(request: ReviewTaskCreateRequest) -> ReviewTaskDetail:
    try:
        return consistency_service.create_task(request)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/tasks/{task_id}", response_model=ReviewTaskDetail)
async def get_review_task(task_id: str) -> ReviewTaskDetail:
    try:
        return consistency_service.get_task(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/analyze", response_model=ReviewTaskDetail)
async def analyze_review_task(task_id: str) -> ReviewTaskDetail:
    try:
        return consistency_service.analyze_task(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/tasks/{task_id}/history", response_model=ReviewHistoryResponse)
async def get_review_history(task_id: str) -> ReviewHistoryResponse:
    try:
        return consistency_service.get_history(task_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/tasks/{task_id}/feedback", response_model=ReviewTaskDetail)
async def submit_review_feedback(task_id: str, request: ReviewFeedbackRequest) -> ReviewTaskDetail:
    try:
        return consistency_service.submit_feedback(task_id, request)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/analyze", response_model=ConsistencyAnalyzeResponse)
async def analyze_requirement_consistency(
    request: ConsistencyAnalyzeRequest,
) -> ConsistencyAnalyzeResponse:
    try:
        return consistency_service.analyze(request)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
