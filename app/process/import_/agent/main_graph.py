"""导入流程图：entry → (pdf→md) → md 图片增强 → 切分 → 主体识别 → 向量化 → 入库。"""
from langgraph.graph import END, StateGraph

from app.process.import_.agent.nodes.node_bge_embedding import node_bge_embedding
from app.process.import_.agent.nodes.node_document_split import node_document_split
from app.process.import_.agent.nodes.node_entry import node_entry
from app.process.import_.agent.nodes.node_import_milvus import node_import_milvus
from app.process.import_.agent.nodes.node_item_name_recognition import node_item_name_recognition
from app.process.import_.agent.nodes.node_md_img import node_md_img
from app.process.import_.agent.nodes.node_pdf_to_md import node_pdf_to_md
from app.process.import_.agent.state import ImportGraphState
from app.shared.runtime.logger import logger

import_graph_builder = StateGraph(ImportGraphState)

# 1. 注册节点（节点名即函数名，前端进度展示依赖该命名）
import_graph_builder.add_node(node_entry)
import_graph_builder.add_node(node_pdf_to_md)
import_graph_builder.add_node(node_md_img)
import_graph_builder.add_node(node_document_split)
import_graph_builder.add_node(node_item_name_recognition)
import_graph_builder.add_node(node_bge_embedding)
import_graph_builder.add_node(node_import_milvus)

# 2. 入口节点
import_graph_builder.set_entry_point("node_entry")


def node_entry_after(state: ImportGraphState) -> str:
    """按文件类型路由：md 直接做图片增强，pdf 先转 md，其余类型结束。"""
    if state.get("is_md_read_enabled", False):
        logger.info(f"文件类型判定为 md，进入图片增强：{state.get('local_file_path')}")
        return "node_md_img"
    if state.get("is_pdf_read_enabled", False):
        logger.info(f"文件类型判定为 pdf，先转 Markdown：{state.get('local_file_path')}")
        return "node_pdf_to_md"
    logger.warning(f"不支持的文档类型（仅支持 md / pdf）：{state.get('local_file_path')}")
    return END


import_graph_builder.add_conditional_edges("node_entry", node_entry_after, {
    "node_md_img": "node_md_img",
    "node_pdf_to_md": "node_pdf_to_md",
    END: END,
})

# 3. 静态边（主流程）
import_graph_builder.add_edge("node_pdf_to_md", "node_md_img")
import_graph_builder.add_edge("node_md_img", "node_document_split")
import_graph_builder.add_edge("node_document_split", "node_item_name_recognition")
import_graph_builder.add_edge("node_item_name_recognition", "node_bge_embedding")
import_graph_builder.add_edge("node_bge_embedding", "node_import_milvus")
import_graph_builder.add_edge("node_import_milvus", END)

import_app = import_graph_builder.compile()
