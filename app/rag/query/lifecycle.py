"""查询准入：同一会话同一时刻只允许一轮查询。

单 worker 用进程内字典；多 worker 用 Redis ``SET NX``，跨进程互斥。
"""
from __future__ import annotations

import threading
import uuid

from app.api.errors import ApiError
from app.shared.config import settings
from app.shared.utils.sse_broker import reset_channel, ensure_channel
from app.shared.utils.task_state import clear_task, set_task_query, set_task_turn, update_task_status, TASK_STATUS_PENDING

_lock = threading.Lock()
_active: dict[str, str] = {}

_LOCK_KEY_PREFIX = "query:active:"

# 仅当值仍属于本轮时才删除，避免误删下一轮的锁
_RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def _lock_key(session_id: str) -> str:
    return f"{_LOCK_KEY_PREFIX}{session_id}"


def _use_redis() -> bool:
    from app.shared.clients.redis_gateway import using_redis

    return using_redis()


def _redis_release(session_id: str, turn_id: str) -> None:
    """比对 turn_id 后释放 Redis 锁（异常只告警，TTL 会兜底）。"""
    try:
        from app.shared.clients.redis_gateway import get_redis_client

        get_redis_client().eval(_RELEASE_SCRIPT, 1, _lock_key(session_id), turn_id)
    except Exception as exc:  # noqa: BLE001 - 释放失败由 TTL 兜底
        from app.shared.runtime.logger import logger

        logger.warning(f"释放查询锁失败（忽略）：{session_id}：{exc}")


def _reserve_redis(session_id: str, query: str) -> str:
    from app.shared.clients.redis_gateway import get_redis_client

    turn_id = uuid.uuid4().hex
    acquired = get_redis_client().set(
        _lock_key(session_id),
        turn_id,
        nx=True,
        ex=settings.runtime.query_turn_lock_ttl_seconds,
    )
    if not acquired:
        raise ApiError("query_in_progress", "当前会话仍在处理上一条问题", 409)
    try:
        clear_task(session_id)
        reset_channel(session_id)
        ensure_channel(session_id)
        set_task_query(session_id, query)
        set_task_turn(session_id, turn_id)
        update_task_status(session_id, TASK_STATUS_PENDING)
    except BaseException:
        _redis_release(session_id, turn_id)
        raise
    return turn_id


def reserve_query(session_id: str, query: str) -> str:
    if _use_redis():
        return _reserve_redis(session_id, query)
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
    if _use_redis():
        _redis_release(session_id, turn_id)
        return
    with _lock:
        if _active.get(session_id) == turn_id:
            _active.pop(session_id, None)


def query_is_active(session_id: str) -> bool:
    if _use_redis():
        try:
            from app.shared.clients.redis_gateway import get_redis_client

            return bool(get_redis_client().exists(_lock_key(session_id)))
        except Exception:  # noqa: BLE001 - 读不到按「未在跑」，避免阻断清空历史等操作
            return False
    with _lock:
        return session_id in _active
