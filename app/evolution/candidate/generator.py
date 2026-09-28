"""候选生成：用 LLM 从缺口会话与命中切片提炼候选 Q/A，做 PII 拦截后写入 ``k_candidates``。"""
from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage

from app.evolution.candidate.pii import sanitize_candidate
from app.evolution.models import KnowledgeCandidate
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


def generate_candidate(gap: dict[str, Any], context_docs: list[dict[str, Any]]) -> KnowledgeCandidate | None:
    """基于缺口生成候选；PII 命中或与既有问题重复时以 rejected 落库留痕。"""
    question = gap.get("query") or gap.get("session_id", "")
    snippet = gap.get("transcript_slice") or question
    context = "\n".join(
        str(doc.get("text") or doc.get("content") or "")[:300] for doc in context_docs[-5:]
    )

    last_error: Exception | None = None
    for _ in range(_MAX_RETRY):
        try:
            raw = _call_llm(snippet or question, context)
            faq_question = str(raw.get("faq_question", "")).strip()
            faq_answer = str(raw.get("faq_answer", "")).strip()
            if not faq_question or not faq_answer:
                continue

            faq_question, faq_answer, has_pii = sanitize_candidate(faq_question, faq_answer)
            candidate = KnowledgeCandidate(
                faq_question=faq_question,
                faq_answer=faq_answer,
                source_refs=[str(ref) for ref in (raw.get("source_refs") or [])],
                item_names=list(gap.get("item_names") or []),
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
