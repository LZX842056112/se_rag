"""联网模式共享传输，但保留各自的超时、重试和输出口径。"""
import asyncio
from types import SimpleNamespace

import pytest


def fake_server(monkeypatch, *, fail_at=None, hang_at=None, payload='{"pages": []}'):
    from app.shared.clients import mcp_web
    events, instances = [], []

    class Server:
        def __init__(self, **options):
            self.options = options
            instances.append(self)

        async def step(self, name):
            events.append(name)
            if hang_at == name:
                await asyncio.sleep(10)
            if fail_at == name:
                raise RuntimeError(name)

        async def connect(self):
            await self.step("connect")

        async def call_tool(self, **arguments):
            self.arguments = arguments
            await self.step("call")
            return SimpleNamespace(content=[SimpleNamespace(text=payload)])

        async def cleanup(self):
            await self.step("cleanup")

    monkeypatch.setattr(mcp_web, "MCPServerStreamableHttp", Server)
    return events, instances


def test_legacy_request_keeps_retries_margin_and_page_count(monkeypatch):
    events, instances = fake_server(monkeypatch)
    from app.rag.query import web_search_service as legacy
    from app.shared.clients import mcp_web
    from app.shared.config import settings
    monkeypatch.setattr(settings.mcp, "timeout_seconds", 12)
    windows = []
    timeout_context = asyncio.timeout
    def capture_window(seconds):
        windows.append(seconds)
        return timeout_context(seconds)
    monkeypatch.setattr(mcp_web.asyncio, "timeout", capture_window)
    result = asyncio.run(legacy._call_bailian_web_search("HAK180"))
    assert result.content[0].text == '{"pages": []}'
    assert events == ["connect", "call", "cleanup"]
    server = instances[0]
    assert server.options["params"]["timeout"] == 12
    assert "sse_read_timeout" not in server.options["params"]
    assert "client_session_timeout_seconds" not in server.options
    assert server.options["max_retry_attempts"] == 3
    assert windows == [17]
    assert server.arguments == {"tool_name": "bailian_web_search", "arguments": {"query": "HAK180", "count": 5}}


def test_agent_request_keeps_remaining_budget_and_zero_retries(monkeypatch):
    events, instances = fake_server(monkeypatch)
    from app.rag.agentic import tools
    from app.shared.config import settings
    monkeypatch.setattr(settings.mcp, "timeout_seconds", 30)
    monkeypatch.setattr(tools, "remaining_seconds", lambda deadline: 4.0)
    asyncio.run(tools.RetrievalTools._request_web("HL3040", 2, 99))
    assert events == ["connect", "call", "cleanup"]
    server = instances[0]
    assert server.options["max_retry_attempts"] == 0
    assert server.options["params"]["timeout"] == 4
    assert server.options["params"]["sse_read_timeout"] == 4
    assert server.options["client_session_timeout_seconds"] == 4
    assert server.arguments["arguments"] == {"query": "HL3040", "count": 2}


@pytest.mark.parametrize("phase", ["connect", "call", "cleanup"])
def test_transport_releases_server_once_after_failure(monkeypatch, phase):
    events, _ = fake_server(monkeypatch, fail_at=phase)
    from app.shared.clients.mcp_web import request_web_search
    with pytest.raises(RuntimeError, match=phase):
        asyncio.run(request_web_search("q", 1, request_timeout=1, overall_timeout=1, retries=0))
    assert events.count("cleanup") == 1


@pytest.mark.parametrize("phase", ["connect", "call", "cleanup"])
def test_transport_times_out_and_cleans_up(monkeypatch, phase):
    events, _ = fake_server(monkeypatch, hang_at=phase)
    from app.shared.clients.mcp_web import request_web_search
    with pytest.raises(TimeoutError):
        asyncio.run(request_web_search("q", 1, request_timeout=.01, overall_timeout=.01, retries=0))
    assert events.count("cleanup") == 1


@pytest.mark.parametrize("phase", ["connect", "call", "cleanup"])
def test_transport_cancellation_is_not_converted_to_a_result(monkeypatch, phase):
    events, _ = fake_server(monkeypatch, hang_at=phase)
    from app.shared.clients.mcp_web import request_web_search

    async def run():
        task = asyncio.create_task(request_web_search("q", 1, request_timeout=1, overall_timeout=1, retries=0))
        while phase not in events:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert asyncio.all_tasks() == {asyncio.current_task()}

    asyncio.run(run())
    assert events.count("cleanup") == 1


def test_legacy_invalid_json_and_failure_keep_degradation_codes(monkeypatch):
    from app.rag.query import web_search_service as legacy
    reasons = []
    monkeypatch.setattr(legacy, "mark_degraded", reasons.append)
    fake_server(monkeypatch, payload="invalid json")
    assert legacy.search_by_web({"rewritten_query": "q"}) == []
    assert reasons == ["web_invalid_output"]
    fake_server(monkeypatch, fail_at="call")
    assert legacy.search_by_web({"rewritten_query": "q"}) == []
    assert reasons[-1] == "web_error"


@pytest.mark.parametrize("external_cancel,previous_cancel", [(False, False), (False, True), (True, False)])
def test_real_anyio_scope_distinguishes_transport_failure_from_caller_cancel(monkeypatch, external_cancel, previous_cancel):
    """SDK 的后台失败会取消其所属任务；不能把它传成整条查询的取消。"""
    from contextlib import AsyncExitStack
    import anyio
    from app.shared.clients import mcp_web
    events = []

    class ScopedServer:
        def __init__(self, **options):
            self.stack = AsyncExitStack()

        async def connect(self):
            self.group = await self.stack.enter_async_context(anyio.create_task_group())
            self.group.start_soon(self.background)

        async def background(self):
            await anyio.sleep(0)
            if not external_cancel:
                raise ConnectionError("transport closed")
            await anyio.sleep_forever()

        async def call_tool(self, **arguments):
            events.append("call")
            await anyio.sleep_forever()

        async def cleanup(self):
            events.append("cleanup")
            # 与已安装 SDK 一样，清理会记录并吸收 TaskGroup 的 ExceptionGroup。
            self.group.cancel_scope.cancel()
            try:
                await self.stack.aclose()
            except ExceptionGroup:
                pass

    monkeypatch.setattr(mcp_web, "MCPServerStreamableHttp", ScopedServer)

    async def run():
        caller = asyncio.current_task()
        if previous_cancel:
            caller.cancel()
            try:
                await asyncio.sleep(0)
            except asyncio.CancelledError:
                pass
        if external_cancel:
            async def cancel_when_calling():
                while "call" not in events:
                    await asyncio.sleep(0)
                caller.cancel()
            canceller = asyncio.create_task(cancel_when_calling())
            with pytest.raises(asyncio.CancelledError):
                await mcp_web.request_web_search("q", 1, request_timeout=1, overall_timeout=1, retries=0)
            await canceller
        else:
            with pytest.raises(ConnectionError, match="cancelled"):
                await mcp_web.request_web_search("q", 1, request_timeout=1, overall_timeout=1, retries=0)
            assert caller.cancelling() == int(previous_cancel)
        assert asyncio.all_tasks() == {caller}
        assert events == ["call", "cleanup"]

    asyncio.run(run())
