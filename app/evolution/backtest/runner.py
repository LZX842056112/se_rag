"""
回测观察窗：聚合近 N 天内候选条目在 fb_events 中的命中/采纳统计。
达标（采纳≥attain_rate_min）保留放量，不达标（拒绝率>reject_rate_max 或采纳过低）自动下架并告警。
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.evolution.config import evolution_config
from app.evolution.index.update import deactivate
from app.evolution.repositories import get_evolution_mongo_tool
from app.shared.runtime.logger import logger


@dataclass
class BacktestResult:
    evo_doc_id: str
    hits: int = 0
    adopted: int = 0
    rejected: int = 0
    adopt_rate: float = 0.0
    reject_rate: float = 0.0
    verdict: str = "hold"   # hold | promote | remove


def _active_items() -> list[dict]:
    try:
        return list(get_evolution_mongo_tool().k_candidates.find(
            {"status": "active", "evo_doc_id": {"$ne": None}}
        ))
    except Exception as e:
        logger.warning(f"读取 active 候选失败: {e}")
        return []


def run_backtest(window_days: int | None = None) -> list[BacktestResult]:
    """对每个 active 候选统计观察窗内命中与采纳，输出判定并落执行。"""
    if not getattr(evolution_config, "enabled", False):
        return []
    window_days = window_days or evolution_config.observe_window_days
    since = time.time() - window_days * 86400
    cfg = evolution_config
    results: list[BacktestResult] = []
    repo = get_evolution_mongo_tool()

    # 一次性加载观察窗内反馈，并按被引用的进化 id 建倒排索引，避免对每个候选全表扫描（N+1）。
    buckets: dict[str, list[dict]] = {}
    try:
        for ev in repo.fb_events.find(
            {"ts": {"$gte": since}}, {"ts": 1, "adopt": 1, "thumbs": 1, "cited_chunk_ids": 1}
        ):
            for c in (ev.get("cited_chunk_ids") or []):
                buckets.setdefault(str(c), []).append(ev)
    except Exception as e:
        logger.warning(f"回测加载反馈失败: {e}")
        return []

    for candidate in _active_items():
        evo_id = candidate.get("evo_doc_id")
        res = BacktestResult(evo_doc_id=evo_id)
        events = buckets.get(evo_id) or []
        for ev in events:
            res.hits += 1
            if ev.get("adopt") is True or (ev.get("thumbs") or 0) > 0:
                res.adopted += 1
            elif ev.get("adopt") is False or (ev.get("thumbs") or 0) < 0:
                res.rejected += 1

        res.adopt_rate = res.adopted / res.hits if res.hits else 0.0
        res.reject_rate = res.rejected / res.hits if res.hits else 0.0
        if res.hits == 0:
            res.verdict = "hold"          # 证据不足，先放着
        elif res.reject_rate > cfg.reject_rate_max:
            res.verdict = "remove"        # 拒绝率超上限，先下架
        elif res.adopt_rate >= cfg.attain_rate_min:
            res.verdict = "promote"
        else:
            res.verdict = "hold"          # 被引用但多为中立，证据不足继续观察，避免误杀
        if res.verdict == "remove":
            if deactivate(evo_id):
                try:
                    repo.k_candidates.update_one(
                        {"evo_doc_id": evo_id}, {"$set": {"status": "deprecated", "reason": "backtest failed"}}
                    )
                except Exception as e:
                    logger.error(f"回测下架后更新候选状态失败 {evo_id}: {e}")
                logger.warning(f"[自进化止损] 条目 {evo_id} 回测不达标已下架")
        results.append(res)
    return results