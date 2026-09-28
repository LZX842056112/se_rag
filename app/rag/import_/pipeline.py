"""导入链路编排：执行 LangGraph 导入图并维护任务状态。"""
from __future__ import annotations

from app.process.import_.agent.main_graph import import_app
from app.process.import_.agent.state import ImportGraphState, create_default_state
from app.shared.runtime.logger import logger
from app.shared.utils.task_state import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PROCESSING,
    update_task_status,
)


def invoke_import_graph(task_id: str, local_file_path: str, local_dir: str) -> None:
    """执行一次导入：跑图并落任务状态；不支持的文档类型显式标记失败。"""
    try:
        update_task_status(task_id, TASK_STATUS_PROCESSING)
        state: ImportGraphState = create_default_state(
            task_id=task_id, local_file_path=local_file_path, local_dir=local_dir
        )
        final_state = import_app.invoke(state)
        # 防御：图对不支持的文档类型会静默走到 END，这里显式校验是否真的解析过该文件
        if not (final_state.get("md_path") or final_state.get("pdf_path")):
            update_task_status(task_id, TASK_STATUS_FAILED)
            logger.warning(f"导入任务[{task_id}]中止：文件类型不支持（md/pdf 以外）")
            return
        update_task_status(task_id, TASK_STATUS_COMPLETED)
    except Exception as exc:  # noqa: BLE001
        update_task_status(task_id, TASK_STATUS_FAILED)
        logger.exception(f"导入模块执行发生异常：{exc}")
    # 说明：这里刻意不立即清理进度记录——前端仍需轮询到最终状态；内存占用由
    # task_state 的 TTL 回收（TASK_STATE_TTL_SECONDS，默认 6 小时）保证不无限增长。
