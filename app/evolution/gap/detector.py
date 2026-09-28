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

# 扫描游标（进程内）：按 ``ts`` **升序**推进。
# 真实问题：早期实现 `.sort("ts", -1).limit(batch)` 只取「最新 batch 条」，反馈事件一多，
# 更早的未解决信号就永远排不进窗口（永远扫不到）。改为升序 + 游标前进后，窗口内的信号
# 一定会在有限轮次内被逐条看到；游标用尽（窗口内没有更新的信号）时回到窗口起点复扫，
# 重复由问题级去重兜住。进程重启丢游标只会多扫一轮，故不为此新增 Mongo 字段。
_CURSOR: dict[str, float] = {"ts": 0.0}
_CURSOR_EPSILON = 1e-6  # 冷启动/复扫时包含窗口边界上的事件


def reset_scan_cursor() -> None:
    """重置扫描游标（测试与排障用）。"""
    _CURSOR["ts"] = 0.0


def _is_duplicated(query: str, ts: float) -> bool:
    """**按问题**去重（不是按会话）。

    真实事故：早期实现按 session 判定「已有 pending/candidate 缺口就跳过」，而缺口在候选通过后
    不会复位，于是同一会话里**后续的所有问题永远扫不到**（日志表现为「产出 strong 缺口 0 条」）。

    规则：
    - 同一问题已有 ``pending`` 缺口（尚未生成候选）→ 跳过；
    - 同一问题在去重窗口内出现过缺口 → 跳过。
    已经生成过候选的问题由候选级 ``_exists_question`` 负责去重，因此这里不再看 ``candidate``。
    """
    text = str(query or "").strip()
    if not text:
        return False
    try:
        if evolution_repo.k_gaps.find_one({"query": text, "status": "pending"}):
            return True
        recent = evolution_repo.k_gaps.find_one({"query": text, "ts": {"$gte": ts - _DEDUP_WINDOW}})
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
    """扫描观察窗内未解决的反馈，产出 strong 缺口（其余归档为 pending）。

    按 ``ts`` 升序 + 游标推进：每轮最多消费 ``batch`` 条，**不饿死**更早的未解决信号。
    """
    if not settings.evolution.enabled:
        return []
    gaps: list[KnowledgeGap] = []
    try:
        since = time.time() - settings.evolution.observe_window_days * 86400
        cursor_ts = max(_CURSOR["ts"], since - _CURSOR_EPSILON)
        # 未采纳信号包含两类：读链路写入的 adopt=False（零命中/兜底话术），
        # 以及用户点踩产生的事件（adopt 为 None、thumbs<0）。只查 adopt=False 会漏掉后者，
        # 导致「点踩」永远进不了缺口扫描（实测缺陷 D1）。
        events = (
            evolution_repo.fb_events.find({
                "ts": {"$gt": cursor_ts},
                "$or": [{"adopt": False}, {"thumbs": {"$lt": 0}}],
            })
            .sort("ts", 1)
            .limit(batch)
        )
        scanned = 0
        last_ts = _CURSOR["ts"]
        for doc in events:
            scanned += 1
            last_ts = float(doc.get("ts") or 0.0)
            if _is_duplicated(doc.get("query", ""), doc.get("ts") or 0):
                continue
            gap = detect_and_classify(doc, transcript_slice=doc.get("query", ""))
            if gap.status == "candidate":
                inserted = evolution_repo.k_gaps.insert_one(gap.document())
                gap.gap_id = str(inserted.inserted_id)  # 透传给候选，便于审批后回写缺口状态
                gaps.append(gap)
        # 扫到东西 → 游标前进；窗口内已无更新信号 → 归零，下一轮从窗口起点复扫
        _CURSOR["ts"] = last_ts if scanned else 0.0
        logger.info(
            f"缺口扫描完成：消费信号 {scanned} 条，产出 strong 缺口 {len(gaps)} 条，"
            f"游标 {cursor_ts:.0f} → {_CURSOR['ts']:.0f}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"缺口扫描失败：{exc}")
    return gaps
