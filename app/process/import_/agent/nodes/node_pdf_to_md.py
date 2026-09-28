"""导入图节点：PDF → Markdown（MinerU）。"""
from app.process.import_.agent.state import ImportGraphState
from app.rag.import_.pdf_parse_service import parse_pdf_to_markdown
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_pdf_to_md")
@track_node_task("node_pdf_to_md", id_key="task_id")
def node_pdf_to_md(state: ImportGraphState) -> ImportGraphState:
    """调用 MinerU 解析 PDF 并落地 Markdown。"""
    return parse_pdf_to_markdown(state)
