"""联网检索服务：通过 MCP 协议调用百炼联网搜索，失败降级为空结果。"""
from __future__ import annotations

import asyncio
import json
import re

from app.process.query.agent.state import QueryGraphState
from app.rag.item_name.config import GLOBAL_ITEM_NAME
from app.rag.query.business import classify_business_request
from app.rag.query.config import LEGACY_ANSWER_RESERVE_SECONDS
from app.shared.clients.mcp_web import request_web_search
from app.shared.config import settings
from app.shared.runtime.deadline import bounded_timeout
from app.shared.runtime.logger import logger
from app.shared.runtime.query_metrics import mark_degraded
from app.shared.utils.require import require_state_str
from app.shared.utils.text import normalize_item_name, product_model_tokens


_WEB_REQUEST = re.compile(
    r"请联网|(?:联网|上网|网上)(?:查|搜|找|核实)|网络搜索|官网|最新|实时(?:信息|数据)"
    r"|\b(?:search the web|search online|look up online|latest)\b",
    re.IGNORECASE,
)


def should_search_web(state: QueryGraphState) -> bool:
    """本地两路首位均为同一产品手册切片时，直接使用本地证据。"""
    if classify_business_request(state) in ("complaint", "pricing", "after_sales"):
        return False
    items = state.get("item_names") or []
    question = state.get("rewrite_hint") or state.get("original_query") or state.get("rewritten_query") or ""
    if len(items) != 1 or _WEB_REQUEST.search(question):
        return True
    if len(product_model_tokens(items[0]) & product_model_tokens(question)) > 1:
        return True
    vectors = state.get("embedding_chunks") or []
    keywords = state.get("keyword_chunks") or []
    if not vectors or not keywords:
        return True
    best = vectors[0]
    return not (
        best.get("chunk_id")
        and best["chunk_id"] == keywords[0].get("chunk_id")
        and best.get("source") == "milvus"
        and best.get("document_kind") in (None, "", "manual")
        and best.get("content")
        and best.get("item_name") != GLOBAL_ITEM_NAME
        and normalize_item_name(best.get("item_name")) == normalize_item_name(items[0])
    )


def filter_web_pages(pages: list[dict], item_names: list[str], question: str) -> list[dict]:
    """只保留正文或标题提到请求型号的网页。"""
    models = {model for name in item_names for model in product_model_tokens(name)}
    if not models:
        return pages
    requested = models & product_model_tokens(question) or models
    return [
        page for page in pages
        if requested & product_model_tokens(" ".join(str(page.get(key) or "") for key in ("title", "snippet", "content")))
    ]


async def _call_bailian_web_search(rewritten_query: str):
    """在可选联网检索的时限内执行一次 MCP 调用。"""
    timeout = bounded_timeout(
        min(settings.mcp.timeout_seconds, settings.mcp.legacy_timeout_seconds),
        reserve_seconds=LEGACY_ANSWER_RESERVE_SECONDS,
    )
    return await request_web_search(
        rewritten_query, 5, request_timeout=timeout, overall_timeout=timeout, retries=0,
        read_timeout=timeout, session_timeout=timeout,
    )


def search_by_web(state: QueryGraphState) -> list[dict]:
    """执行联网检索，返回 ``[{title, snippet, url}, ...]``；失败返回空列表。"""
    rewritten_query = require_state_str(state, "rewritten_query")
    if classify_business_request(state) in ("complaint", "pricing", "after_sales"):
        return []
    try:
        mcp_result = asyncio.run(_call_bailian_web_search(rewritten_query))
    except TimeoutError:
        mark_degraded("web_timeout")
        logger.warning("联网搜索超时，本次使用本地召回结果")
        return []
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
        pages = payload.get("pages") or []
        if not isinstance(pages, list):
            raise ValueError("无效的联网结果列表")
    except (ValueError, TypeError, AttributeError, IndexError) as exc:
        mark_degraded("web_invalid_output")
        logger.warning(f"联网搜索结果解析失败，降级为空：{exc}")
        return []
    valid = [page for page in pages if isinstance(page, dict) and all(
        page.get(field) is None or isinstance(page[field], str) for field in ("title", "snippet", "url")
    )]
    if len(valid) != len(pages):
        mark_degraded("web_invalid_output")
        logger.warning("已丢弃字段无效的联网结果")
    question = state.get("rewrite_hint") or state.get("original_query") or rewritten_query
    return filter_web_pages(valid, state.get("item_names") or [], question)
