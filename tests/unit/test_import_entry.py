"""导入入口分派测试（不触达外部服务）。"""
from __future__ import annotations

import pytest

from app.process.import_.agent.state import create_default_state
from app.rag.import_.entry_service import resolve_input_file


def test_md_route():
    state = resolve_input_file(create_default_state(local_file_path="a/b/manual.md"))
    assert state["md_path"] == "a/b/manual.md"
    assert state["is_md_read_enabled"] is True
    assert state["is_pdf_read_enabled"] is False
    assert state["file_title"] == "manual"


def test_pdf_route():
    state = resolve_input_file(create_default_state(local_file_path="a/b/manual.pdf"))
    assert state["pdf_path"] == "a/b/manual.pdf"
    assert state["is_pdf_read_enabled"] is True
    assert state["is_md_read_enabled"] is False


def test_unsupported_type_disables_both_flags():
    state = resolve_input_file(create_default_state(local_file_path="a/b/manual.txt"))
    assert state["is_md_read_enabled"] is False
    assert state["is_pdf_read_enabled"] is False
    assert not state.get("md_path") and not state.get("pdf_path")


def test_missing_path_raises():
    with pytest.raises(ValueError):
        resolve_input_file(create_default_state(local_file_path=""))
