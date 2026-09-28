"""导入服务路由：文件上传与任务状态查询。"""
from __future__ import annotations

import shutil
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, UploadFile

from app.api.errors import ApiError
from app.api.schema.import_schema import StatusResponseSchema, UploadResponseSchema
from app.rag.import_.pipeline import invoke_import_graph
from app.shared.runtime.logger import logger
from app.shared.utils.paths import PROJECT_ROOT
from app.shared.utils.task_state import (
    add_done_task,
    add_running_task,
    get_done_task_list,
    get_running_task_list,
    get_task_status,
)

router = APIRouter(prefix="/api/import", tags=["import"])

_ALLOWED_SUFFIXES = (".md", ".pdf")


@router.post("/upload", response_model=UploadResponseSchema)
def upload(background_tasks: BackgroundTasks, files: list[UploadFile]) -> UploadResponseSchema:
    """上传单个文件并异步执行导入流程。

    入口即校验扩展名（仅 md / pdf）与文件名，避免「已完成但未入库」的静默成功。
    """
    if not files:
        raise ApiError("no_files", "未收到上传文件", status_code=422)

    upload_file = files[0]
    safe_name = Path(upload_file.filename or "").name  # 仅取 basename，防路径穿越
    if not safe_name or safe_name in (".", ".."):
        raise ApiError("invalid_filename", "文件名不合法", status_code=422)
    if Path(safe_name).suffix.lower() not in _ALLOWED_SUFFIXES:
        raise ApiError("unsupported_type", "仅支持 md / pdf 格式文件", status_code=422)

    task_id = str(uuid.uuid4())
    local_dir = PROJECT_ROOT / "output" / datetime.now().strftime("%Y%m%d") / task_id
    local_dir.mkdir(parents=True, exist_ok=True)
    local_file_path = local_dir / safe_name

    try:
        add_running_task(task_id, "upload_file")
        with local_file_path.open("wb") as buffer:
            shutil.copyfileobj(upload_file.file, buffer)
        add_done_task(task_id, "upload_file")
    except OSError as exc:
        logger.exception(f"文件落盘失败：{exc}")
        raise ApiError("save_failed", "文件保存失败", status_code=500) from exc

    background_tasks.add_task(
        invoke_import_graph,
        task_id=task_id,
        local_file_path=str(local_file_path),
        local_dir=str(local_dir),
    )
    return UploadResponseSchema(code=200, message=f"{upload_file.filename} 文件上传成功", task_ids=[task_id])


@router.get("/status/{task_id}", response_model=StatusResponseSchema)
def task_status(task_id: str) -> StatusResponseSchema:
    """查询导入任务状态（整体状态 + 已完成 / 进行中的节点）。"""
    return StatusResponseSchema(
        code=200,
        task_id=task_id,
        status=get_task_status(task_id),
        done_list=get_done_task_list(task_id),
        running_list=get_running_task_list(task_id),
    )
