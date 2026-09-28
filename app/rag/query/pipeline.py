"""查询链路编排：执行 LangGraph 查询图，维护任务进度并推送 SSE 事件。"""
from __future__ import annotations

from app.evolution.feedback.collector import flush_session_signals
from app.process.query.agent.main_graph import query_app
from app.process.query.agent.state import QueryGraphState, create_query_default_state
from app.rag.query.citations import build_citations
from app.shared.config import settings
from app.shared.runtime.logger import logger
from app.shared.utils.sse_broker import SSEEvent, ensure_channel, publish
from app.shared.utils.task_state import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PROCESSING,
    clear_task,
    update_task_status,
)


def invoke_query_graph(session_id: str, original_query: str, is_stream: bool) -> QueryGraphState | None:
    """执行一次查询：清空旧进度 → 跑图 → 推送最终事件 → 写入未解决信号。"""
    try:
        clear_task(session_id)
        if is_stream:
            ensure_channel(session_id)
        update_task_status(session_id, TASK_STATUS_PROCESSING, push_queue=is_stream)

        query_state = create_query_default_state(
            session_id=session_id, original_query=original_query, is_stream=is_stream
        )
        result_state = query_app.invoke(query_state)
        update_task_status(session_id, TASK_STATUS_COMPLETED, push_queue=is_stream)

        if is_stream:
            publish(session_id, SSEEvent.FINAL, {
                "answer": result_state.get("answer"),
                "status": "completed",
                "image_urls": result_state.get("image_urls", []),
                # 回传已识别主体：前端点踩时原样回传，保证 反馈→缺口→候选 的 item_names 贯通
                "item_names": result_state.get("item_names", []),
                "citations": result_state.get("citations") or build_citations(
                    result_state.get("cited_chunk_ids"), result_state.get("faq_evo_ids")
                ),
                "groundedness": result_state.get("groundedness", 0.0),
                "item_name_options": result_state.get("item_name_options", []),
            })
            publish(session_id, SSEEvent.CLOSE, {})

        # 自进化：会话未解决信号异步落库（开启时；异常不影响主链路）
        if settings.evolution.enabled:
            try:
                flush_session_signals(result_state)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"写入会话信号失败（忽略）：{exc}")
        return result_state
    except Exception as exc:  # noqa: BLE001 - 统一转成失败状态 + SSE 错误事件
        update_task_status(session_id, TASK_STATUS_FAILED, push_queue=is_stream)
        publish(session_id, SSEEvent.ERROR, {
            "code": "query_failed",
            "message": f"{session_id} 业务失败，原因：{exc}",
        })
        logger.exception(f"执行查询流程报错：{exc}")
        return None
