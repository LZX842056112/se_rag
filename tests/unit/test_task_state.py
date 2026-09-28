"""任务进度状态测试：登记 / 清理 / TTL 回收。"""
from __future__ import annotations

import time

from app.shared.config import settings
from app.shared.utils import task_state


def _reset():
    for task_id in ("t1", "t2", "t3"):
        task_state.clear_task(task_id)


def test_running_then_done_flow():
    _reset()
    task_state.add_running_task("t1", "node_entry")
    assert task_state.get_running_task_list("t1") == ["检查文件"]

    task_state.add_done_task("t1", "node_entry")
    assert task_state.get_running_task_list("t1") == []
    assert task_state.get_done_task_list("t1") == ["检查文件"]


def test_status_and_clear():
    _reset()
    task_state.update_task_status("t2", task_state.TASK_STATUS_PROCESSING)
    assert task_state.get_task_status("t2") == task_state.TASK_STATUS_PROCESSING
    task_state.clear_task("t2")
    assert task_state.get_task_status("t2") == ""


def test_ttl_prunes_stale_tasks(monkeypatch):
    """TTL 到期后任务记录应被回收（修复历史「字典只增不减」的内存泄漏）。"""
    _reset()
    monkeypatch.setattr(settings.runtime, "task_state_ttl_seconds", 0)
    task_state.add_running_task("t3", "node_entry")
    time.sleep(0.01)  # 让 t3 的 updated_at 明确早于下一次写入
    task_state.add_running_task("t1", "node_document_split")  # 触发一次 prune
    assert task_state.get_running_task_list("t3") == []
    assert task_state.get_running_task_list("t1") == ["文档切分"]
