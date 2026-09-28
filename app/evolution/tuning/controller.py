"""参数自适配控制器：依据 ``k_metrics`` 增量做小幅自调，并做止损回退。

原则：无恶化不动作；明显恶化回退到回滚值；写 ``param_registry``（updated_by=metric）。
"""
from __future__ import annotations

from app.evolution.models import MetricSnapshot
from app.evolution.online_eval.metrics import compute_snapshot
from app.evolution.repositories import evolution_repo
from app.evolution.tuning import param_registry
from app.shared.config import settings
from app.shared.runtime.logger import logger

# 可自调参数的步长与界限
_STEP = {"RRF_K": 10, "RRF_TOP": 1, "RERANK_TOP_K": 1}
_MIN = {"RRF_K": 20, "RRF_TOP": 2, "RERANK_TOP_K": 2}
_MAX = {"RRF_K": 200, "RRF_TOP": 20, "RERANK_TOP_K": 6}


def _baseline_adopt_rate(before_ts: float | None = None) -> float | None:
    """取最近 3 条历史指标快照的平均采纳率作为基线。

    ``before_ts`` 用于排除「本轮刚写入的快照」：否则自己与自己比较，``diff`` 恒为
    一个被自身拉平的小值，自调几乎永不触发（接线时的经典隐形坑）。
    """
    try:
        query = {"ts": {"$lt": before_ts}} if before_ts is not None else {}
        rows = list(evolution_repo.k_metrics.find(query).sort("ts", -1).limit(3))
        if not rows:
            return None
        return sum(row.get("adopt_rate", 0.0) for row in rows) / len(rows)
    except Exception:  # noqa: BLE001
        return None


def adjust_step(snapshot: MetricSnapshot | None = None) -> bool:
    """执行一次参数自调 + 止损判定，返回是否有参数被更新。

    三态：数据不足/持平不动；明显改善小幅上调；小幅恶化小幅下调（不越下限）；骤降回退下限。

    ``snapshot`` 由调度器传入（与写库的是同一份）；不传时自行计算，保持函数可独立调用。
    """
    if not settings.evolution.enabled:
        return False
    snapshot = snapshot or compute_snapshot()
    baseline = _baseline_adopt_rate(snapshot.ts)
    if baseline is None or baseline <= 0.0:
        return False  # 基线数据不足，不做动作

    diff = snapshot.adopt_rate - baseline
    drop_line = settings.evolution.alarm_adopt_rate_drop
    small = drop_line / 2.0
    changed = False

    if diff <= -drop_line:
        logger.warning(f"[自进化止损] 采纳率 {snapshot.adopt_rate:.2f} 较基线 {baseline:.2f} 骤降，回退参数")
        for key, low in _MIN.items():
            param_registry.set_param(key, low, updated_by="metric")
        return True

    if diff >= small:
        for key, step in _STEP.items():
            nxt = int(param_registry.get_param(key) or 0) + step
            if nxt <= _MAX[key]:
                param_registry.set_param(key, nxt, updated_by="metric")
                changed = True
    elif diff <= -small:
        for key, step in _STEP.items():
            nxt = int(param_registry.get_param(key) or 0) - step
            if nxt >= _MIN[key]:
                param_registry.set_param(key, nxt, updated_by="metric")
                changed = True
    return changed
