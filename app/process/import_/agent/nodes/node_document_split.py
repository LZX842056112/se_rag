"""导入图节点：文档切分（标题粗切 + 超长细切 + 短块合并）。"""
from app.process.import_.agent.state import ImportGraphState
from app.rag.import_.split_service import split_document
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_document_split")
@track_node_task("node_document_split", id_key="task_id")
def node_document_split(state: ImportGraphState) -> ImportGraphState:
    """把长文档切成便于检索的 chunks。"""
    return split_document(state)
