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
from app.rag.query.citations import build_citations, build_doc_meta, split_cited
from app.rag.query.history_utils import build_history_context
from app.shared.clients.history_repository import history_repository
from app.shared.config import settings
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger
from app.shared.runtime.prompts import load_prompt
from app.shared.utils.answer import is_no_answer
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
    """组装答案生成提示词（问题 + 上下文 + 主体 + 历史）。

    本地子块会附带所属章节的背景与页码：子块负责精确命中，章节背景补足跨子块的语义连续性
    （见 ``node_parent_expand``）；联网结果与自进化条目没有父块，字段自动省略。
    """
    context = ""
    for index, doc in enumerate(state.get("reranked_docs", []), start=1):
        source = "网络搜索" if doc.get("type") == "web" else "向量库"
        page = doc.get("page")
        page_text = f",页码:{page}" if isinstance(page, int) and page > 0 else ""
        background = doc.get("parent_content")
        background_text = f",章节背景:{background}" if background else ""
        context += (
            f"第{index}部分,标题:{doc.get('title')},来源:{source} ,"
            f"置信度: {doc.get('score')}{page_text}{background_text},内容:{doc.get('text')}\n"
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
    """从重排结果中抽取图片地址（图片类型 URL + Markdown 图片语法）。

    若模型已给出「无法作答」兜底话术，则不再回填图片：切片正文常是「零碎文字 + 配图」
    （如说明书安装步骤），模型据文字判为答不出、图片却会被照常渲染，形成
    「说答不出、界面却给出图」的自相矛盾。兜底话术与图片互斥，二者只保留其一。
    """
    if is_no_answer(state.get("answer")):
        state["image_urls"] = []
        logger.info("命中无法作答兜底话术，已抑制检索图片回填")
        return
    image_urls: list[str] = []
    for doc in state.get("reranked_docs", []):
        url = doc.get("url")
        if url and url.endswith(SUPPORTED_IMAGE_EXTENSIONS):
            image_urls.append(url)
        image_urls.extend(_IMAGE_MARKDOWN_RE.findall(doc.get("text") or ""))
    state["image_urls"] = image_urls


def backfill_evolution_outputs(state: QueryGraphState) -> QueryGraphState:
    """回填引用、检索信号与接地性（仅自进化开启时计算 groundedness）。

    引用覆盖三类来源：知识库切片（kb）、自进化条目（evolution）、联网补充（web）。
    联网引用单独带 ``source="web"``，前端标「联网」，避免「靠联网答出来却显示无引用」。
    """
    reranked_docs = state.get("reranked_docs", [])
    cited, evolution_ids, web_docs = split_cited(reranked_docs)

    # Milvus 数值型主键会被解析成 int，必须统一转 str 再落状态：
    # 否则 FeedbackEvent.cited_chunk_ids(list[str]) 校验失败，自动会话信号被静默丢弃（缺口漏检）
    state["cited_chunk_ids"] = [str(c) for c in cited]
    state["faq_evo_ids"] = [str(c) for c in evolution_ids]
    state["citations"] = build_citations(cited, evolution_ids, web_docs, build_doc_meta(reranked_docs))
    state["retrieval_signals"] = {
        "zero_hit": len(reranked_docs) == 0,
        "no_retrieval": not state.get("embedding_chunks") and not state.get("hyde_embedding_chunks"),
        "evolution_hit": bool(evolution_ids),
        "web_hit": bool(web_docs),
    }
    if settings.evolution.enabled and reranked_docs:
        evidence = [str(d.get("text") or "") for d in reranked_docs]
        state["groundedness"] = compute_groundedness(state.get("answer", ""), evidence)
    else:
        # 未开启自进化时不做接地性评估 → None（前端显示「未评估」，不误报 0%）
        state["groundedness"] = None
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
