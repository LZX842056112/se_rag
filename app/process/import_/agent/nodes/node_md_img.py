"""导入图节点：Markdown 图片增强（说明生成 + MinIO 上传 + 链接替换）。"""
from app.process.import_.agent.state import ImportGraphState
from app.rag.import_.enrich_markdown_images import enrich_markdown_images
from app.shared.runtime.logger import node_log
from app.shared.utils.task_state import track_node_task


@node_log("node_md_img")
@track_node_task("node_md_img", id_key="task_id")
def node_md_img(state: ImportGraphState) -> ImportGraphState:
    """处理 Markdown 中引用的图片资源。"""
    return enrich_markdown_images(state)
