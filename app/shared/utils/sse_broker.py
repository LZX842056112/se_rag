"""
SSE 事件推送（会话级广播）。

设计要点（相对旧实现的改进）：
1. 旧实现用 ``queue.Queue`` + ``run_in_executor`` 阻塞轮询，每个 SSE 连接会长期
   占用一个线程池线程；新实现改用 ``asyncio.Queue`` + ``call_soon_threadsafe``，
   连接不再占用线程。
2. 旧实现要求「先建队列再连流」，存在 POST /query 与 /stream 的竞态；新实现由
   生产者或订阅者任意一方先 ``ensure_channel``，订阅前的消息进入有界缓冲并在
   订阅时回放，保证 ``final`` 不会丢。
3. 去掉调试 ``print``，异常统一走 ``error`` 事件；会话通道空闲超时自动回收。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import Request

# 订阅前的事件缓冲上限（超出丢弃最旧，防止无人订阅时无限增长）
_BUFFER_SIZE = 512
# 通道空闲回收时间（秒）：超过该时间没有读写的通道会被清理
_CHANNEL_IDLE_SECONDS = 30 * 60


class SSEEvent:
    """SSE 事件名常量（前端按同名事件注册监听）。"""

    READY = "ready"        # 连接建立
    PROGRESS = "progress"  # 节点进度：status / done_list / running_list
    DELTA = "delta"        # LLM 流式增量
    FINAL = "final"        # 最终答案（含 citations / groundedness / image_urls）
    ERROR = "error"        # 错误
    CLOSE = "close"        # 服务端关闭连接


@dataclass
class _Channel:
    """单个会话的事件通道。"""

    pending: deque = field(default_factory=lambda: deque(maxlen=_BUFFER_SIZE))
    queue: Optional[asyncio.Queue] = None
    loop: Optional[asyncio.AbstractEventLoop] = None
    touched: float = field(default_factory=time.time)


_channels: Dict[str, _Channel] = {}
_lock = threading.Lock()


def _prune_locked(now: float) -> None:
    """回收长时间空闲的通道（调用方需持锁）。"""
    stale = [sid for sid, ch in _channels.items() if now - ch.touched > _CHANNEL_IDLE_SECONDS]
    for sid in stale:
        _channels.pop(sid, None)


def ensure_channel(session_id: str) -> None:
    """确保会话通道存在（生产者与订阅者都可安全调用）。"""
    if not session_id:
        return
    now = time.time()
    with _lock:
        _prune_locked(now)
        channel = _channels.get(session_id)
        if channel is None:
            _channels[session_id] = _Channel()
        else:
            channel.touched = now


def drop_channel(session_id: str) -> None:
    """移除会话通道（流结束时调用）。"""
    with _lock:
        _channels.pop(session_id, None)


def publish(session_id: str, event: str, data: Dict[str, Any]) -> None:
    """推送事件（线程安全，可从同步业务代码调用）。

    尚无订阅者时事件进入有界缓冲，订阅后回放；已有订阅者时直接投递。
    """
    if not session_id:
        return
    ensure_channel(session_id)
    message = {"event": event, "data": data}
    with _lock:
        channel = _channels.get(session_id)
        if channel is None:
            return
        channel.touched = time.time()
        queue, loop = channel.queue, channel.loop
        if queue is None or loop is None:
            channel.pending.append(message)
            return
    try:
        loop.call_soon_threadsafe(queue.put_nowait, message)
    except RuntimeError:
        # 事件循环已关闭：连接已断开，忽略本次推送
        drop_channel(session_id)


def _pack(event: str, data: Dict[str, Any]) -> str:
    """打包为 SSE 报文。"""
    payload = json.dumps(data, ensure_ascii=False, default=_json_default)
    return f"event: {event}\ndata: {payload}\n\n"


def _json_default(obj: Any) -> Any:
    """JSON 序列化兜底：Pydantic 模型转 dict。"""
    dump = getattr(obj, "model_dump", None)
    if callable(dump):
        return dump()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


async def stream_events(session_id: str, request: Request) -> AsyncIterator[str]:
    """SSE 生成器：订阅会话通道并把事件写回客户端。"""
    with _lock:
        channel = _channels.get(session_id)
        if channel is not None:
            queue: asyncio.Queue = asyncio.Queue()
            channel.queue = queue
            channel.loop = asyncio.get_running_loop()
            channel.touched = time.time()
            pending = list(channel.pending)
            channel.pending.clear()

    if channel is None:
        # 通道不存在：说明本次会话未发起查询或已被回收，显式下发错误
        yield _pack(SSEEvent.ERROR, {"code": "stream_not_found", "message": "流式会话不存在或已结束"})
        return

    try:
        yield _pack(SSEEvent.READY, {})
        for message in pending:
            yield _pack(message["event"], message["data"])

        while True:
            if await request.is_disconnected():
                return
            try:
                message = await asyncio.wait_for(queue.get(), timeout=5.0)
            except asyncio.TimeoutError:
                continue  # 仅用于周期性检查客户端是否断开

            event = message["event"]
            if event == SSEEvent.CLOSE:
                yield _pack(SSEEvent.CLOSE, message["data"])
                return
            yield _pack(event, message["data"])
    except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
        return
    finally:
        drop_channel(session_id)
