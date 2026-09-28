"""向量化服务：读取 chunks，生成稠密+稀疏向量并回填。"""
from __future__ import annotations

from typing import Any

from app.process.import_.agent.state import ImportGraphState
from app.rag.import_.config import EMBEDDING_BATCH_SIZE
from app.rag.import_.inputs import load_chunks, load_subject
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger, step_log


@step_log("batch_generate_embeddings")
def batch_generate_embeddings(
    chunks: list[dict[str, Any]],
    item_name: str,
    batch_size: int = EMBEDDING_BATCH_SIZE,
) -> list[dict[str, Any]]:
    """分批生成向量并回填到每个 chunk（``dense_vector`` / ``sparse_vector``）。

    向量化文本为「主体:xxx,内容:yyy」，与查询端构造方式保持一致。
    """
    length = len(chunks)
    for start in range(0, length, batch_size):
        current_chunks = chunks[start:start + batch_size]
        texts = [f"主体:{item_name},内容:{chunk.get('content')}" for chunk in current_chunks]
        result = llm_providers.generate_embeddings(texts)
        for index, chunk in enumerate(current_chunks):
            chunk["dense_vector"] = result["dense"][index]
            chunk["sparse_vector"] = result["sparse"][index]
    logger.info(f"向量生成完成：{length} 条（批大小 {batch_size}）")
    return chunks


@step_log("generate_chunk_embeddings")
def generate_chunk_embeddings(state: ImportGraphState) -> ImportGraphState:
    """向量化服务入口：校验输入 → 批量向量化 → 回写 ``embeddings_content``。"""
    chunks = load_chunks(state)
    item_name = load_subject(state, "item_name")
    state["embeddings_content"] = batch_generate_embeddings(chunks, item_name)
    return state
