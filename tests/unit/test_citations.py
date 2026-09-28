"""对外引用构造测试（读链路与 API 层共用同一实现）。"""
from __future__ import annotations

from app.rag.query.citations import build_citations


def test_build_citations_marks_source_and_dedupes():
    citations = build_citations(["11", "12", "11", "13"], ["12"])
    assert [c["faq_id"] for c in citations] == ["11", "12", "13"]
    assert [c["source"] for c in citations] == ["kb", "evolution", "kb"]


def test_build_citations_handles_empty_input():
    assert build_citations(None, None) == []
