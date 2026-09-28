"""对外引用（citation）构造：读链路落库、历史回显与实时 SSE 共用同一实现。

历史问题：API 层 ``_build_citations`` 与 ``answer_service.backfill_evolution_outputs``
各写一份相同的引用构造逻辑，容易出现「历史回显与实时返回不一致」。
"""
from __future__ import annotations

from app.evolution.schema import CitationModel


def build_citations(cited_ids: list | None, evolution_ids: list | None) -> list[dict]:
    """按「引用 id 列表 + 自进化 id 集合」构造引用列表（同 id 去重保序）。"""
    evolution_set = {str(i) for i in (evolution_ids or [])}
    citations: list[dict] = []
    seen: set[str] = set()
    for cid in cited_ids or []:
        if cid is None:
            continue
        key = str(cid)
        if key in seen:
            continue
        seen.add(key)
        citations.append(CitationModel(
            faq_id=key,
            source="evolution" if key in evolution_set else "kb",
        ).model_dump())
    return citations
