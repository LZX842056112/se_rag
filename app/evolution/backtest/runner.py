"""回测观察窗：聚合近 N 天内候选条目的命中/采纳统计，不达标自动下架并告警。"""
from __future__ import annotations

import time
from dataclasses import dataclass

from app.evolution.index.update import deactivate
from app.evolution.repositories import evolution_repo
from app.shared.config import settings
from app.shared.runtime.logger import logger


@dataclass
class BacktestResult:
    """单个进化条目的回测结论。"""

    evo_doc_id: str
    hits: int = 0
    adopted: int = 0
    rejected: int = 0
    adopt_rate: float = 0.0
    reject_rate: float = 0.0
    verdict: str = "hold"  # hold | promote | remove


def _active_items() -> list[dict]:
    """读取已入库（active）的候选条目。"""
    try:
        return list(evolution_repo.k_candidates.find(
            {"status": "active", "evo_doc_id": {"$ne": None}}
        ))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取 active 候选失败：{exc}")
        return []


def run_backtest(window_days: int | None = None) -> list[BacktestResult]:
    """对每个 active 候选统计观察窗内命中与采纳，输出判定并执行止损下架。"""
    if not settings.evolution.enabled:
        return []
    cfg = settings.evolution
    window_days = window_days or cfg.observe_window_days
    since = time.time() - window_days * 86400

    # 一次性加载观察窗内反馈并按被引用的进化 id 建倒排索引，避免逐候选全表扫描（N+1）
    buckets: dict[str, list[dict]] = {}
    try:
        for event in evolution_repo.fb_events.find(
            {"ts": {"$gte": since}}, {"ts": 1, "adopt": 1, "thumbs": 1, "cited_chunk_ids": 1}
        ):
            for cited in (event.get("cited_chunk_ids") or []):
                buckets.setdefault(str(cited), []).append(event)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"回测加载反馈失败：{exc}")
        return []

    results: list[BacktestResult] = []
    for candidate in _active_items():
        evo_id = candidate.get("evo_doc_id")
        result = BacktestResult(evo_doc_id=evo_id)
        for event in buckets.get(evo_id) or []:
            result.hits += 1
            if event.get("adopt") is True or (event.get("thumbs") or 0) > 0:
                result.adopted += 1
            elif event.get("adopt") is False or (event.get("thumbs") or 0) < 0:
                result.rejected += 1

        result.adopt_rate = result.adopted / result.hits if result.hits else 0.0
        result.reject_rate = result.rejected / result.hits if result.hits else 0.0
        if result.hits == 0:
            result.verdict = "hold"  # 证据不足，先放着
        elif result.hits < cfg.backtest_min_hits:
            result.verdict = "hold"  # 命中次数不足，避免单条差评误杀人工审批的知识
        elif result.reject_rate > cfg.reject_rate_max:
            result.verdict = "remove"  # 拒绝率超上限，先下架
        elif result.adopt_rate >= cfg.attain_rate_min:
            result.verdict = "promote"
        else:
            result.verdict = "hold"  # 被引用但多为中立，继续观察避免误杀

        if result.verdict == "remove":
            if deactivate(evo_id):
                try:
                    evolution_repo.k_candidates.update_one(
                        {"evo_doc_id": evo_id},
                        {"$set": {"status": "deprecated", "reason": "backtest failed"}},
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"回测下架后更新候选状态失败 {evo_id}：{exc}")
                logger.warning(f"[自进化止损] 条目 {evo_id} 回测不达标已下架")
        results.append(result)
    return results
