"""确定性切片标识测试：稳定性、消歧、字节截断。"""
from __future__ import annotations

from app.rag.import_.ids import (
    make_chunk_id,
    make_content_hash,
    make_doc_id,
    make_parent_id,
    section_key,
    truncate_utf8,
)


def test_doc_id_is_deterministic_and_case_insensitive():
    assert make_doc_id("HAK 180 手册") == make_doc_id("  hak 180 手册  ")


def test_doc_id_differs_across_documents():
    assert make_doc_id("手册A") != make_doc_id("手册B")


def test_chunk_id_stable_across_repeated_calls():
    """同一结构重复计算必须得到同一 id，重导入后标注/引用才不会失效。"""
    first = make_chunk_id("doc", "设备", 0, 1)
    second = make_chunk_id("doc", "设备", 0, 1)
    assert first == second
    assert len(first) == 40


def test_chunk_id_ignores_content_changes():
    """id 只编码结构：内容变化时 id 不变，由 content_hash 感知。"""
    assert make_chunk_id("doc", "设备", 0, 1) == make_chunk_id("doc", "设备", 0, 1)
    assert make_content_hash("内容A") != make_content_hash("内容B")


def test_occurrence_disambiguates_duplicate_headings():
    """重名标题（如手册中的多个「设备」）必须得到不同 id。"""
    first = make_parent_id("doc", "设备", 0)
    second = make_parent_id("doc", "设备", 1)
    assert first != second
    assert section_key("设备", 0) != section_key("设备", 1)


def test_parent_and_chunk_ids_do_not_collide():
    assert make_parent_id("doc", "设备", 0) != make_chunk_id("doc", "设备", 0, 1)


def test_different_parts_get_different_ids():
    assert make_chunk_id("doc", "设备", 0, 1) != make_chunk_id("doc", "设备", 0, 2)


def test_truncate_utf8_keeps_multibyte_characters_intact():
    """Milvus VARCHAR 按字节计长度，截断不得产生半个汉字。"""
    result = truncate_utf8("中文" * 100, 10)
    assert result == "中文中"
    assert len(result.encode("utf-8")) <= 10


def test_truncate_utf8_returns_input_when_within_limit():
    assert truncate_utf8("短文本", 512) == "短文本"
    assert truncate_utf8("", 10) == ""
