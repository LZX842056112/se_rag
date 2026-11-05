"""自进化调度器：定时驱动完整闭环「扫描未解决反馈 → 缺口分级 → 候选生成 →
指标快照/参数自调（按周期）→ 回测止损（按周期）」。

复用生产函数 ``scan_unresolved_feedbacks`` / ``generate_candidate``，只做编排与兜底；
幂等由会话级去重 + 问题级去重保证。

历史问题：``record_metric`` / ``adjust_step`` / ``run_backtest`` 三个函数定义完整却**没有任何
调用方**——文档里的「自调参与回测」实际从未运行。现在由本调度器按配置周期调用。

缺口处理使用持久化租约；周期指标任务仍按单 worker 部署。
"""
from __future__ import annotations

import asyncio
import time
import uuid

from pymongo import ReturnDocument

from app.evolution.backtest.runner import run_backtest
from app.evolution.candidate.generator import generate_candidate
from app.evolution.gap.detector import scan_unresolved_feedbacks
from app.evolution.gap.decisions import ignored_gap
from app.evolution.gap.registration import (
    GapClaimLost, activate_gap_registrations, claim_filter, gap_claim_scope, reconcile_registrations,
)
from app.evolution.models import KnowledgeGap
from app.evolution.repositories import evolution_repo
from app.evolution.flags import writes_enabled
from app.evolution.online_eval.metrics import compute_snapshot, record_metric
from app.evolution.tuning.controller import adjust_step
from app.rag.query.embedding_search_service import search_by_milvus
from app.rag.query.greetings import is_plain_greeting
from app.shared.config import settings
from app.shared.runtime.logger import logger

# 周期任务的最近执行时间（进程内；重启后视为「已到点」，当轮即执行一次）
_LAST_METRIC_TS = 0.0
_LAST_BACKTEST_TS = 0.0


def _claim_gap(doc: dict) -> dict | None:
    """创建副作用之前原子领取。过期租约可由后续调度恢复。"""
    now = time.time()
    match = {"_id": doc["_id"], "status": doc["status"]}
    if doc["status"] == "processing":
        match.update(lease_until={"$lte": now}, processing_owner=doc.get("processing_owner"))
    # 覆盖候选生成的两次尝试和 SDK 内部的有界重试。
    lease = max(120, settings.import_.llm_timeout_seconds * (settings.import_.llm_max_retries + 1) * 2 + 60)
    return evolution_repo.k_gaps.find_one_and_update(match, {"$set": {
        "status": "processing", "resume_status": doc.get("resume_status") or doc["status"],
        "processing_owner": uuid.uuid4().hex, "lease_until": now + lease,
        "last_attempt_at": now, "processing_reason": "后台处理中，请稍后刷新",
    }}, return_document=ReturnDocument.AFTER)


def _finish_gap(doc: dict, patch: dict) -> None:
    result = evolution_repo.k_gaps.update_one(
        claim_filter(doc),
        {"$set": patch, "$unset": {key: "" for key in
         ("processing_owner", "lease_until", "resume_status", "next_retry_at") if key not in patch}},
    )
    if not result.matched_count:
        raise GapClaimLost("缺口领取已失效，未提交处理结果")
    activate_gap_registrations(evolution_repo.k_gaps.find_one({"_id": doc["_id"]}) or {})


def _build_context(query: str, item_names: list[str] | None = None) -> list[dict[str, str]]:
    """对缺口查询做一次轻量全库检索取参考片段；零命中/异常一律回退空（由 LLM 据问题提炼）。"""
    try:
        if not query:
            return []
        hits = search_by_milvus(item_names=item_names or [], rewritten_query=query) or []
        docs: list[dict[str, str]] = []
        for hit in hits:
            entity = hit.get("entity", {}) if isinstance(hit, dict) else {}
            content = entity.get("content") or ""
            if content:
                docs.append({**entity, "text": str(content)})
        return docs
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"候选生成上下文检索失败，回退空：{exc}")
        return []


