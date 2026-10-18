"""闲聊作答服务（查询链路分支末节点）。

职责：意图识别判定为 chitchat 后，用人设提示词生成轻松友好的回复，
流式逐段推送 SSE delta，并把 user + assistant 两条消息写入会话历史。

闲聊分支不经过主体确认与检索：不产出 citations / 检索信号，
``groundedness`` 置 None（前端显示「未评估」）；自进化未解决信号由
``pipeline.py`` 的 intent 守卫显式排除（双保险，见该处注释）。
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage

from app.process.query.agent.state import QueryGraphState
from app.rag.query.history_utils import build_chat_history_context
from app.rag.query.intent_route_service import INTENT_CHITCHAT
from app.shared.clients.history_repository import history_repository
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger, step_log
from app.shared.runtime.query_metrics import mark_degraded
from app.shared.runtime.prompts import load_prompt
from app.shared.utils.sse_broker import SSEEvent, publish

# 闲聊作答的历史上下文条数：与 answer_service 的历史口径一致
_CHITCHAT_HISTORY_LIMIT = 6

# 闲聊 LLM 失败时的兜底话术：闲聊是增强路径，降级不打 500
CHITCHAT_FALLBACK_ANSWER = "哈哈，这一下把我问住了。要不换个话题，或者直接问我产品相关的问题？"


def load_chitchat_prompt(state: QueryGraphState) -> str:
    """组装闲聊作答提示词（人设 + 全量历史 + 用户输入）。"""
    history_text = build_chat_history_context(
        state.get("session_id"), limit=_CHITCHAT_HISTORY_LIMIT
    )
    return load_prompt(
        "chitchat_answer",
        query=state.get("original_query"),
        history_text=history_text,
    )


def call_llm_chitchat(state: QueryGraphState, prompt_text: str) -> None:
    """调用 LLM 生成闲聊回复；流式逐段推送 SSE delta，失败降级为兜底话术。"""
    messages = [HumanMessage(content=prompt_text)]
    try:
        llm_client = llm_providers.chat()
        if state.get("is_stream", False):
            answer = ""
            for chunk in llm_client.stream(messages):
                if chunk.content:
                    publish(state.get("session_id"), SSEEvent.DELTA, {"delta": chunk.content})
                    answer += chunk.content
            state["answer"] = answer or CHITCHAT_FALLBACK_ANSWER
        else:
            state["answer"] = llm_client.invoke(messages).content or CHITCHAT_FALLBACK_ANSWER
    except Exception as exc:  # noqa: BLE001 - 闲聊失败降级，口径同 HyDE/联网
        mark_degraded("chitchat_error")
        logger.warning(f"闲聊作答失败，使用兜底话术：{exc}")
        if not state.get("answer"):
            state["answer"] = CHITCHAT_FALLBACK_ANSWER


def save_chitchat_history(state: QueryGraphState) -> None:
    """保存助手消息。用户消息已在生成前保存。

    ``item_names`` 置空：被 ``list_valid_history`` 过滤，不污染下一轮主体消解；
    ``pending_subject_ask`` 置空：不被 ``find_pending_clarify`` 认领为反问轮。
    """
    state["answer_id"] = history_repository.save_message(
        session_id=state.get("session_id"),
        role="assistant",
        text=state.get("answer"),
        item_names=[],
        image_urls=[],
        citations=[],
        groundedness=None,
        pending_subject_ask={},
        intent=INTENT_CHITCHAT,
        turn_id=state.get("turn_id", ""),
        query=state.get("original_query", ""),
    )


@step_log("generate_chitchat_answer")
def generate_chitchat_answer(state: QueryGraphState) -> QueryGraphState:
    """闲聊入口：取旧历史 → 保存提问 → 生成回复 → 保存回答。"""
    prompt_text = load_chitchat_prompt(state)
    history_repository.save_message(
        session_id=state.get("session_id"),
        role="user",
        text=state.get("original_query"),
        item_names=[],
        image_urls=[],
        intent=INTENT_CHITCHAT,
        turn_id=state.get("turn_id", ""),
    )
    call_llm_chitchat(state, prompt_text)
    state["business_tag"] = "chitchat"
    state["intent"] = INTENT_CHITCHAT
    state["citations"] = []
    state["groundedness"] = None  # 前端显示「未评估」
    state["image_urls"] = []
    state["item_names"] = []
    state["rewritten_query"] = ""
    save_chitchat_history(state)
    return state
