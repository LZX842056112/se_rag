"""导入链路编排：执行 LangGraph 导入图并维护任务状态。

加固要点（**不引入消息队列**，保持单进程部署形态）：

- **并发闸门**：进程内信号量限制同时执行的导入任务数，避免 ``BackgroundTasks`` 线程池被打满；
- **有界重试**：仅对可重试错误码退避重试；配合 MinerU 产物复用（``checkpoint_enabled``），
  重试不会重复付出云端解析的代价；
- **错误分类**：失败原因落成稳定机器码（见 ``app/rag/import_/errors.py``）；
- **磁盘快照**：状态迁移时落盘，进程重启后状态接口仍可回读最终状态。

已知局限：信号量与快照均为**单进程**语义；多 worker 部署下并发限制不跨进程
（如需跨进程需引入分布式锁，不在本次范围内）。
"""
from __future__ import annotations

import threading
import time

from app.process.import_.agent.main_graph import import_app
from app.process.import_.agent.state import ImportGraphState, create_default_state
from app.rag.import_.errors import classify_error, is_retryable
from app.shared.config import settings
from app.shared.runtime.logger import logger
from app.shared.utils.task_snapshot import save_snapshot
from app.shared.utils.task_state import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PROCESSING,
    set_task_error,
    snapshot,
    update_task_status,
)

_IMPORT_SLOTS: threading.BoundedSemaphore | None = None
_SLOTS_LOCK = threading.Lock()


def _import_slots() -> threading.BoundedSemaphore:
    """进程内导入并发闸门（懒加载，避免在导入期读配置副作用）。"""
    global _IMPORT_SLOTS
    with _SLOTS_LOCK:
        if _IMPORT_SLOTS is None:
            _IMPORT_SLOTS = threading.BoundedSemaphore(
                max(1, settings.import_.max_concurrent_tasks)
            )
        return _IMPORT_SLOTS


def _persist(task_id: str) -> None:
    """把当前内存状态落盘（失败只告警，不影响任务执行）。"""
    payload = snapshot(task_id)
    if payload:
        save_snapshot(task_id, payload)


def _run_graph_once(state: ImportGraphState) -> None:
    """跑一次导入图；不支持的文档类型在此显式抛错。"""
    final_state = import_app.invoke(state)
    # 防御：图对不支持的文档类型会静默走到 END，这里显式校验是否真的解析过该文件
    if not (final_state.get("md_path") or final_state.get("pdf_path")):
        raise ValueError("不支持的文档类型（仅支持 md / pdf）")


def invoke_import_graph(task_id: str, local_file_path: str, local_dir: str, document_kind: str = "manual", document_version: str = "", valid_until: float = 0.0) -> None:
    """执行一次导入：抢并发名额 → 跑图（可重试）→ 落状态与快照。"""
    slots = _import_slots()
    if not slots.acquire(timeout=max(1, settings.import_.acquire_timeout_seconds)):
        update_task_status(task_id, TASK_STATUS_FAILED)
        set_task_error(task_id, "import_busy", "导入并发已满，等待名额超时")
        _persist(task_id)
        logger.warning(f"导入任务[{task_id}]放弃：等待导入名额超时")
        return

    try:
        update_task_status(task_id, TASK_STATUS_PROCESSING)
        _persist(task_id)

        state: ImportGraphState = create_default_state(
            task_id=task_id, local_file_path=local_file_path, local_dir=local_dir,
            document_kind=document_kind, document_version=document_version, valid_until=valid_until
        )

        attempts = max(1, settings.import_.max_retry_attempts)
        for attempt in range(1, attempts + 1):
            try:
                _run_graph_once(state)
            except Exception as exc:  # noqa: BLE001 - 统一分类后落状态
                code, message = classify_error(exc)
                if is_retryable(code) and attempt < attempts:
                    logger.warning(
                        f"导入任务[{task_id}]第 {attempt}/{attempts} 次失败（{code}），"
                        f"退避后重试：{message}"
                    )
                    if settings.import_.retry_backoff_seconds > 0:
                        time.sleep(settings.import_.retry_backoff_seconds * attempt)
                    continue
                logger.exception(f"导入任务[{task_id}]失败（{code}）：{message}")
                update_task_status(task_id, TASK_STATUS_FAILED)
                set_task_error(task_id, code, message)
                _persist(task_id)
                return

            update_task_status(task_id, TASK_STATUS_COMPLETED)
            set_task_error(task_id, "", "")
            _persist(task_id)
            return
    finally:
        slots.release()
    # 说明：这里刻意不立即清理进度记录——前端仍需轮询到最终状态；内存占用由
    # task_state 的 TTL 回收（TASK_STATE_TTL_SECONDS，默认 6 小时）保证不无限增长。
