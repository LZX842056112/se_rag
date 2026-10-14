"""上传入口加固回归：鉴权 fail-closed、文件数与大小限制、文件头嗅探。

回归背景：
- 上传与状态查询历史上完全无鉴权，网络内任何人可上传文档污染知识库；
- 接口收的是文件列表却只处理 ``files[0]``，其余文件被**静默丢弃**；
- 无文件大小上限，也无内容校验（改后缀即可绕过）。
"""
from __future__ import annotations

import io

import pytest
from fastapi import UploadFile

from app.api.errors import ApiError
from app.api.routers import import_ as import_router


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


def test_multiple_files_are_rejected_not_silently_dropped(token):
    """多个文件必须显式拒绝，不能只取第一个、静默丢弃其余。"""
    with pytest.raises(ApiError) as exc:
        import_router.validate_upload_files([_upload("a.md"), _upload("b.md")])
    assert exc.value.code == "too_many_files"
    assert exc.value.status_code == 422


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
    assert import_router.validate_upload_files([_upload("a.md")]).filename == "a.md"


# ---------------- 文件头嗅探 ----------------

def test_pdf_with_wrong_signature_rejected(token):
    """改后缀不能绕过：内容不是 %PDF- 一律拒绝。"""
    with pytest.raises(ApiError) as exc:
        import_router.validate_upload_files([_upload("a.pdf", b"not a real pdf")])
    assert exc.value.code == "file_content_mismatch"
    assert exc.value.status_code == 422


def test_valid_pdf_passes(token):
    assert import_router.validate_upload_files([_upload("a.pdf", b"%PDF-1.7\n...")]).filename == "a.pdf"


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
