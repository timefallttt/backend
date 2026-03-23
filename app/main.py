import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api import router as api_router
from app.data_models import Status

app = FastAPI(
    title="ArkTS Code AR Backend API",
    description="基于双塔语义模型的智能代码检索与变更追溯后端服务",
    version="1.0.0"
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],  # 在生产环境中应该设置为具体的前端域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 集成 API 路由
app.include_router(api_router, prefix="/api/v1", tags=["api"])

# 根路径
@app.get("/", response_model=Status)
async def root():
    """
    根路径，返回服务状态
    """
    return Status(
        status="healthy",
        message="ArkTS Code AR Backend API is running"
    )

# 健康检查端点
@app.get("/health", response_model=Status)
async def health_check():
    """
    健康检查端点
    """
    return Status(
        status="healthy",
        message="Service is operational"
    )

if __name__ == "__main__":
    import uvicorn
    # 本地运行配置
    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )
