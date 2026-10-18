"""导入服务路由：文件上传与任务状态查询。

加固要点：

1. **鉴权**：上传与状态查询都要求请求头 ``X-Internal-Token`` 与 ``IMPORT_ADMIN_TOKEN`` 一致；
   未配置 Token 时 **fail-closed**（503），避免「忘配 Token 等于无鉴权」。
2. **入口校验**：文件数 → 文件名 → 后缀 → 大小 → 文件头（手写魔数，不依赖 libmagic）。
3. **状态回退**：内存态未命中时读磁盘快照，进程重启后仍可查到最终状态。
"""
from __future__ import annotations

import os
import shutil
import uuid
from datetime import datetime
from pathlib import Path

from typing import Annotated, Literal
import time

from fastapi import Form, APIRouter, BackgroundTasks, Depends, Header, UploadFile

from app.api.errors import ApiError
from app.api.schema.import_schema import StatusResponseSchema, UploadResponseSchema
from app.rag.import_.pipeline import invoke_import_graph
from app.shared.config import settings
from app.shared.runtime.logger import logger
from app.shared.utils.paths import PROJECT_ROOT
from app.shared.utils.task_snapshot import load_snapshot
from app.shared.utils.task_state import (
    add_done_task,
    add_running_task,
    get_done_task_list,
    get_running_task_list,
    get_task_error,
    get_task_status,
)

router = APIRouter(prefix="/api/import", tags=["import"])

_ALLOWED_SUFFIXES = (".md", ".pdf")
# 文件头嗅探读取的字节数
_HEAD_BYTES = 1024


def require_import_admin_token(x_internal_token: str = Header(default="")) -> None:
    """写/读操作的鉴权依赖：校验 ``X-Internal-Token`` 与 ``IMPORT_ADMIN_TOKEN`` 一致。

    未配置 Token 时直接 503（fail-closed），防止漏配导致接口裸奔。
    与 ``evolution`` 的鉴权形态一致，但**从 settings 读取**，不直读 ``os.getenv``。
    """
    expected = settings.import_.admin_token
    if not expected:
        raise ApiError("import_admin_token_missing", "IMPORT_ADMIN_TOKEN 未配置", status_code=503)
    if x_internal_token != expected:
        raise ApiError("import_unauthorized", "鉴权失败", status_code=401)


def _read_head(upload_file: UploadFile, size: int = _HEAD_BYTES) -> bytes:
    """读取文件头若干字节，并把游标复位到起点（后续落盘依赖游标在 0）。"""
    stream = upload_file.file
    try:
        head = stream.read(size)
    finally:
        stream.seek(0)
    return head or b""


def _measure_size(upload_file: UploadFile) -> int:
    """返回文件字节数；无法测量时返回 -1（按「未知」处理，不误拒）。"""
    size = getattr(upload_file, "size", None)
    if isinstance(size, int) and size >= 0:
        return size
    stream = upload_file.file
    try:
        position = stream.tell()
        stream.seek(0, os.SEEK_END)
        total = stream.tell()
        stream.seek(position)
        return int(total)
    except Exception:  # noqa: BLE001 - 测量失败不应阻断上传
        return -1


def _check_signature(upload_file: UploadFile, suffix: str) -> None:
    """文件头/编码校验：只认扩展名会被「改后缀」绕过，这里做一次内容嗅探。"""
    head = _read_head(upload_file)
    if suffix == ".pdf":
        if not head.startswith(b"%PDF-"):
            raise ApiError("file_content_mismatch", "文件内容不是有效的 PDF", status_code=422)
        return
    # .md：必须是可解码的 UTF-8 文本（含二进制控制字节的一律拒绝）
    if b"\x00" in head:
        raise ApiError("file_content_mismatch", "文件内容不是有效的文本", status_code=422)
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApiError("file_content_mismatch", "文件不是 UTF-8 文本", status_code=422) from exc


def validate_upload_files(files: list[UploadFile]) -> UploadFile:
    """校验上传列表并返回唯一待处理文件。

    顺序：文件数 → 文件名 → 后缀 → 大小 → 文件头。任一不通过即抛 ``ApiError``。
    """
    if not files:
        raise ApiError("no_files", "未收到上传文件", status_code=422)
    if len(files) > 1:
        # 前端是「每个文件一次请求」；历史上后端只取 files[0] 并静默丢弃其余文件
        raise ApiError("too_many_files", "一次仅支持上传一个文件", status_code=422)

    upload_file = files[0]
    safe_name = Path(upload_file.filename or "").name  # 仅取 basename，防路径穿越
    if not safe_name or safe_name in (".", ".."):
        raise ApiError("invalid_filename", "文件名不合法", status_code=422)
    suffix = Path(safe_name).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise ApiError("unsupported_type", "仅支持 md / pdf 格式文件", status_code=422)

    # 大小上限：IMPORT_MAX_UPLOAD_MB <= 0 表示不限制
    limit_mb = settings.import_.max_upload_mb
    if limit_mb > 0:
        size = _measure_size(upload_file)
        if size >= 0 and size > limit_mb * 1024 * 1024:
            raise ApiError(
                "file_too_large",
                f"文件超过 {limit_mb}MB 上限",
                status_code=413,
            )

    _check_signature(upload_file, suffix)
    return upload_file


@router.post(
    "/upload",
    response_model=UploadResponseSchema,
    dependencies=[Depends(require_import_admin_token)],
)
def upload(background_tasks: BackgroundTasks, files: list[UploadFile], document_kind: Annotated[Literal["manual", "policy"], Form()] = "manual", document_version: Annotated[str, Form(max_length=128)] = "", valid_until: Annotated[float, Form(allow_inf_nan=False)] = 0.0) -> UploadResponseSchema:
    """上传单个文件并异步执行导入流程。

    入口即校验鉴权、文件数、扩展名、大小与文件头，避免「已完成但未入库」的静默成功。
    """
    if document_kind == "policy" and (not document_version.strip() or valid_until <= time.time()):
        raise ApiError("policy_metadata_required", "业务政策必须填写版本和未来的失效时间", 422)
    upload_file = validate_upload_files(files)
    safe_name = Path(upload_file.filename or "").name

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
        document_kind=document_kind,
        document_version=document_version,
        valid_until=valid_until,
    )
    return UploadResponseSchema(code=200, message=f"{upload_file.filename} 文件上传成功", task_ids=[task_id])


@router.get(
    "/status/{task_id}",
    response_model=StatusResponseSchema,
    dependencies=[Depends(require_import_admin_token)],
)
def task_status(task_id: str) -> StatusResponseSchema:
    """查询导入任务状态（内存优先，未命中时回退磁盘快照）。"""
    status = get_task_status(task_id)
    done_list = get_done_task_list(task_id)
    running_list = get_running_task_list(task_id)
    error_code, message = get_task_error(task_id)

    if not status and not done_list and not running_list:
        saved = load_snapshot(task_id)
        if saved:
            status = str(saved.get("status") or "")
            done_list = list(saved.get("done_list") or [])
            running_list = list(saved.get("running_list") or [])
            error_code = str(saved.get("error_code") or "")
            message = str(saved.get("message") or "")

    return StatusResponseSchema(
        code=200,
        task_id=task_id,
        status=status,
        done_list=done_list,
        running_list=running_list,
        error_code=error_code or None,
        message=message or None,
    )
