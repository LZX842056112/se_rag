"""在线指标快照：计算采纳率 / 缺口率等并写入 ``k_metrics``（供看板与参数自调消费）。"""
from __future__ import annotations

import time

from app.evolution.models import MetricSnapshot
from app.evolution.repositories import evolution_repo
from app.evolution.tuning import param_registry
from app.shared.config import settings
from app.shared.runtime.logger import logger


def compute_snapshot(window_hours: int = 24) -> MetricSnapshot:
    """按观察窗聚合反馈与缺口，生成一份指标快照。"""
    since = time.time() - window_hours * 3600
    snapshot = MetricSnapshot()
    try:
        total_fb = evolution_repo.fb_events.count_documents({"ts": {"$gte": since}})
        adopted = evolution_repo.fb_events.count_documents(
            {"ts": {"$gte": since}, "$or": [{"adopt": True}, {"thumbs": {"$gt": 0}}]}
        )
        rejected = evolution_repo.fb_events.count_documents(
            {"ts": {"$gte": since}, "$or": [{"adopt": False}, {"thumbs": {"$lt": 0}}]}
        )
        gap_count = evolution_repo.k_gaps.count_documents({"ts": {"$gte": since}})
        resolved = adopted + rejected
        snapshot.adopt_rate = adopted / resolved if resolved else 0.0
        snapshot.gap_rate = gap_count / total_fb if total_fb else 0.0
        snapshot.params_snapshot = param_registry.get_all_params()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"online_eval 计算失败：{exc}")
    return snapshot


def record_metric(snapshot: MetricSnapshot | None = None) -> None:
    """把指标快照写入 ``k_metrics``（自进化关闭时不写）。"""
    if not settings.evolution.enabled:
        return
    snapshot = snapshot or compute_snapshot()
    try:
        evolution_repo.k_metrics.insert_one(snapshot.document())
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"写 k_metrics 失败：{exc}")


def latest_metrics(recent: int = 7) -> list[dict]:
    """读取最近若干条指标快照。"""
    try:
        return list(evolution_repo.k_metrics.find().sort("ts", -1).limit(recent))
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"读取 k_metrics 失败：{exc}")
        return []
