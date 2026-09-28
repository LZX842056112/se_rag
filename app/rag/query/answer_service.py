"""答案生成服务（查询链路末节点）。

职责：组装上下文提示词 → 调用 LLM（流式/非流式）→ 抽取图片 → 回填引用/信号/接地性 →
落库助手消息。
"""
from __future__ import annotations

import re

from langchain_core.messages import HumanMessage

from app.evolution.online_eval.grounding import compute_groundedness
from app.process.query.agent.state import QueryGraphState
from app.rag.config import SUPPORTED_IMAGE_EXTENSIONS
from app.rag.query.citations import build_citations
from app.rag.query.history_utils import build_history_context
from app.shared.clients.history_repository import history_repository
from app.shared.config import settings
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger
from app.shared.runtime.prompts import load_prompt
from app.shared.utils.sse_broker import SSEEvent, publish

_IMAGE_MARKDOWN_RE = re.compile(r"!\[.*?\]\((.*?)\)")


def state_exists_answer(state: QueryGraphState) -> bool:
    """判断 state 中是否已有现成答案（主体未确认时的反问/兜底话术）。

    有则按流式约定推送一次 delta 并短路，避免重复调用模型。
    """
    answer = state.get("answer")
    if not answer:
        logger.debug("answer 为空，进入正常检索作答流程")
        return False
    if state.get("is_stream", False):
        publish(state.get("session_id"), SSEEvent.DELTA, {"delta": answer})
    logger.info("answer 已存在（主体未确认/兜底话术），直接返回")
    return True


def load_answer_prompt(state: QueryGraphState) -> str:
    """组装答案生成提示词（问题 + 上下文 + 主体 + 历史）。"""
    context = ""
    for index, doc in enumerate(state.get("reranked_docs", []), start=1):
        source = "网络搜索" if doc.get("type") == "web" else "向量库"
        context += (
            f"第{index}部分,标题:{doc.get('title')},来源:{source} ,"
            f"置信度: {doc.get('score')},内容:{doc.get('text')}\n"
        )
    item_names = f"{','.join(state.get('item_names', []))}"
    history_text = build_history_context(state.get("session_id"), limit=6)
    return load_prompt(
        "answer_out",
        question=state.get("rewritten_query"),
        context=context,
        item_names=item_names,
        history=history_text,
    )


def call_llm_deal_answer(state: QueryGraphState, answer_prompt_text: str) -> None:
    """调用 LLM 生成答案；流式模式下逐段推送到 SSE。"""
    session_id = state.get("session_id")
    llm_client = llm_providers.chat()
    messages = [HumanMessage(content=answer_prompt_text)]
    if state.get("is_stream", False):
        answer = ""
        for chunk in llm_client.stream(messages):
            if chunk.content:
                publish(session_id, SSEEvent.DELTA, {"delta": chunk.content})
                answer += chunk.content
        state["answer"] = answer
    else:
        state["answer"] = llm_client.invoke(messages).content


def extract_text_image_url(state: QueryGraphState) -> None:
    """从重排结果中抽取图片地址（图片类型 URL + Markdown 图片语法）。"""
    image_urls: list[str] = []
    for doc in state.get("reranked_docs", []):
        url = doc.get("url")
        if url and url.endswith(SUPPORTED_IMAGE_EXTENSIONS):
            image_urls.append(url)
        image_urls.extend(_IMAGE_MARKDOWN_RE.findall(doc.get("text") or ""))
    state["image_urls"] = image_urls


def backfill_evolution_outputs(state: QueryGraphState) -> QueryGraphState:
    """回填引用、检索信号与接地性（仅自进化开启时计算 groundedness）。"""
    reranked_docs = state.get("reranked_docs", [])
    cited: list = []
    evolution_ids: list = []
    for doc in reranked_docs:
        chunk_id = doc.get("chunk_id")
        if doc.get("type") == "web":
            continue
        if chunk_id is not None:
            cited.append(chunk_id)
            if doc.get("source") == "evolution":
                evolution_ids.append(chunk_id)

    state["cited_chunk_ids"] = cited
    state["faq_evo_ids"] = evolution_ids
    state["citations"] = build_citations(cited, evolution_ids)
    state["retrieval_signals"] = {
        "zero_hit": len(reranked_docs) == 0,
        "no_retrieval": not state.get("embedding_chunks") and not state.get("hyde_embedding_chunks"),
        "evolution_hit": bool(evolution_ids),
    }
    if settings.evolution.enabled and reranked_docs:
        evidence = [str(d.get("text") or "") for d in reranked_docs]
        state["groundedness"] = compute_groundedness(state.get("answer", ""), evidence)
    else:
        state["groundedness"] = 0.0
    return state


def save_answer_message_history(state: QueryGraphState) -> None:
    """把助手回答写入会话历史（须在回填之后，保证 citations/groundedness 可持久化）。"""
    history_repository.save_message(
        session_id=state.get("session_id"),
        role="assistant",
        text=state.get("answer"),
        rewritten_query=state.get("rewritten_query"),
        item_names=state.get("item_names", []),
        image_urls=state.get("image_urls", []),
        citations=state.get("citations", []),
        groundedness=state.get("groundedness", 0.0),
    )


def generate_answer(state: QueryGraphState) -> QueryGraphState:
    """答案生成服务入口。"""
    if not state_exists_answer(state):
        answer_prompt_text = load_answer_prompt(state)
        call_llm_deal_answer(state, answer_prompt_text)
        extract_text_image_url(state)
    backfill_evolution_outputs(state)
    save_answer_message_history(state)
    return state
