"""多 worker 跨进程状态测试：后端选择、任务状态、SSE、会话锁、导入名额、调度器主节点。

全部用 fakeredis，不连接真实 Redis。
"""
from __future__ import annotations

import asyncio

import fakeredis
import pytest

from app.api.errors import ApiError
from app.rag.import_ import pipeline
from app.rag.query import lifecycle
from app.shared.clients import redis_gateway
from app.shared.config import settings
from app.shared.utils import sse_broker, task_state


class _FakeRequest:
    """最小 Request 替身：始终认为客户端在线。"""

    async def is_disconnected(self) -> bool:
        return False


@pytest.fixture
def fake_redis(monkeypatch):
    """切换到 Redis 后端，并注入 fakeredis。"""
    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(settings.runtime, "state_backend", "redis")
    monkeypatch.setattr(redis_gateway, "get_redis_client", lambda: client)
    redis_gateway.set_client_for_tests(client)
    try:
        yield client
    finally:
        redis_gateway.set_client_for_tests(None)
        client.flushall()


async def _collect(session_id: str, expected_events: int, timeout: float = 3.0) -> list[str]:
    """读取 SSE 报文直到收到指定数量的事件或超时。"""
    chunks: list[str] = []
    generator = sse_broker.stream_events(session_id, _FakeRequest())
    try:
        while len(chunks) < expected_events:
            chunk = await asyncio.wait_for(generator.__anext__(), timeout=timeout)
            chunks.append(chunk)
    except (asyncio.TimeoutError, StopAsyncIteration):
        pass
    finally:
        await generator.aclose()
    return chunks


# ---------------- 后端选择 ----------------

def test_backend_defaults_to_memory_without_url(monkeypatch):
    monkeypatch.setattr(settings.runtime, "state_backend", "auto")
    monkeypatch.setattr(settings.redis, "url", "")
    assert redis_gateway.backend_name() == "memory"


def test_backend_auto_uses_redis_when_url_configured(monkeypatch):
    monkeypatch.setattr(settings.runtime, "state_backend", "auto")
    monkeypatch.setattr(settings.redis, "url", "redis://127.0.0.1:6379/0")
    assert redis_gateway.backend_name() == "redis"


# ---------------- 任务状态 ----------------

def test_redis_task_state_roundtrip(fake_redis):
    task_state.clear_task("mw-state")
    task_state.set_task_query("mw-state", "烫金机怎么安装")
    task_state.set_task_turn("mw-state", "turn-1")
    task_state.update_task_status("mw-state", task_state.TASK_STATUS_PROCESSING)
    task_state.add_running_task("mw-state", "node_search_embedding")
    task_state.add_done_task("mw-state", "node_search_embedding")
    task_state.add_done_task("mw-state", "node_answer_output")

    snap = task_state.snapshot("mw-state")

    assert snap["status"] == task_state.TASK_STATUS_PROCESSING
    assert snap["query"] == "烫金机怎么安装"
    assert snap["turn_id"] == "turn-1"
    assert snap["running_list"] == []
    assert snap["done_list"] == ["切片搜索", "生成答案"]
    assert task_state.get_task_status("mw-state") == task_state.TASK_STATUS_PROCESSING

    task_state.set_task_error("mw-state", "mineru_failed", "解析失败")
    assert task_state.get_task_error("mw-state") == ("mineru_failed", "解析失败")

    task_state.clear_task("mw-state")
    assert task_state.snapshot("mw-state") == {}


# ---------------- SSE ----------------

def test_redis_sse_replays_events_published_before_subscribe(fake_redis):
    sse_broker.drop_channel("mw-sse")
    sse_broker.ensure_channel("mw-sse")
    sse_broker.publish("mw-sse", sse_broker.SSEEvent.PROGRESS, {"status": "processing"})
    sse_broker.publish("mw-sse", sse_broker.SSEEvent.FINAL, {"answer": "ok"})
    sse_broker.publish("mw-sse", sse_broker.SSEEvent.CLOSE, {})

    joined = "".join(asyncio.run(_collect("mw-sse", 4)))

    assert "event: ready" in joined
    assert "event: progress" in joined
    assert "event: final" in joined
    assert joined.index("event: final") < joined.index("event: close")
    assert "ok" in joined


