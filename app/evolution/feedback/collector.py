"""反馈采集：显式反馈（POST /api/evolution/feedback）与读链路会话信号 → ``fb_events``。"""
from __future__ import annotations

import time
from typing import Any

from app.evolution.models import FeedbackEvent
from app.evolution.repositories import evolution_repo
from app.shared.config import settings
from app.shared.clients.history_repository import history_repository
from app.shared.runtime.logger import logger
from app.shared.utils.answer import is_no_answer

# 幂等窗口：同一 session/query/thumbs/source 在该窗口内不重复落库，防信号放大
_DEDUP_WINDOW_SECONDS = 30


def _emit(event: FeedbackEvent) -> None:
    """幂等写入一条反馈事件。"""
    now = time.time()
    if event.answer_id:
        evolution_repo.fb_events.update_one(
            {"session_id": event.session_id, "answer_id": event.answer_id},
            {"$set": event.document()}, upsert=True,
        )
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
    canonical = event.model_copy(update={
        "query": answer.get("query") or answer.get("rewritten_query") or "",
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
        if event.thumbs is None:
            payload.pop("thumbs", None)
        evolution_repo.query_feedback.update_one(
            {"answer_id": event.answer_id}, {"$set": payload}, upsert=True,
        )
        if settings.evolution.enabled and answer["intent"] == "rag":
            _emit(canonical)
        if answer["intent"] == "rag" and (event.resolved is False or (event.thumbs or 0) < 0):
            from app.evolution.issues import open_issue
            open_issue({"session_id": event.session_id, "answer_id": event.answer_id,
                        "original_query": canonical.query,
                        "business_tag": answer.get("business_tag", ""),
                        "issue_type": "answer_error" if canonical.cited_chunk_ids else "missing_material"})
    except Exception as exc:
        logger.warning(f"记录显式反馈失败：{exc}")


def flush_session_signals(state: dict[str, Any]) -> None:
    """读链路会话结束信号：零命中 / 无检索直达 / 命中却答不出 → 写入 ``fb_events``。"""
    if not settings.evolution.enabled:
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
            answer_id=state.get("answer_id") or "",
            query=state.get("original_query", ""),
            rewritten_query=state.get("rewritten_query", ""),
            cited_chunk_ids=state.get("cited_chunk_ids") or [],
            item_names=state.get("item_names") or [],
            adopt=False,
            thumbs=-1,
            source="evolution" if signals.get("evolution_hit") else "kb",
        ))
        logger.info(f"会话 {session_id} 已写入未解决信号到 fb_events")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"写入会话信号失败：{exc}")
