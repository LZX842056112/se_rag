"""刷新后接管进行中查询的回归测试。

回归背景：对话进行中刷新页面，这一轮问答会凭空消失。原因是两条链路都断了——
1. 回答只在答案生成完才落库，刷新时历史里只有提问、没有回答，``loadHistory`` 无可回显；
2. SSE 通道在客户端断开时被无条件回收，重连既拿不到 pending 里的 ``final``，
   也接不上后续进度。

修复：新增 ``GET /api/query/status/{sid}`` 进度快照接口，前端据此重建阶段进度并
重新订阅 SSE；同时让 SSE 通道在「任务仍在跑」时不被断开回收。
"""
from __future__ import annotations

import types

from app.api.routers import query as query_router
from app.rag.query import pipeline
from app.shared.utils import sse_broker, task_state


def _reset(*session_ids: str) -> None:
    for session_id in session_ids:
        task_state.clear_task(session_id)


# ==================== 进度快照接口 ====================

def test_status_endpoint_reports_idle_for_unknown_session():
    """没有在册任务时必须返回空状态，前端据此判定「无进行中查询」。"""
    _reset("resume-idle")
    payload = query_router.query_status("resume-idle")

    assert payload.session_id == "resume-idle"
    assert payload.status == ""
    assert payload.done_list == []
    assert payload.running_list == []
    assert payload.query == ""


def test_status_endpoint_reports_in_flight_progress():
    """进行中查询必须回传 done/running 与提问原文，供前端重建进度条与提问气泡。"""
    session_id = "resume-running"
    _reset(session_id)
    task_state.set_task_query(session_id, "烫金机怎么安装")
    task_state.update_task_status(session_id, task_state.TASK_STATUS_PROCESSING)
    task_state.add_done_task(session_id, "node_item_name_confirm")
    task_state.add_running_task(session_id, "node_search_embedding")

    payload = query_router.query_status(session_id)

    assert payload.status == task_state.TASK_STATUS_PROCESSING
    assert payload.done_list == ["确认问题产品"]
    assert payload.running_list == ["切片搜索"]
    assert payload.query == "烫金机怎么安装"


def test_status_endpoint_surfaces_failure_classification():
    """失败任务要回传分类，前端才能在重连前就给出原因，而不是一直等待中。"""
    session_id = "resume-failed"
    _reset(session_id)
    task_state.update_task_status(session_id, task_state.TASK_STATUS_FAILED)
    task_state.set_task_error(session_id, "llm_timeout", "模型调用超时")

    payload = query_router.query_status(session_id)

    assert payload.status == task_state.TASK_STATUS_FAILED
    assert payload.error_code == "llm_timeout"
    assert payload.message == "模型调用超时"


def test_snapshot_expires_stale_processing_record(monkeypatch):
    """超期记录必须被回收：否则进程重启前遗留的 processing 会让前端永远等待。"""
    import time

    from app.shared.config import settings

    session_id = "resume-stale"
    _reset(session_id)
    task_state.update_task_status(session_id, task_state.TASK_STATUS_PROCESSING)

    monkeypatch.setattr(settings.runtime, "task_state_ttl_seconds", 0)
    time.sleep(0.01)  # 让 updated_at 明确早于读取时刻（时钟粒度可能吞掉 0 间隔）

    assert task_state.snapshot(session_id) == {}
    assert query_router.query_status(session_id).status == ""


# ==================== 通道不得在任务进行中被回收 ====================

def test_task_in_flight_only_for_running_status():
    """只有 pending/processing 算「在跑」；completed/failed/空串都必须视为结束。"""
    session_id = "resume-inflight"
    _reset(session_id)
    assert sse_broker._task_in_flight(session_id) is False  # 无记录

    task_state.update_task_status(session_id, task_state.TASK_STATUS_PROCESSING)
    assert sse_broker._task_in_flight(session_id) is True

    task_state.update_task_status(session_id, task_state.TASK_STATUS_COMPLETED)
    assert sse_broker._task_in_flight(session_id) is False

    task_state.update_task_status(session_id, task_state.TASK_STATUS_FAILED)
    assert sse_broker._task_in_flight(session_id) is False


def test_disconnect_keeps_channel_while_task_running():
    """守卫：断开连接的清理分支必须按「任务是否在跑」决定是否回收通道。"""
    import inspect

    source = inspect.getsource(sse_broker.stream_events)
    assert "_task_in_flight(session_id)" in source, (
        "stream_events 又无条件回收通道，刷新后重连拿不到 pending 中的 final"
    )
    assert "if not _task_in_flight(session_id):" in source
    # 断开后必须解绑订阅者，否则事件会被投递到无人读取的旧队列而静默丢失
    assert "channel.queue = None" in source and "channel.loop = None" in source


# ==================== 断线重连的事件续传（端到端） ====================

class _FakeRequest:
    """假请求：``is_disconnected`` 恒为 False，由调用方显式 aclose 模拟页面刷新。"""

    async def is_disconnected(self) -> bool:
        return False


async def _read_stream(session_id: str, count: int) -> list[str]:
    """读 ``count`` 个 SSE 报文后主动关闭生成器（等价于客户端断开）。"""
    agen = sse_broker.stream_events(session_id, _FakeRequest())
    chunks: list[str] = []
    try:
        async for chunk in agen:
            chunks.append(chunk)
            if len(chunks) >= count:
                break
    finally:
        await agen.aclose()
    return chunks


