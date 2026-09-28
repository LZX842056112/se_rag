"""查询服务入口（客服对话 + 审批后台 + 自进化接口）。

启动：
    uvicorn app.api.http.query_server:app --host 0.0.0.0 --port 8001
    python -m app.api.http.query_server
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware

from app.api.errors import install_error_handlers
from app.api.routers import evolution as evolution_router
from app.api.routers import health as health_router
from app.api.routers import pages as pages_router
from app.api.routers import query as query_router
from app.evolution.scheduler import evolution_scheduler_loop
from app.shared.clients.mongo import ensure_indexes
from app.shared.config import settings
from app.shared.runtime.logger import logger


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """服务生命周期：确保 Mongo 索引就绪，并按开关启停自进化调度器。"""
    ensure_indexes()
    scheduler_task = None
    if settings.evolution.enabled and settings.evolution.scheduler_enabled:
        scheduler_task = asyncio.create_task(
            evolution_scheduler_loop(settings.evolution.schedule_interval_minutes * 60.0)
        )
        logger.info(
            f"自进化调度器已启动：interval={settings.evolution.schedule_interval_minutes}min, "
            f"batch={settings.evolution.scan_batch}"
        )
    try:
        yield
    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass


app = FastAPI(
    title=settings.api.query_app_name,
    description="企业化 RAG 查询服务：客服问答、会话历史、自进化反馈与审批。",
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

app.include_router(query_router.router)
app.include_router(evolution_router.router)
app.include_router(health_router.router)
app.include_router(pages_router.query_pages_router)
app.include_router(pages_router.static_router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=settings.api.host, port=settings.api.query_port)
