"""HyDE 检索服务：先让 LLM 生成假设性答案，再以「问题 + 假设答案」做向量检索。"""
from __future__ import annotations

from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser

from app.process.query.agent.state import QueryGraphState
from app.rag.query.chunk_search import require_query_and_items, search_chunks, to_chunks
from app.shared.models import llm_providers
from app.shared.runtime.prompts import load_prompt


def call_llm_answer(rewritten_query: str) -> str:
    """让 LLM 生成假设性答案（HyDE 的 H）。"""
    prompt_text = load_prompt("hyde_prompt", rewritten_query=rewritten_query)
    chain = llm_providers.chat() | StrOutputParser()
    return chain.invoke([HumanMessage(content=prompt_text)])


def search_by_hyde(state: QueryGraphState) -> list:
    """执行 HyDE 检索并返回业务 chunk 列表。"""
    item_names, rewritten_query = require_query_and_items(state)
    hypothetical_answer = call_llm_answer(rewritten_query)
    milvus_list = search_chunks(item_names, f"{rewritten_query}:{hypothetical_answer}")
    return to_chunks(milvus_list)