def test_refresh_replays_buffered_events_on_reconnect():
    """刷新断开后，后台继续跑完的 final/close 必须在重连时补回。"""
    import asyncio

    session_id = "resume-sse"
    _reset(session_id)
    sse_broker.drop_channel(session_id)

    task_state.update_task_status(session_id, task_state.TASK_STATUS_PROCESSING)
    sse_broker.publish(session_id, sse_broker.SSEEvent.PROGRESS, {"status": "processing"})

    # 首次订阅读到 ready + progress 后断开（等价于用户刷新页面）
    first = asyncio.run(_read_stream(session_id, 2))
    assert "event: ready" in first[0]
    assert "event: progress" in first[1]
    assert session_id in sse_broker._channels, "任务仍在跑，通道不得被回收"

    # 断开期间后台跑完：先置完成状态再推 final/close（与 pipeline 的实际顺序一致）
    task_state.update_task_status(session_id, task_state.TASK_STATUS_COMPLETED)
    sse_broker.publish(session_id, sse_broker.SSEEvent.FINAL, {"answer": "答案"})
    sse_broker.publish(session_id, sse_broker.SSEEvent.CLOSE, {})

    # 重连：必须补回缓冲的 final 与 close，否则刷新后这一轮问答仍然消失
    joined = "".join(asyncio.run(_read_stream(session_id, 3)))
    assert "event: final" in joined, "重连未补回 final，回答会永久缺失"
    assert "答案" in joined
    assert "event: close" in joined, "重连未补回 close，发送按钮会一直禁用"

    assert session_id not in sse_broker._channels, "任务已结束，通道应被回收"


def test_channel_dropped_on_disconnect_after_task_finished():
    """任务已结束时断开照旧回收通道，避免通道与缓冲长期堆积。"""
    import asyncio

    session_id = "resume-sse-done"
    _reset(session_id)
    sse_broker.drop_channel(session_id)

    task_state.update_task_status(session_id, task_state.TASK_STATUS_COMPLETED)
    sse_broker.publish(session_id, sse_broker.SSEEvent.PROGRESS, {"status": "completed"})

    asyncio.run(_read_stream(session_id, 2))
    assert session_id not in sse_broker._channels


def test_reset_channel_keeps_live_subscriber():
    """守卫：清缓冲不得解绑订阅者。

    ``reset_channel`` 的调用时机在「后台任务启动」与「前端连流」之间：
    若连订阅关系一起清掉，先连上的客户端会被改投到缓冲，一个事件都收不到。
    """
    import inspect

    source = inspect.getsource(sse_broker.reset_channel)
    assert "channel.pending.clear()" in source
    assert "channel.queue = None" not in source, "reset_channel 不得解绑订阅者"


def test_new_query_drops_stale_buffered_events():
    """守卫：新一轮查询必须清掉上一轮残留的 final/close。

    否则重连会在那个旧 close 处提前结束，本轮回答永远收不到。
    """
    session_id = "resume-stale-pending"
    _reset(session_id)
    sse_broker.drop_channel(session_id)

    sse_broker.publish(session_id, sse_broker.SSEEvent.FINAL, {"answer": "上一轮"})
    sse_broker.publish(session_id, sse_broker.SSEEvent.CLOSE, {})
    assert len(sse_broker._channels[session_id].pending) == 2

    sse_broker.reset_channel(session_id)

    assert list(sse_broker._channels[session_id].pending) == []
    assert session_id in sse_broker._channels, "reset_channel 只清缓冲，不得删除通道"
    sse_broker.drop_channel(session_id)


# ==================== 查询开始时记录提问原文 ====================

def test_pipeline_records_query_text_for_resume(monkeypatch):
    """查询开始就要记下提问原文：刷新时它可能还没落库到会话历史。"""
    session_id = "resume-pipeline"
    _reset(session_id)
    monkeypatch.setattr(pipeline, "query_app", types.SimpleNamespace(invoke=lambda state: dict(state)))

    result = pipeline.invoke_query_graph(
        session_id=session_id, original_query="烫金机怎么安装", is_stream=False
    )

    assert result is not None
    assert task_state.snapshot(session_id)["query"] == "烫金机怎么安装"
    sse_broker.drop_channel(session_id)
    _reset(session_id)


def test_non_stream_query_also_leaves_final_in_channel(monkeypatch):
    """非流式查询同样要留下 final/close。

    页面刷新会同时丢掉 POST 响应与 SSE 连接；通道里没有本轮事件时，
    非流式这一轮同样会凭空消失（而「流式输出」开关是用户可关的）。
    """
    session_id = "resume-nonstream"
    _reset(session_id)
    sse_broker.drop_channel(session_id)
    monkeypatch.setattr(pipeline, "query_app", types.SimpleNamespace(invoke=lambda state: dict(state)))

    pipeline.invoke_query_graph(session_id=session_id, original_query="烫金机怎么安装", is_stream=False)

    events = [message["event"] for message in sse_broker._channels[session_id].pending]
    assert sse_broker.SSEEvent.FINAL in events, "非流式未留下 final，刷新后无法续上答案"
    assert sse_broker.SSEEvent.CLOSE in events, "非流式未留下 close，重连后按钮会一直禁用"
    sse_broker.drop_channel(session_id)
    _reset(session_id)
