from fastapi import APIRouter, BackgroundTasks, HTTPException, Response

from app.services.indexing.runtime import graph_query_service, indexing_service
from app.services.indexing.schemas import (
    GraphEvidenceQueryRequest,
    GraphEvidenceQueryResponse,
    IndexJobDetail,
    IndexJobListResponse,
    RepositoryIndexRequest,
)

router = APIRouter()


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


@router.delete("/jobs/{job_id}", status_code=204)
async def delete_index_job(job_id: str) -> Response:
    try:
        indexing_service.delete_job(job_id)
        return Response(status_code=204)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/jobs/{job_id}/evidence", response_model=GraphEvidenceQueryResponse)
async def query_index_job_evidence(
    job_id: str,
    request: GraphEvidenceQueryRequest,
) -> GraphEvidenceQueryResponse:
    try:
        job = indexing_service.get_job(job_id)
        return graph_query_service.query_job_evidence(job, request)
    except ValueError as exc:
        status_code = 404 if "not found" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
