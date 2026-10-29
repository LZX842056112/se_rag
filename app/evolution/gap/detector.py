"""知识缺口检测：聚合 ``fb_events`` 中的未解决信号，量化置信度并分级。

仅 strong 缺口进入候选生成；weak/none 归档 ``k_gaps`` 后结束。
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.evolution.models import GapSignal, KnowledgeGap
from app.evolution.flags import writes_enabled
from app.evolution.feedback.state import is_unresolved, unresolved_filter
from app.evolution.gap.decisions import ignored_contexts
from app.evolution.issue_groups import ISSUE_SCAN_LIMIT, context_key
from app.evolution.repositories import evolution_repo
from app.rag.query.greetings import is_plain_greeting
from app.rag.query.business import classify_business
from app.shared.config import settings
from app.shared.clients.mongo import ensure_gap_feedback_index
from app.shared.runtime.logger import logger

# 聚类窗口内视为同一缺口（秒）
_DEDUP_WINDOW = 3600

# 扫描游标（进程内）：按 ``ts`` **升序**推进。
# 真实问题：早期实现 `.sort("ts", -1).limit(batch)` 只取「最新 batch 条」，反馈事件一多，
# 更早的未解决信号就永远排不进窗口（永远扫不到）。改为升序 + 游标前进后，窗口内的信号
# 一定会在有限轮次内被逐条看到；游标用尽（窗口内没有更新的信号）时回到窗口起点复扫，
# 已处理反馈有持久标记。游标重置或反馈时间更新不会重复登记。
_CURSOR: dict[str, float] = {"ts": 0.0}
_CURSOR_EPSILON = 1e-6  # 冷启动/复扫时包含窗口边界上的事件


def reset_scan_cursor() -> None:
    """重置扫描游标（测试与排障用）。"""
    _CURSOR["ts"] = 0.0


def _duplicate_gap(event: dict) -> dict | None:
    """同一已知上下文才可持久合并。未知上下文保留旧的同题窗口规则。"""
    text = str(event.get("query") or "").strip()
    if not text:
        return None
    key = context_key(event)
    query = {
        "query": text, "status": {"$nin": ["resolved", "rejected", "dismissed", "removed", "deprecated"]},
        "$or": [{"status": "pending"}, {"ts": {"$gte": (event.get("ts") or 0) - _DEDUP_WINDOW}}],
    }
    if key is not None:
        query["session_id"] = key[0]
    rows = evolution_repo.k_gaps.find(query).sort("ts", -1).limit(ISSUE_SCAN_LIMIT + 1)
    for index, row in enumerate(rows):
        if index == ISSUE_SCAN_LIMIT:
            raise RuntimeError("同题缺口超过合并检查上限")
        if context_key(row) != key:
            continue
        # 旧版本只完成待办而未回写缺口；精确核对绑定后排除已完成目标。
        if row.get("status") == "strategy_pending" and ObjectId.is_valid(row.get("issue_id")):
            completed = evolution_repo.query_issues.find_one({
                "_id": ObjectId(row["issue_id"]), "gap_id": str(row["_id"]),
                "session_id": row.get("session_id"), "status": "resolved",
            }, {"_id": 1})
            if completed:
                continue
        return row
    return None


def _feedback_gap_id(event: dict) -> ObjectId:
    identity = ["answer", event.get("session_id", ""), event["answer_id"]] if event.get("answer_id") \
        else ["event", str(event["_id"])]
    key = json.dumps(identity, ensure_ascii=False)
    return ObjectId(hashlib.sha256(("feedback-gap:" + key).encode()).hexdigest()[:24])


def _acknowledge(event: dict, *, gap_id: str = "", issue_id: str = "") -> None:
    evolution_repo.fb_events.update_one({"_id": event["_id"], "ts": event["ts"]}, {"$set": {
        "gap_scanned_at": time.time(), "gap_scan_id": gap_id, "gap_scan_issue_id": issue_id,
    }})


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
    user_bad = is_unresolved(fb_doc)
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
        gap_kind=classify_gap(fb_doc),
        business_tag=fb_doc.get("business_tag") or classify_business(fb_doc.get("query") or ""),
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
    if not writes_enabled():
        return []
    gaps: list[KnowledgeGap] = []
    try:
        ensure_gap_feedback_index(evolution_repo.k_gaps)
        since = time.time() - settings.evolution.observe_window_days * 86400
        cursor_ts = max(_CURSOR["ts"], since - _CURSOR_EPSILON)
        # 解决状态优先于历史点踩。持久标记保留在反馈文档中，不受 ts 更新影响。
        events = (
            evolution_repo.fb_events.find({
                "ts": {"$gt": cursor_ts},
                "gap_scanned_at": {"$exists": False},
                **unresolved_filter(),
            })
            .sort("ts", 1)
            .limit(batch)
        )
        events = list(events)
        decisions = ignored_contexts(events)
        scanned = 0
        last_ts = _CURSOR["ts"]
        for doc in events:
            scanned += 1
            last_ts = float(doc.get("ts") or 0.0)
            # 兼容修复前误写入的闲聊信号。保留原事件，不制造知识缺口。
            if doc.get("intent") == "chitchat" or is_plain_greeting(doc.get("query", "")):
                continue
            if context_key(doc) in decisions:
                continue
            current = evolution_repo.fb_events.find_one({"_id": doc["_id"]})
            if not current or not is_unresolved(current):
                continue
            doc = current
            # 兼容旧记录：同一回答已有完成待办时，不把旧信号重新派单。
            if doc.get("answer_id") and doc.get("session_id"):
                completed = evolution_repo.query_issues.find_one({
                    "session_id": doc["session_id"], "answer_id": doc["answer_id"], "status": "resolved",
                }, {"_id": 1})
                if completed:
                    _acknowledge(doc, issue_id=str(completed["_id"]))
                    continue
            oid = _feedback_gap_id(doc)
            key = str(oid)
            existing = evolution_repo.k_gaps.find_one({"_id": oid}) or evolution_repo.k_gaps.find_one({"source_feedback_keys": key})
            if existing:
                _acknowledge(doc, gap_id=str(existing["_id"]))
                continue
            duplicate = _duplicate_gap(doc)
            if duplicate:
                # 来源关联与合并在同一文档原子提交。确认失败后仍可查回来源。
                try:
                    linked = evolution_repo.k_gaps.update_one({"_id": duplicate["_id"], "status": duplicate["status"]},
                        {"$addToSet": {"source_feedback_keys": key}})
                except DuplicateKeyError:
                    winner = evolution_repo.k_gaps.find_one({"source_feedback_keys": key})
                    if not winner:
                        raise
                    _acknowledge(doc, gap_id=str(winner["_id"]))
                    continue
                if linked.matched_count:
                    _acknowledge(doc, gap_id=str(duplicate["_id"]))
                continue
            gap = detect_and_classify(doc, transcript_slice=doc.get("query", ""))
            if gap.status == "candidate":
                payload = {**gap.model_dump(), "source_feedback_keys": [key],
                           "source_feedback_id": str(doc["_id"]), "source_answer_id": doc.get("answer_id") or ""}
                try:
                    inserted = evolution_repo.k_gaps.update_one({"_id": oid}, {"$setOnInsert": payload}, upsert=True)
                except DuplicateKeyError:
                    # 来源可能已由另一进程并入别的缺口。
                    winner = evolution_repo.k_gaps.find_one({"source_feedback_keys": key})
                    if not winner:
                        raise
                    _acknowledge(doc, gap_id=str(winner["_id"]))
                    continue
                _acknowledge(doc, gap_id=str(oid))
                if inserted.upserted_id is not None:
                    gap.gap_id = str(oid)
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


def classify_gap(event: dict[str, Any]) -> str:
    """要判为知识缺失，必须存在明确的来源缺失。"""
    if event.get("gap_kind") in {"retrieval_miss", "reading_miss", "generation_error", "knowledge_missing"}:
        return event["gap_kind"]
    if event.get("source_answer_exists") is False:
        return "knowledge_missing"
    if event.get("evidence_sufficient") is True:
        return "generation_error"
    if event.get("read_chunk_ids") or event.get("cited_chunk_ids"):
        return "reading_miss"
    return "retrieval_miss"
