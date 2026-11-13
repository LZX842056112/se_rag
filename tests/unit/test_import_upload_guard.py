"""上传入口加固回归：鉴权 fail-closed、批量上限、大小限制、文件头嗅探。

回归背景：
- 上传与状态查询历史上完全无鉴权，网络内任何人可上传文档污染知识库；
- 接口收的是文件列表却只处理 ``files[0]``，其余文件被**静默丢弃**；
- 无文件大小上限，也无内容校验（改后缀即可绕过）。

当前约定：一次请求可带多个文件，整批校验通过后逐文件建独立任务；
任一文件不合法则整批拒绝，不创建任何任务。
"""
from __future__ import annotations

import io
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from fastapi import BackgroundTasks, UploadFile

from app.api.errors import ApiError
from app.api.routers import import_ as import_router
from app.rag.import_ import pipeline
from app.shared.utils.task_state import TASK_STATUS_PENDING, get_task_status


def _upload(name: str = "a.md", data: bytes = b"# title\n") -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(data))


@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(import_router.settings.import_, "admin_token", "secret-token")
    return "secret-token"


# ---------------- 鉴权 ----------------

def test_token_not_configured_is_fail_closed(monkeypatch):
    """未配置 Token 时拒绝访问（503），避免「忘配即无鉴权」。"""
    monkeypatch.setattr(import_router.settings.import_, "admin_token", "")
    with pytest.raises(ApiError) as exc:
        import_router.require_import_admin_token("")
    assert exc.value.code == "import_admin_token_missing"
    assert exc.value.status_code == 503


def test_token_mismatch_is_unauthorized(token):
    with pytest.raises(ApiError) as exc:
        import_router.require_import_admin_token("wrong")
    assert exc.value.code == "import_unauthorized"
    assert exc.value.status_code == 401


def test_correct_token_passes(token):
    assert import_router.require_import_admin_token(token) is None


# ---------------- 入口校验 ----------------

def test_no_files_rejected(token):
    with pytest.raises(ApiError) as exc:
        import_router.validate_upload_files([])
    assert exc.value.code == "no_files"


def test_multiple_files_pass_validation_as_one_batch(token):
    """多个文件整批通过校验，不能只取第一个、静默丢弃其余。"""
    files = [_upload("a.md"), _upload("b.pdf", b"%PDF-1.7\n...")]
    validated = import_router.validate_upload_files(files)
    assert [item.filename for item in validated] == ["a.md", "b.pdf"]


def test_batch_over_limit_rejected(token):
    files = [_upload(f"f{index}.md") for index in range(import_router.MAX_BATCH_FILES + 1)]
    with pytest.raises(ApiError) as exc:
        import_router.validate_upload_files(files)
    assert exc.value.code == "too_many_files"
    assert exc.value.status_code == 422


def test_batch_rejected_when_any_file_invalid(token):
    """任一文件不合法即整批拒绝，提示里点名具体文件。"""
    with pytest.raises(ApiError) as exc:
        import_router.validate_upload_files([_upload("a.md"), _upload("b.txt", b"text")])
    assert exc.value.code == "unsupported_type"
    assert "b.txt" in exc.value.message


def test_unsupported_suffix_rejected(token):
    with pytest.raises(ApiError) as exc:
        import_router.validate_upload_files([_upload("a.txt")])
    assert exc.value.code == "unsupported_type"


def test_oversize_rejected(token, monkeypatch):
    monkeypatch.setattr(import_router.settings.import_, "max_upload_mb", 1)
    monkeypatch.setattr(import_router, "_measure_size", lambda _f: 2 * 1024 * 1024)
    with pytest.raises(ApiError) as exc:
        import_router.validate_upload_files([_upload("a.md")])
    assert exc.value.code == "file_too_large"
    assert exc.value.status_code == 413


def test_zero_limit_means_unlimited(token, monkeypatch):
    """IMPORT_MAX_UPLOAD_MB <= 0 视为不限制，避免误拒。"""
    monkeypatch.setattr(import_router.settings.import_, "max_upload_mb", 0)
    monkeypatch.setattr(import_router, "_measure_size", lambda _f: 10 ** 9)
    assert import_router.validate_upload_files([_upload("a.md")])[0].filename == "a.md"


# ---------------- 文件头嗅探 ----------------

def test_pdf_with_wrong_signature_rejected(token):
    """改后缀不能绕过：内容不是 %PDF- 一律拒绝。"""
    with pytest.raises(ApiError) as exc:
        import_router.validate_upload_files([_upload("a.pdf", b"not a real pdf")])
    assert exc.value.code == "file_content_mismatch"
    assert exc.value.status_code == 422


def test_valid_pdf_passes(token):
    assert import_router.validate_upload_files([_upload("a.pdf", b"%PDF-1.7\n...")])[0].filename == "a.pdf"


def test_markdown_with_binary_content_rejected(token):
    with pytest.raises(ApiError) as exc:
        import_router.validate_upload_files([_upload("a.md", b"# t\x00\x01\x02binary")])
    assert exc.value.code == "file_content_mismatch"


def test_markdown_with_non_utf8_content_rejected(token):
    with pytest.raises(ApiError) as exc:
        import_router.validate_upload_files([_upload("a.md", b"# \xff\xfe bad")])
    assert exc.value.code == "file_content_mismatch"


