from fastapi import APIRouter, BackgroundTasks, HTTPException

from app.services.indexing.schemas import (
    IndexJobDetail,
    IndexJobListResponse,
    RepositoryIndexRequest,
)
from app.services.indexing.service import OfflineIndexingService

router = APIRouter()
indexing_service = OfflineIndexingService()


@router.get("/jobs", response_model=IndexJobListResponse)
async def list_index_jobs() -> IndexJobListResponse:
    try:
        return indexing_service.list_jobs()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/jobs/{job_id}", response_model=IndexJobDetail)
async def get_index_job(job_id: str) -> IndexJobDetail:
    try:
        return indexing_service.get_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/jobs", response_model=IndexJobDetail)
async def create_index_job(
    request: RepositoryIndexRequest,
    background_tasks: BackgroundTasks,
) -> IndexJobDetail:
    try:
        detail = indexing_service.create_job(request)
        if request.auto_run:
            background_tasks.add_task(indexing_service.run_job, detail.summary.job_id)
        return detail
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/run", response_model=IndexJobDetail)
async def run_index_job(job_id: str) -> IndexJobDetail:
    try:
        return indexing_service.run_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

