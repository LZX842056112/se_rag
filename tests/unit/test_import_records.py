"""导入记录管理回归：失败重试、删除单条记录。

回归背景：
- 失败后只能重新选文件上传，MinerU 已生成的 Markdown 无法复用；
- 导入记录只增不减，失败与历史记录无法从页面清理。
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.api.errors import ApiError
from app.api.routers import import_ as import_router
from app.rag.import_ import task_records
from app.shared.utils.task_state import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PENDING,
    TASK_STATUS_PROCESSING,
    clear_task,
    get_task_status,
    update_task_status,
)


class _BackgroundTasks:
    """替身：只记录被调度的后台任务，避免真跑导入图。"""

    def __init__(self):
        self.calls: list[dict] = []

    def add_task(self, _func, **kwargs):
        self.calls.append(kwargs)


@pytest.fixture
def output_root(tmp_path, monkeypatch):
    """把任务目录根指到临时目录，避免读写真实的 output/。"""
    root = tmp_path / "output"
    root.mkdir()
    monkeypatch.setattr(task_records, "_OUTPUT_ROOT", root)
    return root


@pytest.fixture
def task_dir(output_root):
    """构造一个失败任务的工作目录：原 PDF + 上传参数 + 派生的 Markdown。"""
    task_id = str(uuid.uuid4())
    work_dir = output_root / "20261106" / task_id
    work_dir.mkdir(parents=True)
    (work_dir / "manual.pdf").write_bytes(b"%PDF-1.4")
    (work_dir / "manual.md").write_text("# 手册", encoding="utf-8")
    (work_dir / task_records.META_FILE_NAME).write_text(
        json.dumps({
            "file_name": "manual.pdf",
            "document_kind": "manual",
            "document_version": "",
            "valid_until": 0.0,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    yield task_id, work_dir
    clear_task(task_id)


def _patch_snapshot(monkeypatch):
    recorded: dict = {"saved": [], "deleted": []}
    monkeypatch.setattr(import_router, "save_snapshot", lambda task_id, payload: recorded["saved"].append(task_id))
    monkeypatch.setattr(import_router, "delete_snapshot", lambda task_id: recorded["deleted"].append(task_id))
    return recorded


# ---------------- 重试 ----------------

def test_retry_resubmits_failed_task_with_original_file(task_dir, monkeypatch):
    """失败任务复用原任务目录与原文件重跑，不再要求用户重新上传。"""
    task_id, work_dir = task_dir
    recorded = _patch_snapshot(monkeypatch)
    update_task_status(task_id, TASK_STATUS_FAILED)
    background = _BackgroundTasks()

    result = import_router.retry_import(task_id, background)

    assert result.task_id == task_id
    assert len(background.calls) == 1
    call = background.calls[0]
    assert call["task_id"] == task_id
    assert call["local_file_path"] == str(work_dir / "manual.pdf")
    assert call["local_dir"] == str(work_dir)
    assert call["document_kind"] == "manual"
    assert recorded["saved"] == [task_id]
    # 重新提交后状态应立刻是 pending，第二个重试请求会因此被拒绝
    assert get_task_status(task_id) == TASK_STATUS_PENDING


def test_retry_uses_pdf_not_derived_markdown_when_meta_missing(task_dir, monkeypatch):
    """元数据缺失（老任务）时必须优先取 PDF，不能把 MinerU 派生的 Markdown 当原文件。"""
    task_id, work_dir = task_dir
    (work_dir / task_records.META_FILE_NAME).unlink()
    _patch_snapshot(monkeypatch)
    update_task_status(task_id, TASK_STATUS_FAILED)
    background = _BackgroundTasks()

    import_router.retry_import(task_id, background)

    assert background.calls[0]["local_file_path"] == str(work_dir / "manual.pdf")


def test_retry_rejects_completed_task(task_dir, monkeypatch):
    task_id, _ = task_dir
    _patch_snapshot(monkeypatch)
    update_task_status(task_id, TASK_STATUS_COMPLETED)

    with pytest.raises(ApiError) as exc:
        import_router.retry_import(task_id, _BackgroundTasks())
    assert exc.value.code == "import_not_failed"
    assert exc.value.status_code == 409


def test_retry_rejects_running_task(task_dir, monkeypatch):
    """进行中的任务不能重复提交，否则会并行跑两次导入。"""
    task_id, _ = task_dir
    _patch_snapshot(monkeypatch)
    update_task_status(task_id, TASK_STATUS_PROCESSING)

    with pytest.raises(ApiError) as exc:
        import_router.retry_import(task_id, _BackgroundTasks())
    assert exc.value.code == "import_in_progress"
    assert exc.value.status_code == 409


def test_retry_rejects_missing_source_file(output_root, monkeypatch):
    """任务目录被清理后重试应给出「请重新上传」，而不是抛 500。"""
    task_id = str(uuid.uuid4())
    _patch_snapshot(monkeypatch)
    update_task_status(task_id, TASK_STATUS_FAILED)
    try:
        with pytest.raises(ApiError) as exc:
            import_router.retry_import(task_id, _BackgroundTasks())
        assert exc.value.code == "import_source_missing"
        assert exc.value.status_code == 410
    finally:
        clear_task(task_id)


def test_retry_rejects_expired_policy(task_dir, monkeypatch):
    """业务政策在失败期间过期后不得重试，否则会把过期政策写入知识库。"""
    task_id, work_dir = task_dir
    (work_dir / task_records.META_FILE_NAME).write_text(
        json.dumps({
            "file_name": "manual.pdf",
            "document_kind": "policy",
            "document_version": "2020-01",
            "valid_until": 1.0,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    _patch_snapshot(monkeypatch)
    update_task_status(task_id, TASK_STATUS_FAILED)

    with pytest.raises(ApiError) as exc:
        import_router.retry_import(task_id, _BackgroundTasks())
    assert exc.value.code == "policy_metadata_expired"


# ---------------- 删除记录 ----------------

def test_delete_record_removes_state_snapshot_and_artifacts(task_dir, monkeypatch):
    task_id, work_dir = task_dir
    recorded = _patch_snapshot(monkeypatch)
    update_task_status(task_id, TASK_STATUS_COMPLETED)

    result = import_router.delete_import_record(task_id)

    assert result.message == "导入记录已删除"
    assert recorded["deleted"] == [task_id]
    assert not work_dir.exists(), "任务目录应被删除，否则失败任务的中间文件会一直堆积"
    assert get_task_status(task_id) == ""


def test_delete_record_rejects_running_task(task_dir, monkeypatch):
    task_id, work_dir = task_dir
    _patch_snapshot(monkeypatch)
    update_task_status(task_id, TASK_STATUS_PROCESSING)

    with pytest.raises(ApiError) as exc:
        import_router.delete_import_record(task_id)
    assert exc.value.code == "import_in_progress"
    assert work_dir.exists(), "拒绝删除时不得动任务目录"


def test_delete_record_is_idempotent_for_missing_dir(output_root, monkeypatch):
    """目录已不存在时删除仍应成功，便于前端反复清理。"""
    task_id = str(uuid.uuid4())
    _patch_snapshot(monkeypatch)

    result = import_router.delete_import_record(task_id)

    assert result.task_id == task_id


# ---------------- 路径安全 ----------------

def test_invalid_task_id_rejected():
    with pytest.raises(ApiError) as exc:
        import_router.delete_import_record("../../etc/passwd")
    assert exc.value.code == "invalid_task_id"


def test_task_id_must_be_uuid():
    assert task_records.is_valid_task_id(str(uuid.uuid4())) is True
    assert task_records.is_valid_task_id("../../etc/passwd") is False
    assert task_records.is_valid_task_id("") is False
