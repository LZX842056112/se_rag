"""自进化调度器：定时驱动完整闭环「扫描未解决反馈 → 缺口分级 → 候选生成 →
指标快照/参数自调（按周期）→ 回测止损（按周期）」。

复用生产函数 ``scan_unresolved_feedbacks`` / ``generate_candidate``，只做编排与兜底；
幂等由会话级去重 + 问题级去重保证。

历史问题：``record_metric`` / ``adjust_step`` / ``run_backtest`` 三个函数定义完整却**没有任何
调用方**——文档里的「自调参与回测」实际从未运行。现在由本调度器按配置周期调用。

注意：按单 worker 部署设计（任务进程内唯一）；多 worker 需另加分布式锁。
"""
from __future__ import annotations

import asyncio
import time

from app.evolution.backtest.runner import run_backtest
from app.evolution.candidate.generator import generate_candidate
from app.evolution.gap.detector import scan_unresolved_feedbacks
from app.evolution.online_eval.metrics import compute_snapshot, record_metric
from app.evolution.tuning.controller import adjust_step
from app.rag.query.embedding_search_service import search_by_milvus
from app.shared.config import settings
from app.shared.runtime.logger import logger

# 周期任务的最近执行时间（进程内；重启后视为「已到点」，当轮即执行一次）
_LAST_METRIC_TS = 0.0
_LAST_BACKTEST_TS = 0.0


def _build_context(query: str) -> list[dict[str, str]]:
    """对缺口查询做一次轻量全库检索取参考片段；零命中/异常一律回退空（由 LLM 据问题提炼）。"""
    try:
        if not query:
            return []
        hits = search_by_milvus(item_names=[], rewritten_query=query) or []
        docs: list[dict[str, str]] = []
        for hit in hits:
            entity = hit.get("entity", {}) if isinstance(hit, dict) else {}
            content = entity.get("content") or ""
            if content:
                docs.append({"text": str(content)})
        return docs
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"候选生成上下文检索失败，回退空：{exc}")
        return []


def run_scan_and_generate_once() -> dict[str, int]:
    """单轮调度：扫描未解决反馈，对 strong 缺口生成候选；任何异常不外抛。"""
    result = {"scanned": 0, "generated": 0, "skipped": 0, "failed": 0}
    if not settings.evolution.enabled:
        return result
    try:
        gaps = scan_unresolved_feedbacks(batch=settings.evolution.scan_batch)
        result["scanned"] = len(gaps)
        for gap in gaps:
            if gap.status != "candidate":
                result["skipped"] += 1
                continue
            context = _build_context(gap.transcript_slice or gap.session_id) \
                if settings.evolution.gen_context_enabled else []
            try:
                candidate = generate_candidate(gap.document(), context)
            except Exception as exc:  # noqa: BLE001 - 单个缺口失败不影响本轮其余缺口
                logger.warning(f"候选生成异常：{exc}")
                result["failed"] += 1
                continue
            if candidate is None:
                result["failed"] += 1
            else:
                result["generated"] += 1
    except Exception as exc:  # noqa: BLE001
        logger.exception(f"调度单轮执行失败：{exc}")
    return result


def run_metrics_and_tuning_once() -> dict[str, object]:
    """指标快照落库 + 参数自调（同一份快照，避免重复计算与自比自）。"""
    if not settings.evolution.enabled:
        return {"recorded": False, "tuned": False}
    snapshot = compute_snapshot()
    record_metric(snapshot)
    try:
        tuned = adjust_step(snapshot)
    except Exception as exc:  # noqa: BLE001 - 自调属增强项，失败不影响主链路
        logger.warning(f"参数自调失败：{exc}")
        tuned = False
    logger.info(
        f"指标快照：采纳率={snapshot.adopt_rate:.2f} 缺口率={snapshot.gap_rate:.2f} 自调={'有' if tuned else '无'}"
    )
    return {
        "recorded": True,
        "tuned": tuned,
        "adopt_rate": round(snapshot.adopt_rate, 4),
        "gap_rate": round(snapshot.gap_rate, 4),
    }


def run_backtest_once() -> dict[str, object]:
    """回测观察窗内的已入库条目，不达标自动下架。"""
    cfg = settings.evolution
    if not cfg.enabled or not cfg.backtest_enabled:
        return {"backtested": 0, "removed": 0}
    results = run_backtest()
    removed = [r.evo_doc_id for r in results if r.verdict == "remove"]
    logger.info(f"回测完成：考察 {len(results)} 条，下架 {len(removed)} 条")
    return {"backtested": len(results), "removed": len(removed)}


def _due(last_ts: float, interval_seconds: float, now: float) -> bool:
    """周期判断：间隔 <= 0 表示每轮都执行。"""
    return interval_seconds <= 0 or now - last_ts >= interval_seconds


def run_evolution_cycle_once(*, now: float | None = None, force: bool = False) -> dict[str, object]:
    """一轮完整闭环：扫描生成 →（按周期）指标/自调 →（按周期）回测止损。

    ``force=True`` 忽略周期限制，供排障与手动触发使用。
    """
    global _LAST_METRIC_TS, _LAST_BACKTEST_TS
    cfg = settings.evolution
    result: dict[str, object] = dict(run_scan_and_generate_once())
    now = time.time() if now is None else now

    metric_interval = max(cfg.metric_interval_minutes, 0) * 60
    if force or _due(_LAST_METRIC_TS, metric_interval, now):
        result["metrics"] = run_metrics_and_tuning_once()
        _LAST_METRIC_TS = now

    backtest_interval = max(cfg.backtest_interval_hours, 0) * 3600
    if force or _due(_LAST_BACKTEST_TS, backtest_interval, now):
        result["backtest"] = run_backtest_once()
        _LAST_BACKTEST_TS = now
    return result


def loop_status() -> dict[str, object]:
    """闭环运行状态快照（供状态接口展示，避免只能翻日志）。"""
    cfg = settings.evolution
    return {
        "scheduler_enabled": cfg.scheduler_enabled,
        "interval_minutes": cfg.schedule_interval_minutes,
        "last_metric_ts": _LAST_METRIC_TS,
        "metric_interval_minutes": cfg.metric_interval_minutes,
        "last_backtest_ts": _LAST_BACKTEST_TS,
        "backtest_enabled": cfg.backtest_enabled,
        "backtest_interval_hours": cfg.backtest_interval_hours,
    }


async def evolution_scheduler_loop(interval_seconds: float) -> None:
    """后台循环：按间隔执行单轮扫描+生成；被取消时正常结束。"""
    while True:
        try:
            result = await asyncio.to_thread(run_evolution_cycle_once)
            logger.info(f"调度执行完成：{result}")
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"调度循环异常：{exc}")
        await asyncio.sleep(interval_seconds)
