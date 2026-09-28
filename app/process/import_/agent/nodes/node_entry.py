"""导入图节点：文件类型分派。"""
from app.process.import_.agent.state import ImportGraphState
from app.rag.import_.entry_service import resolve_input_file
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_entry")
@track_node_task("node_entry", id_key="task_id")
def node_entry(state: ImportGraphState) -> ImportGraphState:
    """按后缀把 ``local_file_path`` 分派到 md / pdf 分支（其余类型直接结束）。"""
    return resolve_input_file(state)
