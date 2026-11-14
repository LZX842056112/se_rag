"""答案生成服务（查询链路末节点）。

职责：组装上下文提示词 → 调用 LLM（流式/非流式）→ 校验答案配图 → 回填引用/信号/接地性 →
落库助手消息。
"""
from __future__ import annotations

from collections.abc import Iterable, Iterator
import re

from langchain_core.messages import HumanMessage

from app.evolution.online_eval.grounding import compute_groundedness
from app.process.query.agent.state import QueryGraphState
from app.rag.query.answer_images import select_answer_images
from app.rag.query.citations import build_citations, build_doc_meta, split_cited
from app.rag.query.history_utils import build_history_context
from app.rag.query.business import apply_business_policy
from app.shared.clients.history_repository import history_repository
from app.shared.config import settings
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger
from app.shared.runtime.prompts import load_prompt
from app.shared.runtime.query_metrics import measure_stage
from app.shared.utils.answer import NO_ANSWER_REPLY, is_no_answer
from app.shared.utils.sse_broker import SSEEvent, publish


_GENERAL_LIMITATION = r"(?:某些|部分)(?:特性和)?功能(?:可能)?(?:无法|不能)实现"
_LIMITATION_QUESTION = re.compile(
    rf"{_GENERAL_LIMITATION}的?原因(?:可能)?(?:是|有)?(?:哪些|什么)[？?。\s]*$"
)
_LIMITATION_PASSAGE = re.compile(
    r"(?:某些|部分)(?:特性和)?功能可能由于[^。！？\n]+(?:无法|不能)实现"
)


def _select_feature_limitation_evidence(state: QueryGraphState) -> None:
    """通用原因已有直接说明时，只把适用手册的说明送给模型。"""
    if state.get("query_mode") != "legacy" or state.get("business_tag") != "product_knowledge":
        return
    if len(state.get("item_names") or []) != 1:
        return
    question = state.get("rewrite_hint") or state.get("original_query") or ""
    if not _LIMITATION_QUESTION.search(question) or re.search(r"[？?。；;].+[^？?。；;\s]", question):
        return
    direct = [
        dict(doc, parent_content=None)
        for doc in state["reranked_docs"]
        if doc.get("source") == "milvus" and doc.get("document_kind") in (None, "", "manual")
        and _LIMITATION_PASSAGE.search(doc.get("text") or "")
    ]
    if direct:
        state["reranked_docs"] = direct


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
    contexts: list[str] = []
    for index, doc in enumerate(state.get("reranked_docs", []), start=1):
        source = "网络搜索" if doc.get("type") == "web" else "向量库"
        page = doc.get("page")
        page_text = f",页码:{page}" if isinstance(page, int) and page > 0 else ""
        heading = doc.get("heading_path")
        heading_text = f",章节路径:{heading}" if heading else ""
        background = None if state.get("business_tag") in ("pricing", "after_sales") else doc.get("parent_content")
        background_text = f",章节背景:{background}" if background else ""
        score = doc.get("score")
        score_text = f",置信度: {score}" if score is not None else ""
        contexts.append(
            f"第{index}部分,标题:{doc.get('title')},来源:{source}{score_text}"
            f"{page_text}{heading_text}{background_text},内容:{doc.get('text')}\n"
        )
    state["answer_contexts"] = contexts
    from app.rag.agentic.evidence import doc_to_evidence
    import hashlib
    manifest = []
    for doc, context in zip(state.get('reranked_docs', []), contexts):
        actual = dict(doc)
        if state.get('business_tag') in ('pricing', 'after_sales'):
            actual.pop('parent_content', None)
        ref = doc_to_evidence(actual).model_dump()
        ref['context_sha256'] = hashlib.sha256(context.encode()).hexdigest()
        manifest.append(ref)
    state['evidence_manifest'] = manifest
    item_names = f"{','.join(state.get('item_names', []))}"
    history_text = ("本轮只使用当前有效的业务政策，不使用历史回答中的价格或承诺。"
                    if state.get("business_tag") in ("pricing", "after_sales")
                    else build_history_context(state.get("session_id"), limit=6))
    rewritten = state.get("rewritten_query") or ""
    original = state.get("rewrite_hint") or state.get("original_query") or rewritten
    question = original if not rewritten or original == rewritten else f"{original}\n检索改写：{rewritten}"
    return load_prompt(
        "answer_out",
        question=question,
        context="".join(contexts),
        item_names=item_names,
        history=history_text,
        no_answer_reply=NO_ANSWER_REPLY,
    )


