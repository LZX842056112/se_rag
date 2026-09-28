"""知识缺口检测：聚合 ``fb_events`` 中的未解决信号，量化置信度并分级。

仅 strong 缺口进入候选生成；weak/none 归档 ``k_gaps`` 后结束。
"""
from __future__ import annotations

import time
from typing import Any

from app.evolution.models import GapSignal, KnowledgeGap
from app.evolution.repositories import evolution_repo
from app.shared.config import settings
from app.shared.runtime.logger import logger

# 聚类窗口内视为同一缺口（秒）
_DEDUP_WINDOW = 3600


def _is_duplicated(session_id: str, ts: float) -> bool:
    """同一 session 在窗口内已产生 pending/candidate 缺口则跳过，防重复消费。"""
    try:
        existed = evolution_repo.k_gaps.find_one(
            {"session_id": session_id, "status": {"$in": ["pending", "candidate"]}}
        )
        if existed:
            return True
        recent = evolution_repo.k_gaps.find_one({"session_id": session_id, "ts": {"$gte": ts - _DEDUP_WINDOW}})
        return recent is not None
    except Exception:  # noqa: BLE001 - 去重失败按「不重复」处理，交由后续去重兜底
        return False


def grade_signals(signals: GapSignal) -> tuple[str, float]:
    """按配置权重加权求置信度并分级（strong / weak / none）。"""
    cfg = settings.evolution
    confidence = (
        cfg.gap_weight_user * signals.user
        + cfg.gap_weight_retrieval * signals.retrieval
        + cfg.gap_weight_generation * signals.generation
    )
    if confidence >= cfg.gap_strong_threshold:
        return "strong", confidence
    if confidence >= cfg.gap_weak_threshold:
        return "weak", confidence
    return "none", confidence


def detect_and_classify(fb_doc: dict[str, Any], transcript_slice: str = "") -> KnowledgeGap:
    """由单条反馈事件构造缺口并分类。"""
    signals = GapSignal()
    user_bad = fb_doc.get("adopt") is False or (fb_doc.get("thumbs") or 0) < 0
    has_cited = bool(fb_doc.get("cited_chunk_ids"))
    if user_bad:
        signals.user = 1.0
    if not has_cited:
        signals.retrieval = 1.0
    # generation：差评但检索命中，说明「答案生成/供给未达标」，作为接地性低的代理信号
    # （fb_events 不含答案正文，无法直接调用 compute_groundedness）
    if user_bad and has_cited:
        signals.generation = 1.0

    grade, confidence = grade_signals(signals)
    return KnowledgeGap(
        session_id=fb_doc.get("session_id", ""),
        query=fb_doc.get("query", ""),
        item_names=list(fb_doc.get("item_names") or []),
        confidence=confidence,
        signals=signals,
        transcript_slice=transcript_slice[:2000],
        status="candidate" if grade == "strong" else "pending",
    )


def scan_unresolved_feedbacks(batch: int = 50) -> list[KnowledgeGap]:
    """扫描观察窗内未解决的反馈，产出 strong 缺口（其余归档为 pending）。"""
    if not settings.evolution.enabled:
        return []
    gaps: list[KnowledgeGap] = []
    try:
        since = time.time() - settings.evolution.observe_window_days * 86400
        # 未采纳信号包含两类：读链路写入的 adopt=False（零命中/兜底话术），
        # 以及用户点踩产生的事件（adopt 为 None、thumbs<0）。只查 adopt=False 会漏掉后者，
        # 导致「点踩」永远进不了缺口扫描（实测缺陷 D1）。
        cursor = (
            evolution_repo.fb_events.find({
                "ts": {"$gte": since},
                "$or": [{"adopt": False}, {"thumbs": {"$lt": 0}}],
            })
            .sort("ts", -1)
            .limit(batch)
        )
        for doc in cursor:
            if _is_duplicated(doc.get("session_id", ""), doc.get("ts") or 0):
                continue
            gap = detect_and_classify(doc, transcript_slice=doc.get("query", ""))
            if gap.status == "candidate":
                evolution_repo.k_gaps.insert_one(gap.document())
                gaps.append(gap)
        logger.info(f"缺口扫描完成，产出 strong 缺口 {len(gaps)} 条")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"缺口扫描失败：{exc}")
    return gaps
