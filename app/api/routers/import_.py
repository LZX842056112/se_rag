"""导入服务路由：文件上传、任务状态查询、失败重试与记录删除。

加固要点：

1. **鉴权**：上传、状态查询、重试与删除都要求请求头 ``X-Internal-Token`` 与 ``IMPORT_ADMIN_TOKEN`` 一致；
   未配置 Token 时 **fail-closed**（503），避免「忘配 Token 等于无鉴权」。
2. **入口校验**：文件数 → 文件名 → 后缀 → 大小 → 文件头（手写魔数，不依赖 libmagic）。
3. **状态回退**：内存态未命中时读磁盘快照，进程重启后仍可查到最终状态。
"""
from __future__ import annotations

import os
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path

from typing import Annotated, Literal
import time

from fastapi import Form, APIRouter, BackgroundTasks, Depends, Header, UploadFile

from app.api.errors import ApiError
from app.api.schema.import_schema import (
    StatusResponseSchema,
    TaskActionResponseSchema,
    UploadResponseSchema,
)
from app.rag.import_.pipeline import submit_import_graph
from app.rag.import_.task_records import (
    delete_task_artifacts,
    find_source_file,
    find_task_dir,
    is_valid_task_id,
    load_task_meta,
    save_task_meta,
)
from app.shared.config import settings
from app.shared.runtime.logger import logger
from app.shared.utils.paths import PROJECT_ROOT
from app.shared.utils.task_snapshot import delete_snapshot, load_snapshot, save_snapshot
from app.shared.utils.task_state import (
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_STATUS_PROCESSING,
    add_done_task,
    add_running_task,
    clear_task,
    get_done_task_list,
    get_running_task_list,
    get_task_error,
    get_task_status,
    snapshot,
    update_task_status,
)

router = APIRouter(prefix="/api/import", tags=["import"])

# 重试与删除的状态检查在同一进程内互斥：同步端点跑在线程池里，
# 不加锁时双击「重试」可能让两个请求同时通过 failed 检查，把同一文件导入两次。
_STATE_CHANGE_LOCK = threading.Lock()

_ALLOWED_SUFFIXES = (".md", ".pdf")
# 文件头嗅探读取的字节数
_HEAD_BYTES = 1024
# 一次请求最多接收的文件数：防止单请求塞入过多任务，压垮导入队列
MAX_BATCH_FILES = 20


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


def _check_signature(upload_file: UploadFile, suffix: str, label: str) -> None:
    """文件头/编码校验：只认扩展名会被「改后缀」绕过，这里做一次内容嗅探。"""
    head = _read_head(upload_file)
    if suffix == ".pdf":
        if not head.startswith(b"%PDF-"):
            raise ApiError("file_content_mismatch", f"{label}内容不是有效的 PDF", status_code=422)
        return
    # .md：必须是可解码的 UTF-8 文本（含二进制控制字节的一律拒绝）
    if b"\x00" in head:
        raise ApiError("file_content_mismatch", f"{label}内容不是有效的文本", status_code=422)
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ApiError("file_content_mismatch", f"{label}不是 UTF-8 文本", status_code=422) from exc


def _validate_upload_file(upload_file: UploadFile, index: int) -> None:
    """校验单个上传文件；提示里带上文件序号与名称，便于用户定位。"""
    safe_name = Path(upload_file.filename or "").name  # 仅取 basename，防路径穿越
    label = f"第 {index} 个文件（{safe_name}）" if safe_name else f"第 {index} 个文件"
    if not safe_name or safe_name in (".", ".."):
        raise ApiError("invalid_filename", f"{label}文件名不合法", status_code=422)
    suffix = Path(safe_name).suffix.lower()
    if suffix not in _ALLOWED_SUFFIXES:
        raise ApiError("unsupported_type", f"{label}格式不支持，仅支持 md / pdf", status_code=422)

    # 大小上限：IMPORT_MAX_UPLOAD_MB <= 0 表示不限制
    limit_mb = settings.import_.max_upload_mb
    if limit_mb > 0:
        size = _measure_size(upload_file)
        if size >= 0 and size > limit_mb * 1024 * 1024:
            raise ApiError(
                "file_too_large",
                f"{label}超过 {limit_mb}MB 上限",
                status_code=413,
            )

    _check_signature(upload_file, suffix, label)


