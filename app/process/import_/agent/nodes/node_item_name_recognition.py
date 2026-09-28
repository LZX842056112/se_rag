"""导入图节点：主体名识别与索引写入。"""
from app.process.import_.agent.state import ImportGraphState
from app.rag.import_.item_name_service import recognize_and_index_item_name
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_item_name_recognition")
@track_node_task("node_item_name_recognition", id_key="task_id")
def node_item_name_recognition(state: ImportGraphState) -> ImportGraphState:
    """识别文档主体名、归并到库内标准名，并写入主体名索引。"""
    return recognize_and_index_item_name(state)
