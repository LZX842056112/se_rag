"""导入任务的工作目录与参数：失败重试、删除记录时定位原文件与产物。

任务目录形如 ``output/<YYYYMMDD>/<task_id>/``，上传文件、MinerU 产物与图片都在其中。
重试直接复用该目录：既不需要用户重新选文件，也能让 ``checkpoint_enabled`` 复用已生成的
Markdown，避免再次付出云端解析的代价。
"""
from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

from app.shared.runtime.logger import logger
from app.shared.utils.paths import PROJECT_ROOT

_OUTPUT_ROOT = PROJECT_ROOT / "output"
# 上传参数（原文件名、资料类型、版本、失效时间）：重试时据此还原，避免政策元数据丢失
META_FILE_NAME = "_import_meta.json"
_SOURCE_SUFFIXES = (".md", ".pdf")


def is_valid_task_id(task_id: str) -> bool:
    """task_id 必须是上传接口生成的 UUID；顺带拦住路径穿越。"""
    try:
        return str(uuid.UUID(str(task_id))).lower() == str(task_id).lower()
    except (ValueError, AttributeError, TypeError):
        return False


def find_task_dir(task_id: str) -> Path | None:
    """按 task_id 找到上传时创建的工作目录；找不到返回 ``None``。

    任务目录固定为 ``output/<日期>/<task_id>``，这里逐层遍历而不使用多级 glob
    （多级通配符在部分 Python 版本上会漏掉末尾的字面路径）。
    """
    if not is_valid_task_id(task_id) or not _OUTPUT_ROOT.is_dir():
        return None
    for date_dir in _OUTPUT_ROOT.iterdir():
        candidate = date_dir / task_id
        if candidate.is_dir():
            return candidate
    return None


def save_task_meta(
    local_dir: Path,
    *,
    file_name: str,
    document_kind: str,
    document_version: str,
    valid_until: float,
) -> None:
    """落盘导入参数，供失败重试时还原（含业务政策的版本与有效期）。"""
    payload = {
        "file_name": file_name,
        "document_kind": document_kind,
        "document_version": document_version,
        "valid_until": valid_until,
    }
    (local_dir / META_FILE_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_task_meta(task_dir: Path) -> dict[str, Any]:
    """读取导入参数；缺失或损坏时返回空字典（重试按产品手册默认值处理）。"""
    path = task_dir / META_FILE_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning(f"导入参数读取失败（按默认值重试）：{path}：{exc}")
        return {}
    return data if isinstance(data, dict) else {}


def find_source_file(task_dir: Path, file_name: str = "") -> Path | None:
    """在任务目录顶层找原始文件。

    元数据缺失时优先 ``.pdf``：MinerU 会把解析产物 ``<stem>.md`` 落在同一目录，
    若先取 ``.md`` 就会把派生文件当成原文件，重试时跳过错位的 PDF 解析。
    """
    if file_name:
        candidate = task_dir / file_name
        if candidate.is_file() and candidate.suffix.lower() in _SOURCE_SUFFIXES:
            return candidate

    candidates = [
        path
        for path in sorted(task_dir.iterdir())
        if path.is_file() and path.suffix.lower() in _SOURCE_SUFFIXES
    ]
    for candidate in candidates:
        if candidate.suffix.lower() == ".pdf":
            return candidate
    return candidates[0] if candidates else None


def delete_task_artifacts(task_id: str) -> bool:
    """删除任务工作目录（原文件、解析产物与图片）；目录不存在视为已删除。"""
    task_dir = find_task_dir(task_id)
    if task_dir is None:
        return True
    try:
        shutil.rmtree(task_dir)
    except OSError as exc:
        logger.warning(f"删除导入任务目录失败（忽略）：{task_dir}：{exc}")
        return False
    return True
