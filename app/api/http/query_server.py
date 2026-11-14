"""查询服务入口（客服对话 + 审批后台 + 自进化接口）。

启动：
    uvicorn app.api.http.query_server:app --host 0.0.0.0 --port 8001
    python -m app.api.http.query_server
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import time

from fastapi import FastAPI, Request
from starlette.middleware.cors import CORSMiddleware

from app.api.errors import install_error_handlers
from app.api.session_access import install_session_cookie
from app.shared.runtime.single_worker import SingleWorkerLease
from app.api.routers import evolution as evolution_router
from app.api.routers import health as health_router
from app.api.routers import pages as pages_router
from app.api.routers import query as query_router
from app.evolution.leader import scheduler_leader_loop
from app.evolution.scheduler import evolution_scheduler_loop
from app.evolution.flags import writes_enabled
from app.shared.clients import redis_gateway
from app.shared.clients.mongo import ensure_indexes
from app.shared.config import settings
from app.shared.runtime.logger import logger


def warmup_models() -> None:
    """预热检索模型：把冷启动成本移出首个请求的 60 秒预算。

    BGE-M3 与重排模型都是进程内单例，首次调用才导入 torch 并加载权重。
    本机 CPU 实测 BGE-M3 冷启动约 28 秒。该耗时若发生在请求内，会直接耗尽
    Agentic 的总时限（见 app/rag/agentic/deadline.py），导致检索与作答全部失败。
    预热失败只告警，不影响服务启动。
    """
    try:
        from app.shared.models.embedding import get_bge_m3_ef
        from app.shared.models.reranker import get_reranker_model

        logger.info("开始预热检索模型（BGE-M3 / 重排）")
        get_bge_m3_ef()
        get_reranker_model()
        logger.info("检索模型预热完成")
    except Exception as exc:  # noqa: BLE001 - 模型缺失或显存不足时仍允许服务启动
        logger.warning(f"检索模型预热失败（不影响启动）：{exc}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """服务生命周期：确保 Mongo 索引就绪，并按开关启停自进化调度器。

    多 worker 模式（Redis 后端）不抢单 worker 文件锁；调度器改用 Redis 主节点租约。
    单 worker 模式保持原有的文件锁语义。
    """
    multi_worker = redis_gateway.using_redis()
    if multi_worker:
        # 多 worker 依赖 Redis 共享状态：启动自检失败直接阻断，避免带病运行
        try:
            await asyncio.to_thread(redis_gateway.ping)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("多 worker 模式需要可用的 Redis，请检查 REDIS_URL") from exc
        logger.info(f"多 worker 模式：Redis 后端，worker 数={settings.api.query_workers}")
    else:
        logger.info("单 worker 模式：未配置 Redis，使用进程内状态")

    lease = None
    if not multi_worker:
        # 启动阶段：抢占单 worker 租约，保证调度器只有一个实例在跑。
        lease = SingleWorkerLease()
        lease.acquire()
    try:
        # 确保数据库索引/集合结构就绪，后续读写才有正确约束。
        ensure_indexes()
        # 接收请求前先加载模型：否则第一个查询会把 60 秒预算耗在冷启动上
        await asyncio.to_thread(warmup_models)
        _app.state.evaluation = await asyncio.to_thread(
            health_router.query_provenance, "redis" if multi_worker else "memory",
        )
        scheduler_task = None
        # 仅当允许写入且调度器开关开启时才启动后台循环（只读/导入进程不启动）。
        if writes_enabled() and settings.evolution.scheduler_enabled:
            interval = settings.evolution.schedule_interval_minutes * 60.0
            if multi_worker:
                # 多 worker：只有一个实例能拿到主节点租约，避免重复跑指标与回测
                scheduler_task = asyncio.create_task(scheduler_leader_loop(interval))
                logger.info(
                    f"自进化调度器已启动（主节点模式）：interval={settings.evolution.schedule_interval_minutes}min"
                )
            else:
                scheduler_task = asyncio.create_task(evolution_scheduler_loop(interval))
                logger.info(
                    f"自进化调度器已启动：interval={settings.evolution.schedule_interval_minutes}min, "
                    f"batch={settings.evolution.scan_batch}"
                )
        try:
            yield
        finally:
            # 关闭阶段：取消调度任务并等待其安全退出（吞掉 CancelledError）。
            if scheduler_task is not None:
                scheduler_task.cancel()
                try:
                    await scheduler_task
                except asyncio.CancelledError:
                    pass
    finally:
        # 释放单 worker 租约，允许其他实例接管。
        if lease is not None:
            lease.release()



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
install_session_cookie(app)


@app.middleware("http")
async def record_query_arrival(request: Request, call_next):
    if request.method == "POST" and request.url.path == "/api/query":
        request.state.query_received_at = time.perf_counter()
    return await call_next(request)


app.include_router(query_router.router)
app.include_router(evolution_router.router)
app.include_router(health_router.router)
app.include_router(pages_router.query_pages_router)
app.include_router(pages_router.static_router)


if __name__ == "__main__":
    import os

    import uvicorn

    if redis_gateway.using_redis():
        worker_count = settings.api.query_workers
        settings.evaluation.query_worker_count = worker_count
        os.environ["EVAL_QUERY_WORKER_COUNT"] = str(worker_count)
        # 多 worker 必须用导入字符串启动，uvicorn 才能派生多个进程
        uvicorn.run(
            "app.api.http.query_server:app",
            host=settings.api.host,
            port=settings.api.query_port,
            workers=worker_count,
        )
    else:
        uvicorn.run(app, host=settings.api.host, port=settings.api.query_port)
