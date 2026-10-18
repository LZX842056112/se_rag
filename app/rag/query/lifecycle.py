"""单进程查询准入。先登记任务，再返回流式响应。"""
from __future__ import annotations

import threading
import uuid

from app.api.errors import ApiError
from app.shared.utils.sse_broker import reset_channel, ensure_channel
from app.shared.utils.task_state import clear_task, set_task_query, set_task_turn, update_task_status, TASK_STATUS_PENDING

_lock = threading.Lock()
_active: dict[str, str] = {}


def reserve_query(session_id: str, query: str) -> str:
    with _lock:
        if session_id in _active:
            raise ApiError("query_in_progress", "当前会话仍在处理上一条问题", 409)
        turn_id = uuid.uuid4().hex
        _active[session_id] = turn_id
        try:
            clear_task(session_id)
            reset_channel(session_id)
            ensure_channel(session_id)
            set_task_query(session_id, query)
            set_task_turn(session_id, turn_id)
            update_task_status(session_id, TASK_STATUS_PENDING)
        except BaseException:
            _active.pop(session_id, None)
            raise
        return turn_id


def release_query(session_id: str, turn_id: str) -> None:
    with _lock:
        if _active.get(session_id) == turn_id:
            _active.pop(session_id, None)


def query_is_active(session_id: str) -> bool:
    with _lock:
        return session_id in _active
