"""查询服务路由：健康检查、提问、SSE 流、会话历史。"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import StreamingResponse

from app.api.errors import ApiError
from app.api.schema.query_schema import (
    HistoryClearResponseSchema,
    HistoryItemResponseSchema,
    HistoryListResponseSchema,
    QueryNotStreamResponseSchema,
    QueryRequestSchema,
    QueryStreamResponseSchema,
)
from app.rag.query.citations import build_citations
from app.rag.query.pipeline import invoke_query_graph
from app.shared.clients.history_repository import history_repository
from app.shared.runtime.logger import logger
from app.shared.utils.sse_broker import stream_events
from app.shared.utils.task_state import get_done_task_list

router = APIRouter(prefix="/api", tags=["query"])


@router.post("/query", response_model=QueryStreamResponseSchema | QueryNotStreamResponseSchema)
def query(background_tasks: BackgroundTasks, query_params: QueryRequestSchema):
    """提交提问。

    ``is_stream=true`` 时立即返回 ``session_id`` 并在后台执行，前端随后连接
    ``GET /api/stream/{session_id}`` 接收流式事件；否则同步等待结果。
    """
    session_id = query_params.session_id or str(uuid.uuid4())
    is_stream = query_params.is_stream

    if is_stream:
        background_tasks.add_task(
            invoke_query_graph,
            session_id=session_id,
            original_query=query_params.query,
            is_stream=is_stream,
        )
        return QueryStreamResponseSchema(message=f"已开始查询:{query_params.query}", session_id=session_id)

    state = invoke_query_graph(
        session_id=session_id,
        original_query=query_params.query,
        is_stream=is_stream,
    )
    if state is None:
        raise ApiError("query_failed", "查询流程执行失败", status_code=500)
    return QueryNotStreamResponseSchema(
        message=f"已完成{query_params.query}的内容检索",
        session_id=session_id,
        answer=state.get("answer"),
        done_list=get_done_task_list(session_id),
        image_urls=state.get("image_urls", []),
        item_names=state.get("item_names", []),
        citations=state.get("citations") or build_citations(
            state.get("cited_chunk_ids"), state.get("faq_evo_ids")
        ),
        groundedness=state.get("groundedness", 0.0),
        retrieval_signals=state.get("retrieval_signals", {}),
        item_name_options=state.get("item_name_options", []),
    )


@router.get("/stream/{session_id}")
def stream(session_id: str, request: Request) -> StreamingResponse:
    """SSE 事件流：ready → progress → delta → final → close。"""
    return StreamingResponse(stream_events(session_id, request), media_type="text/event-stream")


@router.get("/history/{session_id}", response_model=HistoryListResponseSchema)
def get_history(session_id: str, limit: int = 10) -> HistoryListResponseSchema:
    """读取会话历史（新 → 旧，最多 ``limit`` 条）。"""
    history_list = history_repository.list_recent(session_id=session_id, limit=limit)
    return HistoryListResponseSchema(
        session_id=session_id,
        items=[
            HistoryItemResponseSchema(
                id=str(item.get("_id")),
                session_id=session_id,
                role=item.get("role"),
                text=item.get("text"),
                rewritten_query=item.get("rewritten_query"),
                item_names=item.get("item_names", []),
                image_urls=item.get("image_urls", []),
                citations=item.get("citations", []),
                groundedness=item.get("groundedness", 0.0),
                ts=item.get("ts"),
            )
            for item in history_list
        ],
    )


@router.delete("/history/{session_id}", response_model=HistoryClearResponseSchema)
def clear_history(session_id: str) -> HistoryClearResponseSchema:
    """清空指定会话的历史记录。"""
    deleted_count = history_repository.clear_session(session_id=session_id)
    logger.info(f"会话 {session_id} 历史已清空，共 {deleted_count} 条")
    return HistoryClearResponseSchema(
        message=f"session_id:{session_id} 历史聊天记录已经清空",
        deleted_count=deleted_count,
    )
