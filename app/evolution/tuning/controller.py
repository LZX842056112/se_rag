"""
参数自适配控制器：依据 k_metrics 增量做小幅自调，并做止损回退。
原则：无恶化不动作；明显恶化回退到回滚值；写 param_registry (updated_by=metric)。
"""
from __future__ import annotations

from app.evolution.config import evolution_config
from app.evolution.online_eval.metrics import compute_snapshot
from app.evolution.repositories import get_evolution_mongo_tool
from app.evolution.tuning import param_registry
from app.shared.runtime.logger import logger

# 可自调参数的步长与界限
_STEP = {"RRF_K": 10, "RRF_TOP": 1, "RERANK_TOP_K": 1}
_MIN = {"RRF_K": 20, "RRF_TOP": 2, "RERANK_TOP_K": 2}
_MAX = {"RRF_K": 200, "RRF_TOP": 20, "RERANK_TOP_K": 6}


def _baseline_adopt_rate() -> float | None:
    """取最近 3 条 k_metrics 的平均采纳率作基线。"""
    try:
        rows = list(get_evolution_mongo_tool().k_metrics.find().sort("ts", -1).limit(3))
        if not rows:
            return None
        rates = [r.get("adopt_rate", 0.0) for r in rows]
        return sum(rates) / len(rates)
    except Exception:
        return None


def adjust_step() -> bool:
    """执行一次参数自调 + 止损判定。返回是否有更新。

    三态：数据不足/持平不动；明显改善小幅上调；小幅恶化小幅下调（不越 MIN）；骤降回退下限。
    """
    if not getattr(evolution_config, "enabled", False):
        return False
    snap = compute_snapshot()
    baseline = _baseline_adopt_rate()
    if baseline is None or baseline <= 0.0:
        return False  # 基线数据不足，不做动作
    diff = snap.adopt_rate - baseline
    drop_line = evolution_config.alarm_adopt_rate_drop
    small = drop_line / 2.0
    changed = False
    if diff <= -drop_line:
        # 止损：采纳率骤降，回退到下限
        logger.warning(f"[自进化止损] 采纳率 {snap.adopt_rate:.2f} 较基线 {baseline:.2f} 骤降，回退参数")
        for key, low in _MIN.items():
            param_registry.set_param(key, low, updated_by="metric")
        return True
    if diff >= small:
        # 明显改善：小幅上调
        for key, step in _STEP.items():
            cur = int(param_registry.get_param(key) or 0)
            nxt = cur + step
            if nxt <= _MAX[key]:
                param_registry.set_param(key, nxt, updated_by="metric")
                changed = True
    elif diff <= -small:
        # 小幅恶化：小幅下调，不低于下限
        for key, step in _STEP.items():
            cur = int(param_registry.get_param(key) or 0)
            nxt = cur - step
            if nxt >= _MIN[key]:
                param_registry.set_param(key, nxt, updated_by="metric")
                changed = True
    # 持平区间内不动，避免参数单调漂移
    return changed