def validate_upload_files(files: list[UploadFile]) -> list[UploadFile]:
    """整批校验上传文件；任一文件不通过即整批拒绝。

    顺序：文件数 → 文件名 → 后缀 → 大小 → 文件头。全部通过才返回原列表。
    """
    if not files:
        raise ApiError("no_files", "未收到上传文件", status_code=422)
    if len(files) > MAX_BATCH_FILES:
        raise ApiError(
            "too_many_files",
            f"一次最多上传 {MAX_BATCH_FILES} 个文件",
            status_code=422,
        )

    for index, upload_file in enumerate(files, start=1):
        _validate_upload_file(upload_file, index)
    return files


@router.post(
    "/upload",
    response_model=UploadResponseSchema,
    dependencies=[Depends(require_import_admin_token)],
)
def upload(background_tasks: BackgroundTasks, files: list[UploadFile], document_kind: Annotated[Literal["manual", "policy"], Form()] = "manual", document_version: Annotated[str, Form(max_length=128)] = "", valid_until: Annotated[float, Form(allow_inf_nan=False)] = 0.0) -> UploadResponseSchema:
    """上传一批文件，并为每个文件创建独立的导入任务。

    入口先整批校验（鉴权、文件名、后缀、大小、文件头）；任一文件不通过则不创建
    任何任务。逐文件落盘建任务，中途落盘失败时清理本批已建任务与目录。
    全部落盘后先置为排队中，再提交进程内线程池并行执行。
    """
    if document_kind == "policy" and (not document_version.strip() or valid_until <= time.time()):
        raise ApiError("policy_metadata_required", "业务政策必须填写版本和未来的失效时间", 422)
    upload_files = validate_upload_files(files)

    # (task_id, 任务目录, 本地文件路径)；落盘失败时按此清单回滚
    created: list[tuple[str, Path, Path]] = []
    try:
        for upload_file in upload_files:
            safe_name = Path(upload_file.filename or "").name
            task_id = str(uuid.uuid4())
            local_dir = PROJECT_ROOT / "output" / datetime.now().strftime("%Y%m%d") / task_id
            local_dir.mkdir(parents=True, exist_ok=True)
            local_file_path = local_dir / safe_name
            created.append((task_id, local_dir, local_file_path))

            add_running_task(task_id, "upload_file")
            with local_file_path.open("wb") as buffer:
                shutil.copyfileobj(upload_file.file, buffer)
            # 记录上传参数：失败重试时要还原资料类型、版本与失效时间
            save_task_meta(
                local_dir,
                file_name=safe_name,
                document_kind=document_kind,
                document_version=document_version,
                valid_until=valid_until,
            )
            add_done_task(task_id, "upload_file")
    except OSError as exc:
        logger.exception(f"文件落盘失败：{exc}")
        # 整批拒绝：任一文件落盘失败时清掉本批已建任务与目录，避免半批残留
        for task_id, local_dir, _ in created:
            clear_task(task_id)
            shutil.rmtree(local_dir, ignore_errors=True)
        raise ApiError("save_failed", "文件保存失败，请重试", status_code=500) from exc

    # 先置为排队中：任务在线程池队列里等待，前端不会把空状态误判为「状态不可用」
    for task_id, _, _ in created:
        update_task_status(task_id, TASK_STATUS_PENDING)

    for task_id, local_dir, local_file_path in created:
        background_tasks.add_task(
            submit_import_graph,
            task_id=task_id,
            local_file_path=str(local_file_path),
            local_dir=str(local_dir),
            document_kind=document_kind,
            document_version=document_version,
            valid_until=valid_until,
        )

    if len(created) == 1:
        message = f"{upload_files[0].filename} 文件上传成功"
    else:
        message = f"{len(created)} 个文件上传成功"
    return UploadResponseSchema(
        code=200,
        message=message,
        task_ids=[task_id for task_id, _, _ in created],
    )


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


