from fastapi import APIRouter, HTTPException, Query

from app.services.workitems.runtime import workitem_service
from app.services.workitems.schemas import (
    WorkItemConnectorListResponse,
    WorkItemDetail,
    WorkItemImportRequest,
    WorkItemListResponse,
)
from app.services.consistency.schemas import ReviewTaskDetail

router = APIRouter()


@router.get("/connectors", response_model=WorkItemConnectorListResponse)
async def list_workitem_connectors() -> WorkItemConnectorListResponse:
    try:
        return workitem_service.list_connectors()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/items", response_model=WorkItemListResponse)
async def list_workitems(connector: str = Query(..., min_length=1)) -> WorkItemListResponse:
    try:
        return workitem_service.list_items(connector)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/items/{item_id}", response_model=WorkItemDetail)
async def get_workitem_detail(
    item_id: str,
    connector: str = Query(..., min_length=1),
) -> WorkItemDetail:
    try:
        return workitem_service.get_item(connector, item_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/import", response_model=ReviewTaskDetail)
async def import_workitem(request: WorkItemImportRequest) -> ReviewTaskDetail:
    try:
        return workitem_service.import_item(request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
