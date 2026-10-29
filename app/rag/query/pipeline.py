"""查询链路编排：执行 LangGraph 查询图，维护任务进度并推送 SSE 事件。"""
from __future__ import annotations
import time

from app.evolution.feedback.collector import flush_session_signals
from app.evolution.issues import register_unresolved
from app.process.query.agent.main_graph import query_app
from app.process.query.agent.state import QueryGraphState, create_query_default_state
from app.rag.query.answer_payload import answer_fields
from app.rag.query.errors import classify_query_error
from app.rag.query.lifecycle import reserve_query, release_query
from app.shared.config import settings
from app.shared.clients.history_repository import history_repository
from app.shared.runtime.logger import logger
from app.shared.runtime.query_metrics import QueryMetrics, current_metrics, record_query
from app.rag.agentic.deadline import bounded_query
from app.shared.utils.sse_broker import SSEEvent, publish
from app.shared.utils.task_state import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PROCESSING,
    set_task_error,
    update_task_status,
)


@bounded_query
def invoke_query_graph(
    session_id: str,
    original_query: str,
    is_stream: bool,
    rewrite_hint: str | None = None,
    turn_id: str | None = None,
) -> QueryGraphState | None:
    """执行一次查询：清空旧进度 → 跑图 → 推送最终事件 → 写入未解决信号。

    ``rewrite_hint``：点选主体时前端附带的意图原句（见 ``QueryRequestSchema``）。

    通道与收尾事件对非流式同样推送：页面刷新会同时丢掉 POST 响应与 SSE 连接，
    只有通道里留有本轮事件，前端才能在刷新后重新接管这一轮问答。
    """
    turn_id = turn_id or reserve_query(session_id, original_query)
    metrics = QueryMetrics(session_id, turn_id)
    metrics_token = current_metrics.set(metrics)
    from app.rag.agentic.budget import selected_mode
    from app.shared.runtime.deadline import query_deadline
    use_agentic = selected_mode(session_id, mode=settings.agentic.mode,
                                percent=settings.agentic.gray_percent) == 'agentic'
    deadline_token = query_deadline.set((query_deadline.get() or time.monotonic() + settings.agentic.timeout_seconds) if use_agentic else None)
    final_state = {}
    final_status = "failed"
    try:
        update_task_status(session_id, TASK_STATUS_PROCESSING, push_queue=is_stream)

        query_state = create_query_default_state(
            session_id=session_id,
            turn_id=turn_id,
            original_query=original_query,
            is_stream=is_stream,
            rewrite_hint=rewrite_hint or "",
        )
        result_state = query_app.invoke(query_state)
        result_state["issue_id"] = register_unresolved(result_state)
        if result_state["issue_id"] and result_state.get("answer_id"):
            history_repository.attach_issue(session_id, result_state["answer_id"], result_state["issue_id"], bool(result_state.get("handoff_required")))
        final_state = result_state
        final_status = "completed"
        result_state["metrics"] = metrics.document(result_state.get("intent", "rag"), "completed")
        if result_state.get('agentic_metrics'):
            result_state['metrics']['agentic'] = result_state['agentic_metrics']
        update_task_status(session_id, TASK_STATUS_COMPLETED, push_queue=is_stream)

        # 收尾事件不分流式与否：非流式页面刷新后同样靠通道里的 final 续上答案
        publish(session_id, SSEEvent.FINAL, {
            **answer_fields(result_state),
            "turn_id": turn_id,
            "status": "completed",
        })
        publish(session_id, SSEEvent.CLOSE, {})

        # 自进化：会话未解决信号异步落库（开启时；异常不影响主链路）
        # 闲聊轮显式排除：闲聊无检索过程，其回答不代表知识缺口；
        # 双保险是 chitchat_answer.prompt 已禁输出「无法作答」类兜底话术，
        # 防止 is_no_answer 误命中把闲聊记进 fb_events。
        from app.evolution.flags import writes_enabled
        if writes_enabled() and (result_state or {}).get("intent") != "chitchat":
            try:
                flush_session_signals(result_state)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"写入会话信号失败（忽略）：{exc}")
        return result_state
    except Exception as exc:  # noqa: BLE001 - 分类后转失败状态 + SSE 错误事件
        code, message = classify_query_error(exc)
        update_task_status(session_id, TASK_STATUS_FAILED, push_queue=is_stream)
        set_task_error(session_id, code, message)
        publish(session_id, SSEEvent.ERROR, {
            "code": code,
            "message": f"{session_id} 业务失败，原因：{message}",
        })
        logger.exception(f"执行查询流程报错（{code}）：{message}")
        return None

    finally:
        try:
            record_query(metrics, final_state or {}, final_status, original_query)
        finally:
            current_metrics.reset(metrics_token)
            query_deadline.reset(deadline_token)
            release_query(session_id, turn_id)
