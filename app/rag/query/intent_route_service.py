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

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextvars import copy_context
import threading
import time

from langchain_core.messages import HumanMessage

from app.process.query.agent.state import QueryGraphState
from app.rag.query.config import CHITCHAT_ENABLED
from app.rag.query.history_utils import build_chat_history_context
from app.rag.query.greetings import is_plain_greeting
from app.rag.query.business import classify_business, needs_business_route
from app.shared.models import llm_providers
from app.shared.config import settings
from app.shared.runtime.query_metrics import current_metrics, mark_degraded
from app.shared.runtime.logger import logger, step_log
from app.shared.runtime.prompts import load_prompt
from app.shared.utils.json_utils import parse_json_object
from app.shared.utils.require import require_state_str

INTENT_RAG = "rag"
INTENT_CHITCHAT = "chitchat"

# 意图分类的历史上下文条数：与 answer_service 的历史口径一致
_INTENT_HISTORY_LIMIT = 6
INTENT_TIMEOUT_SECONDS = settings.runtime.intent_timeout_seconds
_INTENT_POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="intent")
_INTENT_SLOTS = threading.BoundedSemaphore(4)


@step_log("call_llm_classify_intent")
def call_llm_classify_intent(history_text: str, original_query: str) -> str:
    """调用 LLM 做 rag/chitchat 二分类；任何异常或脏输出都兜底为 rag。

    兜底口径：LLM 抛异常、返回非法 JSON、JSON 非对象、``intent`` 取值不在
    枚举内、字段缺失或为空——一律 ``INTENT_RAG``。意图识别是分流开关，
    不是业务步骤，不得把原链路打死。
    """
    try:
        llm_client = llm_providers.chat(json_mode=True, timeout_seconds=INTENT_TIMEOUT_SECONDS, max_retries=0)
        prompt_text = load_prompt(
            "intent_classify", query=original_query, history_text=history_text
        )
        if not _INTENT_SLOTS.acquire(blocking=False):
            mark_degraded("intent_busy")
            return INTENT_RAG
        context = copy_context()
        def invoke():
            try:
                return context.run(llm_client.invoke, [HumanMessage(content=prompt_text)])
            finally:
                _INTENT_SLOTS.release()
        try:
            future = _INTENT_POOL.submit(invoke)
        except BaseException:
            _INTENT_SLOTS.release()
            raise
        try:
            response = future.result(timeout=INTENT_TIMEOUT_SECONDS)
        except FutureTimeout:
            mark_degraded("intent_timeout")
            return INTENT_RAG
        result_dict = parse_json_object(response.content)
        intent = str(result_dict.get("intent") or "").strip().lower()
    except Exception as exc:  # noqa: BLE001 - 分类失败必须降级，不能升级成 500
        mark_degraded("intent_error")
        logger.warning(f"意图识别失败，按知识库问答处理：{exc}")
        return INTENT_RAG
    if intent not in (INTENT_RAG, INTENT_CHITCHAT):
        mark_degraded("intent_invalid_output")
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
    3. 纯寒暄——完整输入命中确定性规则，不依赖历史或分类模型；
    4. 常规流程——历史上下文 + LLM 二分类，失败兜底 rag。
    """
    state["business_tag"] = classify_business(state.get("original_query") or "")
    if not CHITCHAT_ENABLED:
        state["intent"] = INTENT_RAG
        return state

    if (state.get("rewrite_hint") or "").strip() or needs_business_route(state.get("original_query") or ""):
        state["intent"] = INTENT_RAG
        return state

    session_id = require_state_str(state, "session_id")
    original_query = require_state_str(state, "original_query")
    if is_plain_greeting(original_query):
        state["intent"] = INTENT_CHITCHAT
        logger.info(f"纯寒暄直接进入闲聊：session_id={session_id}")
        return state
    history_text = build_chat_history_context(session_id, limit=_INTENT_HISTORY_LIMIT)
    started = time.perf_counter()
    state["intent"] = call_llm_classify_intent(history_text, original_query)
    metrics = current_metrics.get()
    if metrics:
        metrics.classification_ms = (time.perf_counter() - started) * 1000
    logger.info(
        f"意图识别完成：session_id={session_id}, intent={state['intent']}, "
        f"query={original_query[:50]}"
    )
    return state
