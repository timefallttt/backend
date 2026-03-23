from fastapi import APIRouter

from .consistency import router as consistency_router
from .indexing import router as indexing_router
from .workitems import router as workitems_router

router = APIRouter()

router.include_router(consistency_router, prefix="/consistency", tags=["consistency"])
router.include_router(indexing_router, prefix="/indexing", tags=["indexing"])
router.include_router(workitems_router, prefix="/workitems", tags=["workitems"])
