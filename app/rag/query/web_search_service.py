"""联网检索服务：通过 MCP 协议调用百炼联网搜索，失败降级为空结果。"""
from __future__ import annotations

import asyncio
import json

from app.process.query.agent.state import QueryGraphState
from app.shared.clients.mcp_web import request_web_search
from app.shared.config import settings
from app.shared.runtime.logger import logger
from app.shared.runtime.query_metrics import mark_degraded
from app.shared.utils.require import require_state_str

# MCP 客户端自身超时之外的兜底裕量：连接与协议握手慢于该值时整体放弃
_MCP_TIMEOUT_MARGIN_SECONDS = 5


async def _call_bailian_web_search(rewritten_query: str):
    """连接百炼 MCP 并调用联网搜索工具；异常返回 None（由调用方降级）。"""
    timeout_seconds = settings.mcp.timeout_seconds
    try:
        return await request_web_search(
            rewritten_query, 5, request_timeout=timeout_seconds,
            overall_timeout=timeout_seconds + _MCP_TIMEOUT_MARGIN_SECONDS, retries=3,
        )
    except Exception as exc:  # noqa: BLE001 - 联网失败必须降级而非中断主链路
        logger.exception(f"MCP 联网搜索调用失败：{exc}")
        return None


def search_by_web(state: QueryGraphState) -> list[dict]:
    """执行联网检索，返回 ``[{title, snippet, url}, ...]``；失败返回空列表。"""
    rewritten_query = require_state_str(state, "rewritten_query")
    try:
        mcp_result = asyncio.run(_call_bailian_web_search(rewritten_query))
    except Exception as exc:  # noqa: BLE001 - 联网检索是可选增强，任何异常都降级为空
        mark_degraded("web_error")
        logger.warning(f"联网搜索不可用，降级为纯本地召回：{exc}")
        return []
    if mcp_result is None:
        mark_degraded("web_error")
    if mcp_result is None or not mcp_result.content:
        logger.warning("联网搜索未返回有效结果，本次仅使用本地召回结果")
        return []
    try:
        # MCP 返回对象的字段是属性而非字典
        payload = json.loads(mcp_result.content[0].text)
    except (ValueError, AttributeError, IndexError) as exc:
        mark_degraded("web_invalid_output")
        logger.warning(f"联网搜索结果解析失败，降级为空：{exc}")
        return []
    return payload.get("pages", []) or []
