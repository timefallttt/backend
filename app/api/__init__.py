from fastapi import APIRouter
from .search import router as search_router
from .consistency import router as consistency_router
from .feature_extraction import router as feature_router

router = APIRouter()

# 注册搜索路由
router.include_router(search_router, prefix="", tags=["search"])
# 注册特征提取路由
router.include_router(feature_router, prefix="/feature", tags=["feature"])
router.include_router(consistency_router, prefix="/consistency", tags=["consistency"])
