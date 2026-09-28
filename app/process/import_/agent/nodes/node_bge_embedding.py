"""导入图节点：BGE-M3 向量化。"""
from app.process.import_.agent.state import ImportGraphState
from app.rag.import_.embedding_service import generate_chunk_embeddings
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_bge_embedding")
@track_node_task("node_bge_embedding", id_key="task_id")
def node_bge_embedding(state: ImportGraphState) -> ImportGraphState:
    """为每个 chunk 生成稠密 + 稀疏向量。"""
    return generate_chunk_embeddings(state)
