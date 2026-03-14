from fastapi import APIRouter

from .consistency import router as consistency_router
from .feature_extraction import router as feature_router
from .indexing import router as indexing_router
from .search import router as search_router
from .workitems import router as workitems_router

router = APIRouter()

router.include_router(search_router, prefix="", tags=["search"])
router.include_router(feature_router, prefix="/feature", tags=["feature"])
router.include_router(consistency_router, prefix="/consistency", tags=["consistency"])
router.include_router(indexing_router, prefix="/indexing", tags=["indexing"])
router.include_router(workitems_router, prefix="/workitems", tags=["workitems"])
