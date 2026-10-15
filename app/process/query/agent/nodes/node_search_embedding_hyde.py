"""查询图节点：HyDE 假设性文档检索。"""
from app.rag.query.hyde_search_service import search_by_hyde
from app.shared.runtime.logger import logger, node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_search_embedding_hyde")
@track_node_task("node_search_embedding_hyde")
def node_search_embedding_hyde(state):
    """先让 LLM 生成假设性答案，再用「问题 + 假设答案」检索，提升召回率。

    HyDE 是可选增强路：生成或检索失败都降级为该路空结果（与联网、自进化召回的
    既有口径一致），不得把整笔查询打成 500。
    """
    try:
        return {"hyde_embedding_chunks": search_by_hyde(state)}
    except Exception as exc:  # noqa: BLE001 - HyDE 失败降级为空，由 zero_hit 信号体现
        logger.warning(f"HyDE 检索失败，降级为该路空结果：{exc}")
        return {"hyde_embedding_chunks": []}
