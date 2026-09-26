"""
候选生成：用 LLM 从缺口会话与命中切片提炼候选 Q/A，做 PII 拦截后入库 k_candidates。
JSON-Schema 强约束 + 重试×2；失败返回空，不阻塞。
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from app.evolution.candidate.pii import sanitize_candidate
from app.evolution.config import evolution_config
from app.evolution.models import KnowledgeCandidate
from app.evolution.repositories import get_evolution_mongo_tool
from app.infra.llm.providers import llm_providers
from app.shared.runtime.logger import logger

_PROMPT = (
    "你是企业知识库的候选知识点提炼器。根据‘用户问题’和‘参考片段’，提炼一条标准 FAQ。\n"
    "只输出 JSON，禁止多余文字，格式：{{\"faq_question\":\"\",\"faq_answer\":\"\",\"source_refs\":[\"\"]}}。\n"
    "要求：答案忠实于参考片段与事实，不臆造；长度 60~300 字；faq_question 为该问题的规范化表述。\n"
    "用户问题：{question}\n参考片段：\n{context}\n"
)


def _call_llm(question: str, context: str) -> dict[str, Any]:
    messages = [HumanMessage(content=_PROMPT.format(question=question, context=context[:3000]))]
    llm = llm_providers.chat(mode_name=None, json_mode=True)
    resp: AIMessage = llm.invoke(messages)
    text = resp.content.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return json.loads(text)


def generate_candidate(gap: dict[str, Any], context_docs: list[dict[str, Any]]) -> KnowledgeCandidate | None:
    """基于缺口生成候选。PII 命中则拒绝并留痕。"""
    question = gap.get("query") or gap.get("session_id", "")
    snippet = gap.get("transcript_slice") or gap.get("query") or gap.get("session_id", "")
    context = "\n".join(str(d.get("text") or d.get("content") or "")[:300] for d in context_docs[-5:])
    last_err: Exception | None = None
    for _ in range(2):
        try:
            raw = _call_llm(snippet or question, context)
            q = str(raw.get("faq_question", "")).strip()
            a = str(raw.get("faq_answer", "")).strip()
            refs = list(raw.get("source_refs") or [])
            if not q or not a:
                continue
            q, a, has_pii = sanitize_candidate(q, a)
            candidate = KnowledgeCandidate(
                faq_question=q,
                faq_answer=a,
                source_refs=[str(r) for r in refs],
                item_names=list(gap.get("item_names") or []),
                status="rejected" if has_pii else "draft",
                reason="PII detected" if has_pii else "",
            )
            # 按 faq_question 去重
            if _exists_question(q):
                candidate.status = "rejected"
                candidate.reason = "duplicate"
            get_evolution_mongo_tool().k_candidates.insert_one(candidate.document())
            return candidate
        except Exception as e:  # 重试
            last_err = e
    if last_err:
        logger.warning(f"候选生成失败: {last_err}")
    return None


def _exists_question(q: str) -> bool:
    try:
        return get_evolution_mongo_tool().k_candidates.find_one({"faq_question": q}) is not None
    except Exception:
        return False