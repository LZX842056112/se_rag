"""反馈采集：显式反馈（POST /api/evolution/feedback）与读链路会话信号 → ``fb_events``。"""
from __future__ import annotations

import time
from typing import Any

from app.evolution.models import FeedbackEvent
from app.evolution.repositories import evolution_repo
from app.shared.config import settings
from app.shared.runtime.logger import logger

# 幂等窗口：同一 session/query/thumbs/source 在该窗口内不重复落库，防信号放大
_DEDUP_WINDOW_SECONDS = 30
# 兜底话术：检索到证据但模型仍答不出，同样视为知识缺口
_NO_ANSWER_MARKERS = ("未查询到该问题相关信息", "无法作答")


def _emit(event: FeedbackEvent) -> None:
    """幂等写入一条反馈事件。"""
    now = time.time()
    evolution_repo.fb_events.update_one(
        {
            "session_id": event.session_id,
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
    if not settings.evolution.enabled:
        return
    try:
        _emit(event)
    except Exception as exc:  # noqa: BLE001 - 反馈失败不影响主链路
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
        answer = state.get("answer") or ""
        no_answer = any(marker in answer for marker in _NO_ANSWER_MARKERS)
        if not (signals.get("zero_hit") or signals.get("no_retrieval") or no_answer):
            return
        _emit(FeedbackEvent(
            session_id=session_id,
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