def test_signature_check_rewinds_stream(token):
    """嗅探后必须把游标复位，否则落盘会写出空文件。"""
    upload = _upload("a.md", b"# title\nbody")
    import_router.validate_upload_files([upload])
    assert upload.file.read() == b"# title\nbody"


# ---------------- 批量建任务 ----------------

def _task_dir(root, task_id):
    return root / "output" / datetime.now().strftime("%Y%m%d") / task_id


def test_upload_creates_one_task_per_file(tmp_path, monkeypatch, token):
    """一次请求上传多个文件：每个文件独立任务、独立目录与后台任务。"""
    monkeypatch.setattr(import_router, "PROJECT_ROOT", tmp_path)
    background = BackgroundTasks()
    result = import_router.upload(
        background,
        [_upload("a.md", b"# a\n"), _upload("b.pdf", b"%PDF-1.7\n")],
    )
    try:
        assert len(result.task_ids) == 2
        assert result.message == "2 个文件上传成功"
        assert [task.kwargs["task_id"] for task in background.tasks] == result.task_ids
        # 入队前必须置为排队中，否则前端会把空状态误判为「状态不可用」
        assert all(get_task_status(task_id) == TASK_STATUS_PENDING for task_id in result.task_ids)
        for task_id, name, data in zip(
            result.task_ids,
            ("a.md", "b.pdf"),
            (b"# a\n", b"%PDF-1.7\n"),
        ):
            task_dir = _task_dir(tmp_path, task_id)
            assert (task_dir / name).read_bytes() == data
            assert import_router.load_task_meta(task_dir)["file_name"] == name
    finally:
        for task_id in result.task_ids:
            import_router.clear_task(task_id)


def test_upload_rejects_whole_batch_when_one_file_invalid(tmp_path, monkeypatch, token):
    """任一文件不合法：不建任务、不落盘。"""
    monkeypatch.setattr(import_router, "PROJECT_ROOT", tmp_path)
    background = BackgroundTasks()
    with pytest.raises(ApiError) as exc:
        import_router.upload(background, [_upload("a.md"), _upload("b.txt", b"text")])
    assert exc.value.code == "unsupported_type"
    assert "b.txt" in exc.value.message
    assert background.tasks == []
    assert not (tmp_path / "output").exists()


def test_upload_rolls_back_created_tasks_when_disk_write_fails(tmp_path, monkeypatch, token):
    """第二个文件落盘失败时，清理第一个文件已建的任务与目录。"""
    monkeypatch.setattr(import_router, "PROJECT_ROOT", tmp_path)
    calls = 0

    def failing_save(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")

    monkeypatch.setattr(import_router, "save_task_meta", failing_save)
    background = BackgroundTasks()
    with pytest.raises(ApiError) as exc:
        import_router.upload(background, [_upload("a.md"), _upload("b.md")])
    assert exc.value.code == "save_failed"
    assert exc.value.status_code == 500
    assert background.tasks == []
    assert list((tmp_path / "output").glob("*/*")) == []


def test_submit_import_graph_runs_tasks_in_parallel(monkeypatch):
    """线程池调度必须让多个任务同时执行；不能退回 BackgroundTasks 的串行执行。"""
    barrier = threading.Barrier(2, timeout=5)

    def fake_run(task_id, **_kwargs):
        barrier.wait()

    monkeypatch.setattr(pipeline, "invoke_import_graph", fake_run)
    executor = ThreadPoolExecutor(max_workers=2)
    monkeypatch.setattr(pipeline, "_IMPORT_EXECUTOR", executor)
    try:
        first = pipeline.submit_import_graph(task_id="t1", local_file_path="a.md", local_dir="a")
        second = pipeline.submit_import_graph(task_id="t2", local_file_path="b.md", local_dir="b")
        first.result(timeout=10)
        second.result(timeout=10)
    finally:
        executor.shutdown(wait=True)


# ---------------- 状态查询回退 ----------------

def test_status_falls_back_to_disk_snapshot(token, monkeypatch):
    """内存态未命中时读磁盘快照，进程重启后仍能查到最终状态。"""
    monkeypatch.setattr(import_router, "get_task_status", lambda _t: "")
    monkeypatch.setattr(import_router, "get_done_task_list", lambda _t: [])
    monkeypatch.setattr(import_router, "get_running_task_list", lambda _t: [])
    monkeypatch.setattr(import_router, "get_task_error", lambda _t: ("", ""))
    monkeypatch.setattr(
        import_router,
        "load_snapshot",
        lambda _t: {
            "status": "failed",
            "done_list": ["检查文件"],
            "running_list": [],
            "error_code": "mineru_timeout",
            "message": "MinerU 解析超时",
        },
    )

    result = import_router.task_status("t1")

    assert result.status == "failed"
    assert result.error_code == "mineru_timeout"
    assert result.message == "MinerU 解析超时"


def test_status_prefers_memory(token, monkeypatch):
    """内存态命中时不读快照。"""
    monkeypatch.setattr(import_router, "get_task_status", lambda _t: "completed")
    monkeypatch.setattr(import_router, "get_done_task_list", lambda _t: ["处理完成"])
    monkeypatch.setattr(import_router, "get_running_task_list", lambda _t: [])
    monkeypatch.setattr(import_router, "get_task_error", lambda _t: ("", ""))

    def _must_not_read(_t):
        raise AssertionError("内存命中时不应读快照")

    monkeypatch.setattr(import_router, "load_snapshot", _must_not_read)

    result = import_router.task_status("t1")

    assert result.status == "completed"
    assert result.error_code is None
