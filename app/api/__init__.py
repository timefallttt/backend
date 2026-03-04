from fastapi import APIRouter
from .search import router as search_router

router = APIRouter()

# 注册搜索路由
router.include_router(search_router, prefix="", tags=["search"])