def _current_import_status(task_id: str) -> str:
    """当前任务状态：内存优先，未命中时读磁盘快照；两处都没有返回空串。"""
    status = get_task_status(task_id)
    if status:
        return status
    saved = load_snapshot(task_id) or {}
    return str(saved.get("status") or "")


@router.post(
    "/retry/{task_id}",
    response_model=TaskActionResponseSchema,
    dependencies=[Depends(require_import_admin_token)],
)
def retry_import(task_id: str, background_tasks: BackgroundTasks) -> TaskActionResponseSchema:
    """重新执行失败的导入任务。

    复用原任务目录与已落盘的原文件：用户不用重新选择文件，已生成的 MinerU 产物也能复用。
    重试沿用原 task_id，前端无需更换轮询地址。
    """
    if not is_valid_task_id(task_id):
        raise ApiError("invalid_task_id", "任务 ID 不合法", status_code=422)

    with _STATE_CHANGE_LOCK:
        status = _current_import_status(task_id)
        if status in (TASK_STATUS_PENDING, TASK_STATUS_PROCESSING):
            raise ApiError("import_in_progress", "任务正在执行，不能重复提交", status_code=409)
        if status != TASK_STATUS_FAILED:
            raise ApiError("import_not_failed", "只有失败的导入可以重试", status_code=409)

        task_dir = find_task_dir(task_id)
        if task_dir is None:
            raise ApiError("import_source_missing", "原始文件已不存在，请重新上传", status_code=410)

        meta = load_task_meta(task_dir)
        file_name = str(meta.get("file_name") or "")
        source_file = find_source_file(task_dir, file_name)
        if source_file is None:
            raise ApiError("import_source_missing", "原始文件已不存在，请重新上传", status_code=410)

        document_kind = str(meta.get("document_kind") or "manual")
        document_version = str(meta.get("document_version") or "")
        try:
            valid_until = float(meta.get("valid_until") or 0.0)
        except (TypeError, ValueError):
            valid_until = 0.0
        if document_kind == "policy" and (
            not document_version.strip() or valid_until <= time.time()
        ):
            raise ApiError(
                "policy_metadata_expired",
                "业务政策的版本或有效期已失效，请重新上传",
                status_code=409,
            )

        # 清掉上一轮的进度与快照，再把状态置为排队中：第二个重试请求会看到 pending 而被拒绝
        clear_task(task_id)
        update_task_status(task_id, TASK_STATUS_PENDING)
        save_snapshot(task_id, snapshot(task_id))

    background_tasks.add_task(
        submit_import_graph,
        task_id=task_id,
        local_file_path=str(source_file),
        local_dir=str(task_dir),
        document_kind=document_kind,
        document_version=document_version,
        valid_until=valid_until,
    )
    logger.info(f"导入任务[{task_id}]已重新提交：{source_file.name}")
    return TaskActionResponseSchema(code=200, message="已重新提交导入", task_id=task_id)


@router.delete(
    "/records/{task_id}",
    response_model=TaskActionResponseSchema,
    dependencies=[Depends(require_import_admin_token)],
)
def delete_import_record(task_id: str) -> TaskActionResponseSchema:
    """删除一条导入记录：清理任务状态、磁盘快照与任务目录。

    已入库的向量不会被删除；本接口只处理导入记录与本地中间产物。
    进行中的任务不允许删除，避免产出无人跟踪的导入。
    """
    if not is_valid_task_id(task_id):
        raise ApiError("invalid_task_id", "任务 ID 不合法", status_code=422)

    with _STATE_CHANGE_LOCK:
        if get_task_status(task_id) in (TASK_STATUS_PENDING, TASK_STATUS_PROCESSING):
            raise ApiError("import_in_progress", "任务正在执行，暂不能删除", status_code=409)

        clear_task(task_id)
        delete_snapshot(task_id)
        delete_task_artifacts(task_id)
    logger.info(f"导入记录已删除：task_id={task_id}")
    return TaskActionResponseSchema(code=200, message="导入记录已删除", task_id=task_id)
