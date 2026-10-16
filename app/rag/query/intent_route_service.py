"""意图路由服务（查询链路首节点）。

职责：判定本轮提问是「知识库问答(rag)」还是「闲聊(chitchat)」，把结论写进
``state["intent"]``，由图的条件边决定走检索链路还是闲聊作答分支。

三条设计约束：
1. 保守边界——拿不准、识别失败一律按 rag：误判闲聊会让用户得不到答案，
   误判 rag 只是多走一次检索；
2. 失败不死链路——LLM 异常 / 脏输出全部降级为 rag，绝不升级成 500；
3. 开关关闭时零外部调用——``CHITCHAT_ENABLED=false`` 时纯透传，检索与
   作答行为逐字节等价。
"""
from __future__ import annotations

from langchain_core.messages import HumanMessage

from app.process.query.agent.state import QueryGraphState
from app.rag.query.config import CHITCHAT_ENABLED
from app.rag.query.history_utils import build_chat_history_context
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger, step_log
from app.shared.runtime.prompts import load_prompt
from app.shared.utils.json_utils import parse_json_object
from app.shared.utils.require import require_state_str

INTENT_RAG = "rag"
INTENT_CHITCHAT = "chitchat"

# 意图分类的历史上下文条数：与 answer_service 的历史口径一致
_INTENT_HISTORY_LIMIT = 6


@step_log("call_llm_classify_intent")
def call_llm_classify_intent(history_text: str, original_query: str) -> str:
    """调用 LLM 做 rag/chitchat 二分类；任何异常或脏输出都兜底为 rag。

    兜底口径：LLM 抛异常、返回非法 JSON、JSON 非对象、``intent`` 取值不在
    枚举内、字段缺失或为空——一律 ``INTENT_RAG``。意图识别是分流开关，
    不是业务步骤，不得把原链路打死。
    """
    try:
        llm_client = llm_providers.chat(json_mode=True)
        prompt_text = load_prompt(
            "intent_classify", query=original_query, history_text=history_text
        )
        response = llm_client.invoke([HumanMessage(content=prompt_text)])
        result_dict = parse_json_object(response.content)
        intent = str(result_dict.get("intent") or "").strip().lower()
    except Exception as exc:  # noqa: BLE001 - 分类失败必须降级，不能升级成 500
        logger.warning(f"意图识别失败，按知识库问答处理：{exc}")
        return INTENT_RAG
    if intent not in (INTENT_RAG, INTENT_CHITCHAT):
        logger.warning(f"意图识别输出不在枚举内，按知识库问答处理：intent={intent!r}")
        return INTENT_RAG
    return intent


@step_log("route_intent")
def route_intent(state: QueryGraphState) -> QueryGraphState:
    """意图路由服务入口：回填 ``state["intent"]``（rag / chitchat）。

    短路优先级从高到低：
    1. ``CHITCHAT_ENABLED=false``——开关关闭，零外部调用，恒为 rag；
    2. ``rewrite_hint`` 非空——点选主体后的重问一定是知识诉求，确定性回填 rag
       （与 ``resolve_by_hint`` 同一哲学：结构化直传比模型推断可靠）；
    3. 常规流程——历史上下文 + LLM 二分类，失败兜底 rag。
    """
    if not CHITCHAT_ENABLED:
        state["intent"] = INTENT_RAG
        return state

    if (state.get("rewrite_hint") or "").strip():
        state["intent"] = INTENT_RAG
        return state

    session_id = require_state_str(state, "session_id")
    original_query = require_state_str(state, "original_query")
    history_text = build_chat_history_context(session_id, limit=_INTENT_HISTORY_LIMIT)
    state["intent"] = call_llm_classify_intent(history_text, original_query)
    logger.info(
        f"意图识别完成：session_id={session_id}, intent={state['intent']}, "
        f"query={original_query[:50]}"
    )
    return state