def test_redis_sse_flushes_delta_before_final(fake_redis, monkeypatch):
    """增量合并不能把 delta 排到 final 之后。"""
    monkeypatch.setattr(settings.runtime, "sse_delta_flush_ms", 10000)
    sse_broker.drop_channel("mw-sse-order")
    sse_broker.ensure_channel("mw-sse-order")
    sse_broker.publish("mw-sse-order", sse_broker.SSEEvent.DELTA, {"delta": "你好"})
    sse_broker.publish("mw-sse-order", sse_broker.SSEEvent.FINAL, {"answer": "你好"})
    sse_broker.publish("mw-sse-order", sse_broker.SSEEvent.CLOSE, {})

    joined = "".join(asyncio.run(_collect("mw-sse-order", 4)))

    assert "event: delta" in joined
    assert joined.index("event: delta") < joined.index("event: final")


def test_redis_sse_missing_channel_reports_error(fake_redis):
    sse_broker.drop_channel("mw-sse-missing")
    chunks = asyncio.run(_collect("mw-sse-missing", 1))
    assert chunks and "stream_not_found" in chunks[0]


# ---------------- 会话并发锁 ----------------

def test_redis_session_lock_blocks_second_turn(fake_redis):
    turn_id = lifecycle.reserve_query("mw-lock", "第一个问题")
    assert lifecycle.query_is_active("mw-lock") is True

    with pytest.raises(ApiError) as exc:
        lifecycle.reserve_query("mw-lock", "第二个问题")
    assert exc.value.code == "query_in_progress"
    assert exc.value.status_code == 409

    lifecycle.release_query("mw-lock", turn_id)
    assert lifecycle.query_is_active("mw-lock") is False
    # 释放后可再次提交
    next_turn = lifecycle.reserve_query("mw-lock", "第三个问题")
    lifecycle.release_query("mw-lock", next_turn)


def test_redis_session_lock_release_ignores_wrong_turn(fake_redis):
    turn_id = lifecycle.reserve_query("mw-lock-2", "问题")
    lifecycle.release_query("mw-lock-2", "not-this-turn")
    assert lifecycle.query_is_active("mw-lock-2") is True
    lifecycle.release_query("mw-lock-2", turn_id)


# ---------------- 导入名额 ----------------

def test_redis_import_slots_enforce_global_limit(fake_redis, monkeypatch):
    monkeypatch.setattr(settings.import_, "max_concurrent_tasks", 2)
    pipeline._release_redis_slot("slot-a")
    pipeline._release_redis_slot("slot-b")
    pipeline._release_redis_slot("slot-c")

    assert pipeline._try_acquire_redis_slot("slot-a") is True
    assert pipeline._try_acquire_redis_slot("slot-b") is True
    assert pipeline._try_acquire_redis_slot("slot-c") is False

    pipeline._release_redis_slot("slot-a")
    assert pipeline._try_acquire_redis_slot("slot-c") is True


def test_redis_import_slot_expired_lease_is_reclaimed(fake_redis, monkeypatch):
    monkeypatch.setattr(settings.import_, "max_concurrent_tasks", 1)
    monkeypatch.setattr(settings.import_, "slot_lease_seconds", 1)
    pipeline._release_redis_slot("expired-holder")
    pipeline._release_redis_slot("new-holder")

    assert pipeline._try_acquire_redis_slot("expired-holder") is True
    # 手工把租约改成已过期
    fake_redis.zadd(pipeline._SLOTS_KEY, {"expired-holder": 0})
    assert pipeline._try_acquire_redis_slot("new-holder") is True


# ---------------- 调度器主节点 ----------------

def test_scheduler_leader_allows_only_one_owner(fake_redis):
    from app.evolution import leader

    fake_redis.delete(leader.LEADER_KEY)

    assert leader.try_acquire_leader("node-a") is True
    assert leader.try_acquire_leader("node-b") is False
    assert leader.renew_leader("node-a") is True
    assert leader.renew_leader("node-b") is False
