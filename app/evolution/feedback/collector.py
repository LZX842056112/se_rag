"""反馈采集：显式反馈（POST /api/evolution/feedback）与读链路会话信号 → ``fb_events``。"""
from __future__ import annotations

import time
import hashlib
import json
from typing import Any
from pymongo.errors import DuplicateKeyError

from app.evolution.models import FeedbackEvent
from app.evolution.flags import writes_enabled
from app.evolution.repositories import evolution_repo
from app.rag.query.business import classify_business
from app.shared.config import settings
from app.shared.clients.history_repository import history_repository
from app.shared.runtime.logger import logger
from app.shared.utils.answer import is_no_answer

# 幂等窗口：同一 session/query/thumbs/source 在该窗口内不重复落库，防信号放大
_DEDUP_WINDOW_SECONDS = 30


def _upsert_answer_feedback(collection, selector: dict, payload: dict, session_id: str, answer_id: str) -> None:
    """新事件共用确定性主键；旧事件按原条件更新，无需迁移历史。"""
    key = 'answer_' + hashlib.sha256(json.dumps([session_id, answer_id]).encode()).hexdigest()
    try:
        collection.update_one(selector, {'$set': payload, '$setOnInsert': {'_id': key}}, upsert=True)
    except DuplicateKeyError:
        # 另一个进程先插入相同主键时，只合并本次提供的字段。
        collection.update_one(selector, {'$set': payload}, upsert=False)


def _emit(event: FeedbackEvent, *, explicit: bool = False) -> None:
    """幂等写入一条反馈事件。"""
    now = time.time()
    payload = event.document()
    if explicit:
        if event.resolved is None:
            payload.pop('resolved', None)
            payload.pop('adopt', None)
        if event.thumbs is None:
            payload.pop('thumbs', None)
    if event.answer_id:
        _upsert_answer_feedback(evolution_repo.fb_events,
            {"session_id": event.session_id, "answer_id": event.answer_id},
            payload, event.session_id, event.answer_id)
        return
    evolution_repo.fb_events.update_one(
        {
            "session_id": event.session_id,
            "answer_id": event.answer_id,
            "query": event.query,
            "thumbs": event.thumbs,
            "source": event.source,
            "ts": {"$gte": now - _DEDUP_WINDOW_SECONDS},
        },
        {"$setOnInsert": event.document()},
        upsert=True,
    )


def record_feedback(event: FeedbackEvent) -> None:
    """显式反馈入口；未开启自进化时仅幂等接受、不落库。"""
    answer = history_repository.get_answer(event.session_id, event.answer_id)
    if not answer or answer.get("intent") not in ("rag", "chitchat"):
        return
    query = answer.get("query") or answer.get("rewritten_query") or ""
    canonical = event.model_copy(update={
        "query": query,
        "business_tag": answer.get("business_tag") or classify_business(query),
        "rewritten_query": answer.get("rewritten_query") or "",
        "item_names": answer.get("item_names") or [],
        "cited_chunk_ids": [str(c["faq_id"]) for c in answer.get("citations", [])
                            if c.get("source") != "web" and c.get("faq_id")],
        "source": "evolution" if any(c.get("source") == "evolution" for c in answer.get("citations", [])) else "kb",
        "adopt": event.resolved,
        "ts": time.time(),
    })
    try:
        payload = {**canonical.document(), "intent": answer["intent"]}
        if event.resolved is None:
            payload.pop("resolved", None)
            payload.pop("adopt", None)
        if event.thumbs is None:
            payload.pop("thumbs", None)
        _upsert_answer_feedback(evolution_repo.query_feedback, {"answer_id": event.answer_id},
                                payload, event.session_id, event.answer_id)
        if writes_enabled() and answer["intent"] == "rag":
            _emit(canonical, explicit=True)
        if answer["intent"] == "rag" and (event.resolved is False or (event.thumbs or 0) < 0):
            from app.evolution.issues import open_issue
            open_issue({"session_id": event.session_id, "answer_id": event.answer_id,
                        "original_query": canonical.query,
                        "item_names": canonical.item_names,
                        "business_tag": answer.get("business_tag", ""),
                        "issue_type": "answer_error" if canonical.cited_chunk_ids else "missing_material"})
    except Exception as exc:
        logger.warning(f"记录显式反馈失败：{exc}")


def flush_session_signals(state: dict[str, Any]) -> None:
    """读链路会话结束信号：零命中 / 无检索直达 / 命中却答不出 → 写入 ``fb_events``。"""
    if not writes_enabled():
        return
    try:
        session_id = state.get("session_id")
        if not session_id:
            return
        signals = state.get("retrieval_signals") or {}
        no_answer = is_no_answer(state.get("answer"))
        if not (signals.get("zero_hit") or signals.get("no_retrieval") or no_answer):
            return
        _emit(FeedbackEvent(
            session_id=session_id,
            business_tag=state.get("business_tag") or classify_business(state.get("original_query") or ""),
            answer_id=state.get("answer_id") or "",
            query=state.get("original_query", ""),
            rewritten_query=state.get("rewritten_query", ""),
            cited_chunk_ids=state.get("cited_chunk_ids") or [],
            item_names=state.get("item_names") or [],
            adopt=False,
            thumbs=-1,
            source="evolution" if signals.get("evolution_hit") else "kb",
        ))
        diagnostics = state.get('agentic_diagnostics')
        if diagnostics and state.get('answer_id'):
            # 仅使用服务器推导的事实。显式的用户反馈会保留这些字段，
            # 因为 _emit 只会更新已知的反馈字段而不会删除它们。
            evolution_repo.fb_events.update_one(
                {'session_id': session_id, 'answer_id': state['answer_id']},
                {'$set': {key: diagnostics[key] for key in ('evidence_sufficient', 'read_chunk_ids')
                          if key in diagnostics}}, upsert=False)
        logger.info(f"会话 {session_id} 已写入未解决信号到 fb_events")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"写入会话信号失败：{exc}")