def _answer_chunks(contents: Iterable[str]) -> Iterator[str]:
    """只缓冲可能组成固定拒答句的后缀，避免已作答后又发出拒答。"""
    pending = ""
    leading_text = ""
    finished = False
    for content in contents:
        if finished or not content:
            continue
        pending += content
        before, marker, _ = pending.partition(NO_ANSWER_REPLY)
        if marker:
            lead = (leading_text + before).strip(" \t\r\n#*_`>，。！？：,.!?:").casefold()
            if lead in {"", "抱歉", "很抱歉", "非常抱歉", "对不起", "回答", "结论", "答复", "answer", "conclusion"}:
                if before:
                    yield before
                yield NO_ANSWER_REPLY + "。请交由负责人核实。"
            elif before.rstrip():
                yield before.rstrip()
            finished = True
            continue
        # 仅保留与拒答句开头相同的尾部字符。其余内容可立即发送。
        overlap = next((size for size in range(min(len(pending), len(NO_ANSWER_REPLY) - 1), 0, -1)
                        if pending.endswith(NO_ANSWER_REPLY[:size])), 0)
        split_at = len(pending) - overlap
        safe, pending = pending[:split_at], pending[split_at:]
        if safe:
            leading_text = (leading_text + safe.strip())[:64]
            yield safe
    if pending and not finished:
        yield pending


def call_llm_deal_answer(state: QueryGraphState, answer_prompt_text: str) -> None:
    """调用 LLM 生成答案；流式模式下逐段推送到 SSE。"""
    session_id = state.get("session_id")
    messages = [HumanMessage(content=answer_prompt_text)]
    if state.get("is_stream", False):
        answer = []
        for delta in _answer_chunks(llm_providers.stream_chat_text(messages)):
            publish(session_id, SSEEvent.DELTA, {"delta": delta})
            answer.append(delta)
        state["answer"] = "".join(answer)
    else:
        state["answer"] = "".join(_answer_chunks([llm_providers.chat().invoke(messages).content]))


def extract_text_image_url(state: QueryGraphState) -> None:
    """只保留答案明确选用、且存在于实际生成上下文的图片。"""
    select_answer_images(state)


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
        "no_retrieval": not any(state.get(key) for key in ("embedding_chunks", "hyde_embedding_chunks", "keyword_chunks")),
        "evolution_hit": bool(evolution_ids),
        "web_hit": bool(web_docs),
    }
    if state.get('query_mode') == 'agentic':
        state['retrieval_signals']['no_retrieval'] = not any(
            step.get('evidence_ids') for step in state.get('agentic_trace', []) if step.get('tool'))
    if state.get('agentic_verified'):
        pass  # 有界控制器已校验最终的草稿。
    elif settings.evolution.enabled and reranked_docs:
        evidence = state.get('answer_contexts') or [str(d.get("text") or "") for d in reranked_docs]
        state["groundedness"] = compute_groundedness(state.get("answer", ""), evidence)
    else:
        # 未开启自进化时不做接地性评估 → None（前端显示「未评估」，不误报 0%）
        state["groundedness"] = None
    return state


def save_answer_message_history(state: QueryGraphState) -> None:
    """把助手回答写入会话历史（须在回填之后，保证 citations/groundedness 可持久化）。"""
    # 只有「给出了可点选主体」的反问轮才记 pending（item_name_options 非空 ⟺ 反问轮）；
    # 正常作答与「没有关联到任何主体」兜底话术都不记。用 .get 兼容缺键的旧 state。
    options = state.get("item_name_options") or []
    pending_subject_ask = {
        "intent": state.get("original_query") or "",
        "options": [
            str(item.get("item_name"))
            for item in options
            if isinstance(item, dict) and item.get("item_name")
        ],
    } if options else {}
    state["answer_id"] = history_repository.save_message(
        session_id=state.get("session_id"),
        role="assistant",
        text=state.get("answer"),
        rewritten_query=state.get("rewritten_query"),
        item_names=state.get("item_names", []),
        image_urls=state.get("image_urls", []),
        citations=state.get("citations", []),
        groundedness=state.get("groundedness", 0.0),
        pending_subject_ask=pending_subject_ask,
        intent="rag",
        turn_id=state.get("turn_id", ""),
        query=state.get("original_query", ""),
        business_tag=state.get("business_tag", ""),
    )


def generate_answer(state: QueryGraphState) -> QueryGraphState:
    """答案生成服务入口。"""
    state["answer_contexts"] = []
    state["image_urls"] = []
    state['evidence_manifest'] = []
    apply_business_policy(state)
    if not state_exists_answer(state):
        _select_feature_limitation_evidence(state)
        answer_prompt_text = load_answer_prompt(state)
        with measure_stage("answer_model"):
            call_llm_deal_answer(state, answer_prompt_text)
        if is_no_answer(state.get("answer")):
            state["handoff_required"] = True
            state["issue_type"] = "missing_material"
    elif not state.get("handoff_required"):
        state["query_outcome"] = "clarification"
    extract_text_image_url(state)
    backfill_evolution_outputs(state)
    save_answer_message_history(state)
    return state