def _process_gap(claimed: dict) -> str:
    oid = claimed["_id"]
    gap = KnowledgeGap.model_validate({**claimed, "gap_id": str(oid)})
    if is_plain_greeting(gap.query):
        _finish_gap(claimed, {"status": "dismissed", "processing_reason": "历史纯寒暄误判，已排除知识处理"})
        return "skipped"
    if claimed["resume_status"] == "reviewing":
        from app.evolution.gap.review import finish_gap_review
        finish_gap_review(claimed)
        return "generated"
    decision = ignored_gap(claimed)
    if decision:
        _finish_gap(claimed, {"status": "dismissed", "dismissed_by_gap_id": str(decision["_id"]),
                              "processing_reason": "同题已人工忽略：" + decision["review_reason"]})
        return "skipped"
    if gap.gap_kind != "knowledge_missing":
        from app.evolution.issues import open_issue
        labels = {"retrieval_miss": "检索遗漏", "reading_miss": "阅读遗漏", "generation_error": "生成错误"}
        types = {"retrieval_miss": "retrieval_failure", "reading_miss": "reading_failure", "generation_error": "answer_error"}
        issue_id = open_issue({"session_id": gap.session_id, "turn_id": "gap:" + str(oid),
            "original_query": gap.query, "gap_id": str(oid), "gap_kind": gap.gap_kind,
            "item_names": gap.item_names, "business_tag": gap.business_tag,
            "issue_type": types.get(gap.gap_kind, "retrieval_failure")})
        if not issue_id:
            raise RuntimeError("策略修复待办未写入")
        reason = f"{labels.get(gap.gap_kind, '原因待确认')}：已转策略修复；核对资料后可人工确认为知识缺失"
        _finish_gap(claimed, {"status": "strategy_pending", "processing_reason": reason, "issue_id": issue_id})
        logger.info(f"缺口转策略修复：gap_id={oid}, gap_kind={gap.gap_kind}")
        return "strategy"
    existing = evolution_repo.k_candidates.find_one({"gap_id": str(oid)})
    if not existing:
        context = _build_context(gap.query or gap.transcript_slice, gap.item_names) \
            if settings.evolution.gen_context_enabled else []
        generate_candidate(gap.model_dump(), context)
        existing = evolution_repo.k_candidates.find_one({"gap_id": str(oid)})
    if not existing:
        raise RuntimeError("未生成候选，等待重试")
    _finish_gap(claimed, {"status": existing["status"], "candidate_id": str(existing["_id"]),
                          "processing_reason": "已登记知识候选"})
    return "generated"


def run_scan_and_generate_once() -> dict[str, int]:
    """扫描并处理持久化强缺口。旧的跳过记录和中断复核也会恢复。

    统计键含义：
        scanned:  本轮领取并入处理侧面的缺口数。
        generated: 成功生成/恢复知识候选的缺口数（映射 _process_gap 的 "generated"）。
        strategy:  转策略修复待办的缺口数（映射 "strategy"）。
        skipped:  领取失败或已完成的缺口数（映射 "skipped"）。
        failed:   处理异常、等待重试的缺口数。
    """
    result = {"scanned": 0, "generated": 0, "strategy": 0, "skipped": 0, "failed": 0}
    # 只读/导入进程不执行（写开关关闭时直接返回零统计）。
    if not writes_enabled():
        return result
    try:
        # 先对账注册，再扫描未解决反馈：把新出现的持续信号物化为候选/待处理缺口。
        reconcile_registrations(settings.evolution.scan_batch)
        scan_unresolved_feedbacks(batch=settings.evolution.scan_batch)
        # 拉取待处理缺口：待生成 review/candidate，以及脱租（lease 过期）的 processing。
        # 同时过滤仍在退避期内（next_retry_at 未到）的条目，避免过早重试。
        pending = evolution_repo.k_gaps.find({"$and": [
            {"$or": [{"status": {"$in": ["candidate", "reviewing"]}},
                     {"status": "processing", "lease_until": {"$lte": time.time()}}]},
            {"$or": [{"next_retry_at": {"$exists": False}}, {"next_retry_at": {"$lte": time.time()}}]},
        ]}).sort([("last_attempt_at", 1), ("ts", 1)]).limit(settings.evolution.scan_batch)
        # 按最久未尝试优先，逐条原子领取后处理；单个缺口失败不影响本轮其余缺口。
        for doc in pending:
            claimed = None
            try:
                # 原子领取（附带租约）；领取失败说明被并发 worker 抢占或状态已变。
                claimed = _claim_gap(doc)
                if not claimed:
                    result["skipped"] += 1
                    continue
                result["scanned"] += 1
                # 在租约作用域内执行处理：结束回调自动清理处理态/续期字段。
                with gap_claim_scope(claimed):
                    result[_process_gap(claimed)] += 1
            except GapClaimLost:
                # 处理过程中租约失效（被其他 worker 接管/超时），放弃本次结果。
                result["skipped"] += 1
            except Exception as exc:  # noqa: BLE001 - 单个缺口失败不影响本轮其余缺口
                logger.warning(f"候选生成异常：{exc}")
                if claimed:
                    try:
                        # 失败后按原状态回退并安排 60 秒后重试，保证可恢复。
                        _finish_gap(claimed, {"status": claimed["resume_status"],
                            "processing_reason": f"处理失败，等待重试：{type(exc).__name__}",
                            "next_retry_at": time.time() + 60})
                    except GapClaimLost:
                        pass
                result["failed"] += 1
    except Exception as exc:  # noqa: BLE001
        # 框架级异常（如查询本身失败）：记录后返回，不中断外层调度循环。
        logger.exception(f"调度单轮执行失败：{exc}")
    return result


