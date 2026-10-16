"""查询链路编排：执行 LangGraph 查询图，维护任务进度并推送 SSE 事件。"""
from __future__ import annotations

from app.evolution.feedback.collector import flush_session_signals
from app.process.query.agent.main_graph import query_app
from app.process.query.agent.state import QueryGraphState, create_query_default_state
from app.rag.query.citations import citations_from_reranked_docs
from app.rag.query.errors import classify_query_error
from app.shared.config import settings
from app.shared.runtime.logger import logger
from app.shared.utils.sse_broker import SSEEvent, ensure_channel, publish, reset_channel
from app.shared.utils.task_state import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PROCESSING,
    clear_task,
    set_task_error,
    set_task_query,
    update_task_status,
)


def invoke_query_graph(
    session_id: str,
    original_query: str,
    is_stream: bool,
    rewrite_hint: str | None = None,
) -> QueryGraphState | None:
    """执行一次查询：清空旧进度 → 跑图 → 推送最终事件 → 写入未解决信号。

    ``rewrite_hint``：点选主体时前端附带的意图原句（见 ``QueryRequestSchema``）。

    通道与收尾事件对非流式同样推送：页面刷新会同时丢掉 POST 响应与 SSE 连接，
    只有通道里留有本轮事件，前端才能在刷新后重新接管这一轮问答。
    """
    try:
        clear_task(session_id)
        # 记录本轮提问：页面刷新后用户消息可能还没落库，前端用快照补出提问气泡
        set_task_query(session_id, original_query)
        # 先清掉上一轮残留的缓冲事件，再确保通道存在
        reset_channel(session_id)
        ensure_channel(session_id)
        update_task_status(session_id, TASK_STATUS_PROCESSING, push_queue=is_stream)

        query_state = create_query_default_state(
            session_id=session_id,
            original_query=original_query,
            is_stream=is_stream,
            rewrite_hint=rewrite_hint or "",
        )
        result_state = query_app.invoke(query_state)
        update_task_status(session_id, TASK_STATUS_COMPLETED, push_queue=is_stream)

        # 收尾事件不分流式与否：非流式页面刷新后同样靠通道里的 final 续上答案
        publish(session_id, SSEEvent.FINAL, {
            "answer": result_state.get("answer"),
            "status": "completed",
            "image_urls": result_state.get("image_urls", []),
            # 回传已识别主体：前端点踩时原样回传，保证 反馈→缺口→候选 的 item_names 贯通
            "item_names": result_state.get("item_names", []),
            "citations": result_state.get("citations")
            or citations_from_reranked_docs(result_state.get("reranked_docs")),
            "groundedness": result_state.get("groundedness"),
            "item_name_options": result_state.get("item_name_options", []),
            # 本轮意图（rag / chitchat）：前端据此给闲聊回答渲染「闲聊」徽标；
            # 缺键的旧 state 按 rag 处理
            "intent": result_state.get("intent") or "rag",
        })
        publish(session_id, SSEEvent.CLOSE, {})

        # 自进化：会话未解决信号异步落库（开启时；异常不影响主链路）
        # 闲聊轮显式排除：闲聊无检索过程，其回答不代表知识缺口；
        # 双保险是 chitchat_answer.prompt 已禁输出「无法作答」类兜底话术，
        # 防止 is_no_answer 误命中把闲聊记进 fb_events。
        if settings.evolution.enabled and (result_state or {}).get("intent") != "chitchat":
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
