"""MCP 联网搜索传输。调用方决定预算、重试和结果解析。"""
import asyncio

from agents.mcp import MCPServerStreamableHttp

from app.shared.config import settings


async def request_web_search(query: str, count: int, *, request_timeout: float,
                             overall_timeout: float, retries: int,
                             name: str = "Streamable HTTP Python Server",
                             read_timeout: float | None = None,
                             session_timeout: float | None = None):
    params = {
        "url": settings.mcp.base_url,
        "headers": {"Authorization": f"Bearer {settings.mcp.api_key}"},
        "timeout": request_timeout,
    }
    if read_timeout is not None:
        params["sse_read_timeout"] = read_timeout
    options = {"client_session_timeout_seconds": session_timeout} if session_timeout is not None else {}
    server = MCPServerStreamableHttp(
        name=name, params=params, cache_tools_list=True, max_retry_attempts=retries, **options,
    )
    async def call():
        # SDK 的 AnyIO 取消作用域必须在同一任务中连接并释放。
        try:
            await server.connect()
            return await server.call_tool(
                tool_name="bailian_web_search", arguments={"query": query, "count": count},
            )
        finally:
            await server.cleanup()

    caller = asyncio.current_task()
    initial_cancels = caller.cancelling()
    async with asyncio.timeout(overall_timeout):
        task = asyncio.create_task(call())
        try:
            return await task
        except asyncio.CancelledError as exc:
            if caller.cancelling() > initial_cancels:
                raise
            # SDK 后台任务失败可能取消自身任务；它是传输故障，不能取消主查询。
            raise ConnectionError("MCP transport cancelled") from exc
