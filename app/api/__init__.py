from fastapi import APIRouter
from .search import router as search_router
from .consistency import router as consistency_router

router = APIRouter()

# 注册搜索路由
router.include_router(search_router, prefix="", tags=["search"])
router.include_router(consistency_router, prefix="/consistency", tags=["consistency"])
