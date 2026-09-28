"""候选生成：用 LLM 从缺口会话与命中切片提炼候选 Q/A，做 PII 拦截后写入 ``k_candidates``。"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from app.evolution.candidate.pii import sanitize_candidate
from app.evolution.models import KnowledgeCandidate
from app.evolution.quality import looks_like_non_answer
from app.evolution.repositories import evolution_repo
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger
from app.shared.utils.json_utils import parse_json_object

_PROMPT = (
    "你是企业知识库的候选知识点提炼器。根据‘用户问题’和‘参考片段’，提炼一条标准 FAQ。\n"
    "只输出 JSON，禁止多余文字，格式：{{\"faq_question\":\"\",\"faq_answer\":\"\",\"source_refs\":[\"\"]}}。\n"
    "要求：答案忠实于参考片段与事实，不臆造；长度 60~300 字；faq_question 为该问题的规范化表述。\n"
    "用户问题：{question}\n参考片段：\n{context}\n"
)
_MAX_RETRY = 2
_CONTEXT_CHAR_LIMIT = 3000


def _call_llm(question: str, context: str) -> dict[str, Any]:
    """调用模型提炼候选（JSON 模式 + 统一 JSON 解析）。"""
    prompt = _PROMPT.format(question=question, context=context[:_CONTEXT_CHAR_LIMIT])
    response = llm_providers.chat(json_mode=True).invoke([HumanMessage(content=prompt)])
    return parse_json_object(response.content)


def _exists_question(question: str) -> bool:
    """按问题文本去重。"""
    try:
        return evolution_repo.k_candidates.find_one({"faq_question": question}) is not None
    except Exception:  # noqa: BLE001 - 去重查询失败按「不存在」处理
        return False


def _store_need_info(question: str, item_names: list[str], reason: str) -> KnowledgeCandidate | None:
    """登记「待人工补充」候选：不臆造答案，等管理员补充事实后再通过。

    这类候选不可能被误批为知识（``approve`` 会拒绝），也不会进入检索。
    """
    if not question:
        return None
    if _exists_question(question):
        logger.info(f"缺口问题已存在候选，跳过：{question[:40]}")
        return None
    candidate = KnowledgeCandidate(
        faq_question=question,
        faq_answer="",
        source_refs=[],
        item_names=list(item_names or []),
        status="need_info",
        reason=reason,
    )
    evolution_repo.k_candidates.insert_one(candidate.document())
    logger.info(f"候选登记为待人工补充：{question[:40]}（{reason}）")
    return candidate


def generate_candidate(gap: dict[str, Any], context_docs: list[dict[str, Any]]) -> KnowledgeCandidate | None:
    """基于缺口生成候选。

    - 没有任何检索证据 → 不做「无信息」型 FAQ，直接登记 ``need_info``；
    - 生成的答案疑似「无信息」结论 → 同样登记 ``need_info``（管理员补充后才是 draft）；
    - PII 命中 / 问题重复 → 以 ``rejected`` 落库留痕。
    """
    question = gap.get("query") or gap.get("session_id", "")
    snippet = gap.get("transcript_slice") or question
    context = "\n".join(
        str(doc.get("text") or doc.get("content") or "")[:300] for doc in context_docs[-5:]
    )
    item_names = list(gap.get("item_names") or [])

    # 无证据：不臆造，交人工补充（此前正是这里生成了「未提及…建议联系官方」的伪知识）
    if not context.strip():
        return _store_need_info(question, item_names, "无检索证据，需人工补充事实")

    last_error: Exception | None = None
    for _ in range(_MAX_RETRY):
        try:
            raw = _call_llm(snippet or question, context)
            faq_question = str(raw.get("faq_question", "")).strip()
            faq_answer = str(raw.get("faq_answer", "")).strip()
            if not faq_question or not faq_answer:
                continue

            faq_question, faq_answer, has_pii = sanitize_candidate(faq_question, faq_answer)
            if not has_pii and looks_like_non_answer(faq_answer):
                return _store_need_info(faq_question, item_names, "生成答案未包含事实（疑似“无信息”结论）")
            candidate = KnowledgeCandidate(
                faq_question=faq_question,
                faq_answer=faq_answer,
                source_refs=[str(ref) for ref in (raw.get("source_refs") or [])],
                item_names=item_names,
                status="rejected" if has_pii else "draft",
                reason="PII detected" if has_pii else "",
            )
            if _exists_question(faq_question):
                candidate.status = "rejected"
                candidate.reason = "duplicate"
            evolution_repo.k_candidates.insert_one(candidate.document())
            return candidate
        except Exception as exc:  # noqa: BLE001 - 重试后仍失败则放弃该缺口
            last_error = exc

    if last_error:
        logger.warning(f"候选生成失败：{last_error}")
    return None
