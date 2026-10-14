"""导入任务状态的磁盘快照：进程重启后仍可查询任务的最终状态。

背景：``task_state`` 是纯内存态，服务重启后进行中的任务进度与最终状态全部丢失，
前端轮询只能看到空状态。此处把状态快照落到 ``output/tasks/<task_id>.json``
（``output/`` 已在 ``.gitignore`` 内），内存未命中时由状态接口回退读取。

任何异常只告警，不影响内存态与主链路。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.shared.runtime.logger import logger
from app.shared.utils.paths import PROJECT_ROOT

_SNAPSHOT_DIR = PROJECT_ROOT / "output" / "tasks"


def snapshot_path(task_id: str) -> Path:
    """任务快照文件路径。"""
    return _SNAPSHOT_DIR / f"{task_id}.json"


def save_snapshot(task_id: str, payload: dict[str, Any]) -> None:
    """写入任务快照（失败仅告警）。"""
    if not task_id:
        return
    try:
        _SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        snapshot_path(task_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001 - 快照属增强能力，不可影响任务执行
        logger.warning(f"任务快照写入失败（忽略）：task_id={task_id}：{exc}")


def load_snapshot(task_id: str) -> dict[str, Any] | None:
    """读取任务快照；不存在或损坏时返回 ``None``。"""
    if not task_id:
        return None
    path = snapshot_path(task_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 损坏快照按不存在处理
        logger.warning(f"任务快照读取失败（忽略）：task_id={task_id}：{exc}")
        return None
    return data if isinstance(data, dict) else None
