"""HyDE 检索服务：先让 LLM 生成假设性答案，再以「问题 + 假设答案」做向量检索。"""
from __future__ import annotations

from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser

from app.process.query.agent.state import QueryGraphState
from app.rag.query.chunk_search import require_query_and_items, search_chunks, to_chunks
from app.rag.query.config import HYDE_GATE_ENABLED, HYDE_GATE_SHORT_CHARS
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger
from app.shared.runtime.prompts import load_prompt

# 事实型标记词：答案通常是参数/数值本身，不需要假设性文档
_FACTUAL_MARKERS: tuple[str, ...] = (
    "多少", "几", "参数", "规格", "功率", "电压", "电流", "配对码", "序列号",
    "型号", "价格", "电话", "网址", "http", "是多少", "多大", "多重",
)


def _skip_hyde(rewritten_query: str) -> bool:
    """保守启发式：开启分流 且 是短事实型查询时跳过 HyDE。默认关闭时恒为 False。"""
    if not HYDE_GATE_ENABLED:
        return False
    text = (rewritten_query or "").strip()
    if len(text) > HYDE_GATE_SHORT_CHARS:
        return False
    return any(marker in text for marker in _FACTUAL_MARKERS)


def call_llm_answer(rewritten_query: str) -> str:
    """让 LLM 生成假设性答案（HyDE 的 H）。"""
    prompt_text = load_prompt("hyde_prompt", rewritten_query=rewritten_query)
    chain = llm_providers.chat() | StrOutputParser()
    return chain.invoke([HumanMessage(content=prompt_text)])


def search_by_hyde(state: QueryGraphState) -> list:
    """执行 HyDE 检索并返回业务 chunk 列表；分流命中时跳过假设答案生成。"""
    item_names, rewritten_query = require_query_and_items(state)
    if _skip_hyde(rewritten_query):
        logger.info(f"HyDE 分流：短事实型查询跳过假设答案生成，query={rewritten_query[:50]}")
        return []
    hypothetical_answer = call_llm_answer(rewritten_query)
    milvus_list = search_chunks(item_names, f"{rewritten_query}:{hypothetical_answer}")
    return to_chunks(milvus_list)
