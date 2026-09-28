"""SSE 事件通道测试：订阅前缓冲回放、订阅后投递、close 结束流。"""
from __future__ import annotations

import asyncio

from app.shared.utils.sse_broker import SSEEvent, drop_channel, ensure_channel, publish, stream_events


class _FakeRequest:
    """最小的 Request 替身：始终认为客户端在线。"""

    async def is_disconnected(self) -> bool:
        return False


async def _collect(session_id: str, expected_events: int, timeout: float = 3.0) -> list[str]:
    """读取 SSE 报文直到收到指定数量的事件或超时。"""
    chunks: list[str] = []
    generator = stream_events(session_id, _FakeRequest())
    try:
        while len(chunks) < expected_events:
            chunk = await asyncio.wait_for(generator.__anext__(), timeout=timeout)
            chunks.append(chunk)
    except (asyncio.TimeoutError, StopAsyncIteration):
        pass
    finally:
        await generator.aclose()
    return chunks


def test_buffered_events_are_replayed_after_subscribe():
    """先 push 后连流：ready 之后应立即收到此前缓冲的事件。"""
    drop_channel("sess-buffer")
    ensure_channel("sess-buffer")
    publish("sess-buffer", SSEEvent.PROGRESS, {"status": "processing", "done_list": [], "running_list": []})
    publish("sess-buffer", SSEEvent.FINAL, {"answer": "ok"})
    publish("sess-buffer", SSEEvent.CLOSE, {})

    chunks = asyncio.run(_collect("sess-buffer", 4))
    joined = "".join(chunks)
    assert "event: ready" in joined
    assert "event: progress" in joined
    assert "event: final" in joined
    assert "event: close" in joined


def test_missing_channel_reports_error_event():
    drop_channel("sess-missing")
    chunks = asyncio.run(_collect("sess-missing", 1))
    assert chunks and "event: error" in chunks[0]


def test_publish_after_subscribe_is_delivered():
    drop_channel("sess-live")
    ensure_channel("sess-live")

    async def scenario() -> list[str]:
        generator = stream_events("sess-live", _FakeRequest())
        received: list[str] = []
        received.append(await asyncio.wait_for(generator.__anext__(), timeout=3))  # ready
        # 订阅建立后再推送，验证 call_soon_threadsafe 投递路径
        publish("sess-live", SSEEvent.DELTA, {"delta": "你好"})
        received.append(await asyncio.wait_for(generator.__anext__(), timeout=3))
        publish("sess-live", SSEEvent.CLOSE, {})
        received.append(await asyncio.wait_for(generator.__anext__(), timeout=3))
        await generator.aclose()
        return received

    joined = "".join(asyncio.run(scenario()))
    assert "event: delta" in joined and "你好" in joined
    assert "event: close" in joined
