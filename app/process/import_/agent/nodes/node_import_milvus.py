"""导入图节点：写入 Milvus 知识库集合。"""
from app.process.import_.agent.state import ImportGraphState
from app.rag.import_.index_service import index_chunks
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_import_milvus")
@track_node_task("node_import_milvus", id_key="task_id")
def node_import_milvus(state: ImportGraphState) -> ImportGraphState:
    """把带向量的 chunk 批量写入 ``kb_chunks``（同文档幂等覆盖）。"""
    return index_chunks(state)
