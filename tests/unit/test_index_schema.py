"""存储层测试：切片对齐护栏、父块仓储降级行为。"""
from __future__ import annotations

import pytest

from app.rag.import_.index_service import (
    CHUNK_SCALAR_DEFAULTS,
    CHUNK_VARCHAR_LIMITS,
    _align_to_schema,
)
from app.rag.import_.parent_index_service import index_parent_chunks
from app.shared.clients.parent_chunk_repository import parent_chunk_repository


def make_row(**overrides) -> dict:
    row = {
        "chunk_id": "a" * 40,
        "doc_id": "b" * 40,
        "parent_id": "c" * 40,
        "file_title": "手册",
        "item_name": "HAK 180",
        "title": "标题",
        "parent_title": "标题",
        "heading_path": "章节 / 标题",
        "part": 1,
        "seq": 0,
        "page": 1,
        "content": "正文",
        "content_hash": "d" * 40,
        "dense_vector": [0.1, 0.2],
        "sparse_vector": {1: 0.5},
    }
    row.update(overrides)
    return row


def test_align_keeps_every_schema_field():
    aligned = _align_to_schema([make_row()])[0]
    assert set(aligned) == set(CHUNK_VARCHAR_LIMITS) | set(CHUNK_SCALAR_DEFAULTS) | {
        "dense_vector", "sparse_vector",
    }


def test_align_truncates_varchar_by_bytes():
    """Milvus 的 VARCHAR 长度按字节计，超长标题必须截断而非插入失败。"""
    aligned = _align_to_schema([make_row(title="中" * 400)])[0]
    assert len(aligned["title"].encode("utf-8")) <= CHUNK_VARCHAR_LIMITS["title"]
    assert aligned["title"] == "中" * (CHUNK_VARCHAR_LIMITS["title"] // 3)


def test_align_fills_missing_scalars_with_defaults():
    row = make_row()
    for field_name in CHUNK_SCALAR_DEFAULTS:
        row.pop(field_name)
    aligned = _align_to_schema([row])[0]
    assert aligned["part"] == CHUNK_SCALAR_DEFAULTS["part"]
    assert aligned["seq"] == CHUNK_SCALAR_DEFAULTS["seq"]
    assert aligned["page"] == CHUNK_SCALAR_DEFAULTS["page"]


def test_align_accepts_part_beyond_int8_range():
    """part 已由 INT8 升为 INT16：单章节切块数超过 127 不应溢出。"""
    aligned = _align_to_schema([make_row(part=200)])[0]
    assert aligned["part"] == 200


def test_align_drops_fields_outside_schema():
    aligned = _align_to_schema([make_row(unknown_field="x")])[0]
    assert "unknown_field" not in aligned


def test_align_fails_when_vector_missing():
    row = make_row()
    row.pop("dense_vector")
    with pytest.raises(ValueError):
        _align_to_schema([row])


def test_align_converts_none_text_fields_to_empty_string():
    aligned = _align_to_schema([make_row(item_name=None, parent_id=None)])[0]
    assert aligned["item_name"] == ""
    assert aligned["parent_id"] == ""


def test_index_parent_chunks_skips_when_empty():
    assert index_parent_chunks({"parent_chunks": []}) == 0


def test_index_parent_chunks_degrades_when_mongo_unavailable():
    """Mongo 不可用时父块写入失败但不得抛错（父块属上下文增强，不能阻断导入）。"""
    parents = [{"parent_id": "p1", "doc_id": "d1", "title": "t", "content": "c"}]
    assert index_parent_chunks({"parent_chunks": parents}) == 0


def test_fetch_parents_degrades_when_mongo_unavailable():
    """offline_guard 下 get_mongo_client 抛错，仓储必须返回空字典而不是抛出。"""
    assert parent_chunk_repository.fetch_by_ids(["p1", "p2"]) == {}
    assert parent_chunk_repository.fetch_by_ids([]) == {}