def run_metrics_and_tuning_once() -> dict[str, object]:
    """指标快照落库 + 参数自调（同一份快照，避免重复计算与自比自）。

    ``compute_snapshot`` 只调用一次，同时服务「落库」与「自调」两个目的：
    既避免重复计算开销，也保证自调读到的采纳率/缺口率与落库记录完全一致，
    不会出现「自调基于 A 快照、入库却是 B 快照」的偏差。

    返回：
        recorded:  快照是否成功落库（写开关打开时恒为 True）。
        tuned:     本轮的参数自调是否发生/成功。
        adopt_rate / gap_rate: 快照中的采纳率与缺口率（供上层展示/记录）。
    """
    # 只读/导入进程不落库、不自调。
    if not writes_enabled():
        return {"recorded": False, "tuned": False}
    # 计算一次快照并落库（采纳率、缺口率等指标）。
    snapshot = compute_snapshot()
    record_metric(snapshot)
    try:
        # 用同一份快照驱动参数自调；自调属增强项，失败只降级不影响主链路。
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
    if not writes_enabled() or not cfg.backtest_enabled:
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

    三个阶段各自独立：扫描生成每一轮必跑；指标自调与回测按各自的
    配置周期（``metric_interval_minutes`` / ``backtest_interval_hours``）
    决定是否到点执行，并由进程内时间戳 ``_LAST_*_TS`` 记录上次执行时间。

    参数：
        now:  显式指定当前时间（测试注入用）；默认取 ``time.time()``。
        force: ``True`` 时忽略周期限制，三个环节全部执行一次；供排障与手动触发。
    """
    global _LAST_METRIC_TS, _LAST_BACKTEST_TS
    cfg = settings.evolution
    # 阶段一：扫描未解决反馈并生成候选。这一步每轮都执行，不设周期。
    result: dict[str, object] = dict(run_scan_and_generate_once())
    now = time.time() if now is None else now

    # 阶段二：指标快照 + 参数自调。到点（或 force）才执行，并把本次时间推进游标。
    metric_interval = max(cfg.metric_interval_minutes, 0) * 60
    if force or _due(_LAST_METRIC_TS, metric_interval, now):
        result["metrics"] = run_metrics_and_tuning_once()
        _LAST_METRIC_TS = now

    # 阶段三：回测止损。到点（或 force）才执行，同样推进回测游标。
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
        # 函数内部延迟导入，避免模块级循环依赖；阻塞调用放入线程池，避免阻塞事件循环。
        try:
            from app.evolution.governance import run_publication_tasks
            # 先消费发布队列：把已审批候选/撤销任务落地到在线索引。
            await asyncio.to_thread(run_publication_tasks)
            # 再执行一轮完整闭环：扫描缺口→生成候选→（按周期）指标自调/回测止损。
            result = await asyncio.to_thread(run_evolution_cycle_once)
            logger.info(f"调度执行完成：{result}")
        except Exception as exc:  # noqa: BLE001
            # 单轮异常不影响循环存活：记录堆栈后进入下一轮。
            logger.exception(f"调度循环异常：{exc}")
        # 每轮结束后间隔休眠；循环被取消（asyncio.CancelledError）时在此正常退出。
        await asyncio.sleep(interval_seconds)