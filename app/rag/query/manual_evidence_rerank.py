"""按问题的全部要点，对同一产品的手册候选做整组排序。"""
from __future__ import annotations

from collections import Counter
import json
import re

from langchain_core.messages import HumanMessage

from app.rag.query.chunk_search import SEARCH_TOP_K
from app.rag.query.config import LEGACY_ANSWER_RESERVE_SECONDS
from app.rag.query.eval_evidence import chunk_ids
from app.rag.query.manual_keyword_search import KEYWORD_CANDIDATE_LIMIT, matches_requested_heading
from app.shared.models import llm_providers
from app.shared.runtime.deadline import bounded_timeout
from app.shared.runtime.logger import logger
from app.shared.runtime.prompts import load_prompt
from app.shared.runtime.query_metrics import mark_degraded, measure_stage
from app.shared.utils.json_utils import parse_json_object


_MAX_CANDIDATES = 2 * SEARCH_TOP_K + KEYWORD_CANDIDATE_LIMIT
_MAX_PASSAGE_CHARS = 1600
_LINK = re.compile(r"!?\[([^\]]*)\]\(https?://[^)]*\)")
_PROCEDURE = re.compile(r"怎么|怎样|如何|步骤|操作|配置|设置")
_NOTE_HEADINGS = {"注释", "备注", "注意", "警告"}


def _page_key(doc: dict) -> tuple | None:
    page = doc.get("page")
    doc_id = doc.get("doc_id")
    version = doc.get("document_version")
    if doc_id and version and isinstance(page, int) and page > 0:
        return doc_id, version, page
    return None


def _order_by_manual_page(docs: list[dict]) -> list[dict]:
    pages: dict[tuple, list[dict]] = {}
    for position, doc in enumerate(docs):
        key = _page_key(doc) or (position,)
        pages.setdefault(key, []).append(doc)
    return [doc for group in pages.values() for doc in sorted(group, key=lambda row: row.get("seq") or 0)]


def _section_key(doc: dict, *, parent: bool = False) -> tuple | None:
    page = _page_key(doc)
    section = doc.get("heading_path") or ""
    if parent:
        section = section.rpartition(" / ")[0]
    return (*page, section) if page and section else None


def _fill_procedure_gap(ranked: list[dict], candidates: list[dict]) -> list[dict] | None:
    sections = [_section_key(doc) for doc in ranked]
    for section in dict.fromkeys(sections):
        if section is None or sections.count(section) == len(ranked):
            continue
        steps = {doc["seq"] for doc, key in zip(ranked, sections)
                 if key == section and isinstance(doc.get("seq"), int)}
        for candidate in candidates:
            seq = candidate.get("seq")
            if (not isinstance(seq, int) or seq in steps or seq - 1 not in steps or seq + 1 not in steps
                    or _section_key(candidate) != section):
                continue
            replace = max(index for index, key in enumerate(sections) if key != section)
            selected = list(ranked)
            selected[replace] = dict(candidate, score=None)
            section_docs = iter(sorted((doc for doc in selected if _section_key(doc) == section),
                                       key=lambda doc: doc.get("seq") or 0))
            return [next(section_docs) if _section_key(doc) == section else doc for doc in selected]
    return None


def keep_procedure_context(question: str, ranked: list[dict], candidates: list[dict]) -> list[dict]:
    """填补同页操作步骤之间的缺口，或保留一条紧邻的注释。"""
    if len(ranked) < 3 or not _PROCEDURE.search(question):
        return ranked
    completed = _fill_procedure_gap(ranked, candidates)
    if completed is not None:
        return completed

    cited = {doc["chunk_id"] for doc in ranked}
    sections = Counter(_section_key(doc, parent=True) for doc in ranked)
    last_section = _section_key(ranked[-1], parent=True)
    for anchor in ranked[:-1]:
        section = _section_key(anchor, parent=True)
        seq = anchor.get("seq")
        if section is None or section == last_section or sections[section] < 2 or not isinstance(seq, int):
            continue
        for note in candidates:
            if note["chunk_id"] in cited or note.get("seq") != seq + 1 or _section_key(note, parent=True) != section:
                continue
            title = (note.get("heading_path") or "").rsplit(" / ", 1)[-1]
            if title in _NOTE_HEADINGS:
                return ranked[:-1] + [dict(note, score=None)]
    return ranked


