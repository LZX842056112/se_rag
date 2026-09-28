"""自进化闭环接线测试。

回归背景：``record_metric`` / ``adjust_step`` / ``run_backtest`` 三个函数定义完整，
却没有任何调用方——文档里的「指标快照 + 参数自调 + 回测止损」实际从未运行。
本文件锁定「调度器确实按周期调用它们」以及「自调基线不与本轮快照自比自」。
"""
from __future__ import annotations

import time

import mongomock
import pytest

from app.evolution import repositories as repositories_module
from app.evolution import scheduler as scheduler_module
from app.evolution.tuning import controller, param_registry
from app.shared.config import settings


@pytest.fixture
def fake_mongo(monkeypatch):
    """用 mongomock 替换集合适配器（参数注册表 / k_metrics 都走它）。"""
    client = mongomock.MongoClient()
    db = client["test_db"]
    monkeypatch.setattr(repositories_module, "get_collection", lambda name: db[name])
    monkeypatch.setattr(param_registry, "_LAST_LOAD", 0.0)
    param_registry._CACHE.clear()
    return db


@pytest.fixture
def evolution_on(monkeypatch):
    monkeypatch.setattr(settings.evolution, "enabled", True)
    monkeypatch.setattr(settings.evolution, "metric_interval_minutes", 60)
    monkeypatch.setattr(settings.evolution, "backtest_interval_hours", 24)
    monkeypatch.setattr(settings.evolution, "backtest_enabled", True)


def _spy_scheduler(monkeypatch, calls: dict) -> None:
    """把扫描/指标/自调/回测全部替换成记账桩，只验证编排。"""
    class _Snapshot:
        ts = 1.0
        adopt_rate = 0.5
        gap_rate = 0.1

    monkeypatch.setattr(scheduler_module, "run_scan_and_generate_once",
                        lambda: calls.setdefault("scan", []).append(1) or {"scanned": 0, "generated": 0})
    monkeypatch.setattr(
        scheduler_module, "compute_snapshot",
        lambda: (calls.setdefault("snapshot", []).append(1), _Snapshot())[1],
    )
    monkeypatch.setattr(scheduler_module, "record_metric",
                        lambda snapshot=None: calls.setdefault("record", []).append(snapshot))
    monkeypatch.setattr(scheduler_module, "adjust_step",
                        lambda snapshot=None: calls.setdefault("tune", []).append(snapshot) or False)
    monkeypatch.setattr(scheduler_module, "run_backtest",
                        lambda: calls.setdefault("backtest", []).append(1) or [])
    monkeypatch.setattr(scheduler_module, "_LAST_METRIC_TS", 0.0)
    monkeypatch.setattr(scheduler_module, "_LAST_BACKTEST_TS", 0.0)


def test_force_cycle_runs_scan_metrics_and_backtest(monkeypatch, evolution_on):
    calls: dict = {}
    _spy_scheduler(monkeypatch, calls)

    result = scheduler_module.run_evolution_cycle_once(now=1000.0, force=True)

    assert calls["scan"] == [1]
    assert len(calls["record"]) == 1 and calls["record"][0] is not None
    assert len(calls["tune"]) == 1, "自调必须被真正调用（此前从不运行）"
    assert calls["tune"][0] is calls["record"][0], "自调应与落库用同一份快照，避免重复计算"
    assert calls["backtest"] == [1], "回测必须被真正调用（此前从不运行）"
    assert result["metrics"]["recorded"] is True
    assert result["backtest"]["backtested"] == 0


def test_metrics_and_backtest_respect_their_intervals(monkeypatch, evolution_on):
    calls: dict = {}
    _spy_scheduler(monkeypatch, calls)
    start = time.time()

    scheduler_module.run_evolution_cycle_once(now=start)
    # 10 秒后再跑一轮：60 分钟指标周期 / 24 小时回测周期都还没到
    scheduler_module.run_evolution_cycle_once(now=start + 10)

    assert calls["scan"] == [1, 1], "扫描每轮都要做"
    assert len(calls["record"]) == 1, "指标快照应按周期写，而不是每轮都写"
    assert len(calls["backtest"]) == 1, "回测应按周期跑，而不是每轮都跑"

    # 越过指标周期（61 分钟）与剩余回测周期
    scheduler_module.run_evolution_cycle_once(now=start + 61 * 60)
    assert len(calls["record"]) == 2


def test_backtest_skipped_when_disabled(monkeypatch, evolution_on):
    calls: dict = {}
    _spy_scheduler(monkeypatch, calls)
    monkeypatch.setattr(settings.evolution, "backtest_enabled", False)

    result = scheduler_module.run_evolution_cycle_once(now=time.time(), force=True)

    assert "backtest" not in calls
    assert result["backtest"] == {"backtested": 0, "removed": 0}


def test_metrics_and_tuning_noop_when_evolution_disabled(monkeypatch):
    monkeypatch.setattr(settings.evolution, "enabled", False)
    result = scheduler_module.run_metrics_and_tuning_once()
    assert result == {"recorded": False, "tuned": False}
    assert scheduler_module.run_backtest_once() == {"backtested": 0, "removed": 0}


def test_adjust_step_baseline_excludes_current_snapshot(fake_mongo, evolution_on, monkeypatch):
    """基线取「历史」快照：与本轮快照自比自会让 diff 恒接近 0，自调永不触发。"""
    now = time.time()
    monkeypatch.setattr(settings.evolution, "alarm_adopt_rate_drop", 0.10)
    for offset in (300, 200, 100):
        fake_mongo[settings.mongo.k_metrics_collection].insert_one(
            {"ts": now - offset, "adopt_rate": 0.90, "gap_rate": 0.0, "params_snapshot": {}}
        )

    class _Snapshot:
        ts = now
        adopt_rate = 0.20
        gap_rate = 0.0

    changed = controller.adjust_step(_Snapshot())

    assert changed is True, "采纳率骤降必须触发止损回退"
    assert param_registry.get_param("RRF_K") == controller._MIN["RRF_K"]
    record = fake_mongo[settings.mongo.param_registry_collection].find_one({"key": "RRF_K"})
    assert record["updated_by"] == "metric"


def test_adjust_step_does_nothing_without_baseline(fake_mongo, evolution_on):
    class _Snapshot:
        ts = time.time()
        adopt_rate = 0.99
        gap_rate = 0.0

    assert controller.adjust_step(_Snapshot()) is False
