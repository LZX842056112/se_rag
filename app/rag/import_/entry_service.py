"""导入入口服务：按文件后缀分派流程（仅支持 ``.md`` / ``.pdf``）。"""
from __future__ import annotations

from pathlib import Path

from app.process.import_.agent.state import ImportGraphState
from app.shared.runtime.logger import logger, step_log
from app.shared.utils.require import require_state_str


@step_log("resolve_input_file")
def resolve_input_file(state: ImportGraphState) -> ImportGraphState:
    """判断文件类型并写入 ``md_path`` / ``pdf_path`` 与对应开关、``file_title``。"""
    local_file_path = require_state_str(state, "local_file_path")

    if local_file_path.endswith(".md"):
        state["md_path"] = local_file_path
        state["is_md_read_enabled"] = True
        state["is_pdf_read_enabled"] = False
    elif local_file_path.endswith(".pdf"):
        state["pdf_path"] = local_file_path
        state["is_pdf_read_enabled"] = True
        state["is_md_read_enabled"] = False
    else:
        # 不支持的格式：开关全关，图内条件边直接跳到 END，由 API 层判定为失败
        logger.warning(f"{local_file_path} 类型无法解析，仅支持 md/pdf，流程提前结束")
        state["is_pdf_read_enabled"] = False
        state["is_md_read_enabled"] = False
        return state

    state["file_title"] = Path(local_file_path).stem
    return state
