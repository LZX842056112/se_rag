"""
反馈采集：接纳显式反馈（/feedback）与读链路会话信号，写入 fb_events。
失败不影响主链路。
"""
from __future__ import annotations

import time
from typing import Any

from app.evolution.config import evolution_config
from app.evolution.models import FeedbackEvent
from app.evolution.repositories import get_evolution_mongo_tool
from app.shared.runtime.logger import logger


def _emit(event: FeedbackEvent) -> None:
    """幂等写入：30s 窗口内同 session/query/thumbs/source 不重复落库，防信号放大。"""
    repo = get_evolution_mongo_tool()
    now = time.time()
    doc = event.document()
    repo.fb_events.update_one(
        {
            "session_id": event.session_id,
            "query": event.query,
            "thumbs": event.thumbs,
            "source": event.source,
            "ts": {"$gte": now - 30},
        },
        {"$setOnInsert": doc},
        upsert=True,
    )


def record_feedback(event: FeedbackEvent) -> None:
    """显式反馈入口（POST /feedback 调用）。"""
    if not getattr(evolution_config, "enabled", False):
        return
    try:
        _emit(event)
    except Exception as e:
        logger.warning(f"记录显式反馈失败: {e}")


def flush_session_signals(state: dict[str, Any]) -> None:
    """读链路会话结束信号：零命中 / 无检索直达 → 写入 fb_events（供 gap 消费）。"""
    if not getattr(evolution_config, "enabled", False):
        return
    try:
        session_id = state.get("session_id")
        if not session_id:
            return
        signals = state.get("retrieval_signals") or {}
        # 检索到证据模型仍答不出（answer_out.prompt 固定话术）== 知识缺口：虽非零命中，但供给未达标
        answer = state.get("answer") or ""
        no_answer = any(marker in answer for marker in ("未查询到该问题相关信息", "无法作答"))
        if not (signals.get("zero_hit") or signals.get("no_retrieval") or no_answer):
            return
        source = "evolution" if signals.get("evolution_hit") else "kb"
        _emit(FeedbackEvent(
            session_id=session_id,
            query=state.get("original_query", ""),
            rewritten_query=state.get("rewritten_query", ""),
            cited_chunk_ids=state.get("cited_chunk_ids") or [],
            item_names=state.get("item_names") or [],
            adopt=False,
            thumbs=-1,
            source=source,
        ))
        logger.info(f"session[{session_id}] 已写入未解决信号到 fb_events")
    except Exception as e:
        logger.warning(f"写入会话信号失败: {e}")