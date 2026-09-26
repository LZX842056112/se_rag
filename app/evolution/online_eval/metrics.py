"""
online_eval：计算 online groundedness / 命中率 / 反馈采纳率 / gap_rate，写 k_metrics。
供看板与 parameter 自调消费。
"""
from __future__ import annotations

import time

from app.evolution.config import evolution_config
from app.evolution.models import MetricSnapshot
from app.evolution.repositories import get_evolution_mongo_tool
from app.evolution.tuning import param_registry
from app.shared.runtime.logger import logger


def compute_snapshot(window_hours: int = 24) -> MetricSnapshot:
    since = time.time() - window_hours * 3600
    repo = get_evolution_mongo_tool()
    snap = MetricSnapshot()
    try:
        # 用 count 替代全量 list()，降低内存占用；语义与原逐条统计一致
        total_fb = repo.fb_events.count_documents({"ts": {"$gte": since}})
        adopted = repo.fb_events.count_documents(
            {"ts": {"$gte": since}, "$or": [{"adopt": True}, {"thumbs": {"$gt": 0}}]}
        )
        rejected = repo.fb_events.count_documents(
            {"ts": {"$gte": since}, "$or": [{"adopt": False}, {"thumbs": {"$lt": 0}}]}
        )
        gap_count = repo.k_gaps.count_documents({"ts": {"$gte": since}})
        resolved = adopted + rejected
        snap.adopt_rate = adopted / resolved if resolved else 0.0
        snap.gap_rate = gap_count / total_fb if total_fb else 0.0
        # params 快照
        snap.params_snapshot = param_registry.get_all_params()
    except Exception as e:
        logger.warning(f"online_eval 计算失败: {e}")
    return snap


def record_metric(snap: MetricSnapshot | None = None) -> None:
    if not getattr(evolution_config, "enabled", False):
        return
    snap = snap or compute_snapshot()
    try:
        get_evolution_mongo_tool().k_metrics.insert_one(snap.document())
    except Exception as e:
        logger.warning(f"写 k_metrics 失败: {e}")


def latest_metrics(recent: int = 7) -> list[dict]:
    try:
        return list(get_evolution_mongo_tool().k_metrics.find().sort("ts", -1).limit(recent))
    except Exception as e:
        logger.warning(f"读取 k_metrics 失败: {e}")
        return []