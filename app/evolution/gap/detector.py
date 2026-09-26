"""
知识缺口检测：聚合 fb_events 中的未解决信号，量化置信度并分级。
仅 strong 进入候选生成；weak/none 归档 k_gaps 后结束。
"""
from __future__ import annotations

import time
from typing import Any

from app.evolution.config import evolution_config
from app.evolution.models import GapSignal, KnowledgeGap
from app.evolution.repositories import get_evolution_mongo_tool
from app.shared.runtime.logger import logger

# 聚类窗口内视为同一缺口（秒）
_DEDUP_WINDOW = 3600


def _de_duplication_key(session_id: str, ts: float) -> bool:
    """同一 session 在窗口内已产生 pending 缺口则跳过，防重复。"""
    try:
        existed = get_evolution_mongo_tool().k_gaps.find_one(
            {"session_id": session_id, "status": {"$in": ["pending", "candidate"]}}
        )
        if existed:
            return True
        recent = get_evolution_mongo_tool().k_gaps.find_one(
            {"session_id": session_id, "ts": {"$gte": ts - _DEDUP_WINDOW}}
        )
        return recent is not None
    except Exception:
        return False


def _grade(signals: GapSignal) -> tuple[str, float]:
    cfg = evolution_config
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
    """由单条 fb_events 信号构造缺口并分类。"""
    cfg = evolution_config
    signals = GapSignal()
    user_bad = fb_doc.get("adopt") is False or fb_doc.get("thumbs", 0) < 0
    has_cited = bool(fb_doc.get("cited_chunk_ids"))
    if user_bad:
        signals.user = 1.0
    if not has_cited:
        signals.retrieval = 1.0
    # generation：差评却检索命中说明"答案生成/供给未达标"，作为接地性低的代理信号。
    # （fb_events 无答案正文无法直接调 compute_groundedness；若后续记录答案可改走 LLM 接地评估）
    if user_bad and has_cited:
        signals.generation = 1.0
    grade, confidence = _grade(signals)
    gap = KnowledgeGap(
        session_id=fb_doc.get("session_id", ""),
        query=fb_doc.get("query", ""),
        item_names=list(fb_doc.get("item_names") or []),
        confidence=confidence,
        signals=signals,
        transcript_slice=transcript_slice[:2000],
        status="candidate" if grade == "strong" else "pending",
    )
    return gap


def scan_unresolved_feedbacks(batch: int = 50) -> list[KnowledgeGap]:
    """扫描最近未解决的 fb_events，产出 strong 缺口。落实到独立任务。"""
    if not getattr(evolution_config, "enabled", False):
        return []
    repo = get_evolution_mongo_tool()
    gaps: list[KnowledgeGap] = []
    try:
        # 仅扫描观察窗内的未解决反馈，配合会话级去重，防无限积压/重复消费
        since = time.time() - evolution_config.observe_window_days * 86400
        cursor = repo.fb_events.find({"adopt": False, "ts": {"$gte": since}}).sort("ts", -1).limit(batch)
        for doc in cursor:
            if _de_duplication_key(doc.get("session_id", ""), doc.get("ts") or 0):
                continue
            gap = detect_and_classify(doc, transcript_slice=doc.get("query", ""))
            if gap.status == "candidate":
                repo.k_gaps.insert_one(gap.document())
                gaps.append(gap)
        logger.info(f"缺口扫描完成，产出 strong 缺口 {len(gaps)} 条")
    except Exception as e:
        logger.warning(f"缺口扫描失败: {e}")
    return gaps