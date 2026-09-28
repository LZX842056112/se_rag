"""RRF 融合服务：把多路召回（向量 / HyDE / 自进化条目）按 Reciprocal Rank Fusion 合并。"""
from __future__ import annotations

from app.process.query.agent.state import QueryGraphState
from app.rag.query.config import NODE_RRF_K, NODE_RRF_LIMIT_TOP, get_rrf_k, get_rrf_top
from app.shared.config import settings
from app.shared.runtime.logger import logger, step_log
from app.shared.utils.require import require_state_list


@step_log("_require_retrieval_chunks")
def _require_retrieval_chunks(state: QueryGraphState) -> tuple[list, list]:
    """校验两路必备召回结果（向量检索 / HyDE 检索）。"""
    embedding_chunks = require_state_list(state, "embedding_chunks")
    hyde_chunks = require_state_list(state, "hyde_embedding_chunks")
    return embedding_chunks, hyde_chunks


def use_by_rrf(rrf_list: list, top: int = NODE_RRF_LIMIT_TOP, k: int = NODE_RRF_K) -> list:
    """按权重计算 RRF 得分并返回 Top-N。

    :param rrf_list: ``[(weight, chunks), ...]``，chunks 已按各路排名有序
    :param top: 保留条数
    :param k: RRF 平滑参数
    """
    score_dict: dict[str, float] = {}
    chunk_dict: dict[str, dict] = {}
    for weight, current_chunks in rrf_list:
        for rank, chunk in enumerate(current_chunks, start=1):
            chunk_id = chunk.get("chunk_id")
            if chunk_id is None:
                continue
            score_dict[chunk_id] = score_dict.get(chunk_id, 0.0) + weight * (1 / (k + rank))
            # 同一 chunk 在多路命中时保留首次出现的实体内容
            chunk_dict.setdefault(chunk_id, chunk)

    chunk_list = []
    for chunk_id, score in score_dict.items():
        chunk = chunk_dict[chunk_id]
        chunk["score"] = score  # 用融合分替换单路检索分
        chunk_list.append(chunk)
    chunk_list.sort(key=lambda x: x.get("score", 0), reverse=True)
    return chunk_list[:top]


@step_log("fuse_by_rrf")
def fuse_by_rrf(state: QueryGraphState) -> QueryGraphState:
    """融合多路召回结果并回写 ``rrf_chunks``。"""
    embedding_chunks, hyde_chunks = _require_retrieval_chunks(state)
    rrf_list = [(1.0, embedding_chunks), (1.0, hyde_chunks)]

    evolution_chunks = state.get("evolution_chunks", []) if settings.evolution.enabled else []
    if evolution_chunks:
        rrf_list.append((settings.evolution.rrf_weight, evolution_chunks))

    rrf_chunks = use_by_rrf(rrf_list, top=get_rrf_top(), k=get_rrf_k())

    # 自进化权威条目防挤出：已召回的演进 FAQ 若未被 RRF top-N 收录则强制并入，交由重排裁决。
    # 否则权威 FAQ 会被主库弱相关分片（设备/电源线等泛化片段）竞争掉，出现「已入库却永远
    # 进不了答案上下文、客服仍无法作答」。
    if evolution_chunks:
        existing_ids = {c.get("chunk_id") for c in rrf_chunks}
        missing = [c for c in evolution_chunks if c.get("chunk_id") not in existing_ids]
        if missing:
            rrf_chunks = rrf_chunks + missing[:settings.evolution.recall_limit]

    state["rrf_chunks"] = rrf_chunks
    logger.info(
        f"RRF 融合完成：输入 kb={len(embedding_chunks)}, hyde={len(hyde_chunks)}, "
        f"evolution={len(evolution_chunks)}，输出 {len(rrf_chunks)} 条"
    )
    return state
