"""导入服务入口（文件上传与导入状态查询）。

启动：
    uvicorn app.api.http.import_server:app --host 0.0.0.0 --port 8000
    python -m app.api.http.import_server
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.api.errors import install_error_handlers
from app.api.routers import import_ as import_router
from app.api.routers import health as health_router
from app.api.routers import pages as pages_router
from app.shared.clients.mongo import ensure_indexes
from app.shared.config import settings


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """服务生命周期：启动时确保 Mongo 索引就绪（失败只告警）。"""
    ensure_indexes()
    yield


app = FastAPI(
    title=settings.api.import_app_name,
    description="企业化 RAG 导入服务：文件上传、导入执行与状态查询。",
    version="0.3.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.api.cors_origins) or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
install_error_handlers(app)

app.include_router(import_router.router)
app.include_router(health_router.router)
app.include_router(pages_router.import_pages_router)
app.include_router(pages_router.static_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.api.host, port=settings.api.import_port)