def keep_retrieval_agreement(state: dict, ranked: list[dict], candidates: list[dict], limit: int) -> list[dict]:
    """保留明确指定的章节和多路共同支持的首位候选。"""
    embedding = state.get("embedding_chunks") or []
    keyword = state.get("keyword_chunks") or []
    hyde = state.get("hyde_embedding_chunks") or []
    if not embedding or not keyword:
        return ranked
    keyword_id = keyword[0].get("chunk_id")
    question = state.get("rewrite_hint") or state.get("original_query") or state.get("rewritten_query") or ""
    named = bool(keyword_id and matches_requested_heading(keyword[0], question) and any(
        doc.get("chunk_id") == keyword_id for branch in (embedding, hyde) for doc in branch[:5]
    ))
    agreed_id = keyword_id if named else embedding[0].get("chunk_id")
    if not named and (
        not agreed_id or keyword_id != agreed_id or (hyde and hyde[0].get("chunk_id") != agreed_id)
    ):
        fused = state.get("rrf_chunks") or []
        agreed_id = fused[0].get("chunk_id") if fused else None
        semantics_agree = embedding[0].get("chunk_id") == agreed_id and (
            not hyde or hyde[0].get("chunk_id") == agreed_id
        )
        keyword_depth = 5 if semantics_agree else 3
        if not agreed_id or not all(
            any(doc.get("chunk_id") == agreed_id for doc in branch)
            for branch in (embedding[:3], keyword[:keyword_depth], hyde[:3])
            if branch
        ):
            return ranked
    positions = [index for index, doc in enumerate(ranked) if doc.get("type") != "web"][:limit]
    if not positions:
        return ranked
    existing = next((index for index in positions if ranked[index].get("chunk_id") == agreed_id), None)
    if existing is not None:
        if not named or existing == positions[0]:
            return ranked
        selected = list(ranked)
        selected.insert(positions[0], selected.pop(existing))
        return selected
    agreed = next((doc for doc in candidates if doc.get("chunk_id") == agreed_id), None)
    if agreed is None:
        return ranked
    if ranked[positions[0]].get("score") is None:
        agreed = dict(agreed, score=None)
    replace = positions[-1]
    if named:
        selected = [doc for index, doc in enumerate(ranked)
                    if index != replace and doc.get("chunk_id") != agreed_id]
        selected.insert(positions[0], agreed)
        return selected
    return [agreed if index == replace else doc for index, doc in enumerate(ranked)
            if doc.get("chunk_id") != agreed_id]


def rank_manual_evidence(state: dict, docs: list[dict], limit: int) -> list[dict] | None:
    """返回排序后的证据。超时或序号无效时由调用方选择降级路径。"""
    try:
        timeout = bounded_timeout(8.0, reserve_seconds=LEGACY_ANSWER_RESERVE_SECONDS)
    except TimeoutError:
        mark_degraded("manual_evidence_rerank_budget")
        return None

    docs = _order_by_manual_page(docs[:_MAX_CANDIDATES])
    state["manual_rerank_input_chunk_ids"] = chunk_ids(docs)
    passages = [
        {
            "number": number,
            "section": doc.get("heading_path"),
            "page": doc.get("page"),
            "order_on_page": doc.get("seq"),
            "text": _LINK.sub(r"\1", doc.get("text") or "")[:_MAX_PASSAGE_CHARS],
        }
        for number, doc in enumerate(docs, 1)
    ]
    rewritten = state["rewritten_query"]
    original = state.get("rewrite_hint") or state.get("original_query") or rewritten
    payload = {"question": original, "product": state.get("item_names", []), "passages": passages}
    if original != rewritten:
        payload["rewritten_question"] = rewritten
    prompt = load_prompt("manual_evidence_rerank", limit=limit, payload=json.dumps(payload, ensure_ascii=False))

    try:
        with measure_stage("manual_evidence_rerank"):
            client = llm_providers.chat(json_mode=True, timeout_seconds=timeout, max_retries=0)
            response = client.invoke([HumanMessage(content=prompt)])
            order = parse_json_object(response.content).get("ranks")
            if not isinstance(order, list) or len(order) != limit:
                raise ValueError("排序条数不符合要求")
            if any(type(index) is not int or not 1 <= index <= len(docs) for index in order):
                raise ValueError("排序序号超出候选范围")
            if len(set(order)) != limit:
                raise ValueError("排序序号重复")
    except Exception as exc:
        mark_degraded("manual_evidence_rerank_error")
        logger.warning(f"手册证据排序失败：{type(exc).__name__}")
        return None
    return [dict(docs[index - 1], score=None) for index in order]
