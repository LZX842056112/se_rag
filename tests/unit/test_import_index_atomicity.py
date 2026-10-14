"""导入写入原子性回归：断言「先写新数据、再清理残留」，且写入条数要过对账。

回归背景：历史实现是 `delete(file_title)` → `insert`。删除成功但插入失败会让整篇文档
从知识库中消失；且单次全量 insert 不校验返回条数，静默少写也会被当成成功。
"""
from __future__ import annotations

import pytest

from app.rag.import_ import index_service


class _FakeMilvus:
    """记录调用序列的假 Milvus 客户端。"""

    def __init__(self, existing_ids: tuple[str, ...] = (), upsert_delta: int = 0):
        self.calls: list[tuple[str, object]] = []
        self.deleted_filters: list[str] = []
        self.existing_ids = list(existing_ids)
        self.upsert_delta = upsert_delta

    def has_collection(self, collection_name: str) -> bool:
        return True

    def upsert(self, collection_name: str, data: list[dict]) -> dict:
        self.calls.append(("upsert", [row["chunk_id"] for row in data]))
        return {"upsert_count": len(data) + self.upsert_delta}

    def insert(self, collection_name: str, data: list[dict]) -> dict:
        self.calls.append(("insert", len(data)))
        return {"insert_count": len(data)}

    def query(self, collection_name, filter=None, output_fields=None, limit=None) -> list[dict]:
        return [{"chunk_id": cid} for cid in self.existing_ids]

    def delete(self, collection_name: str, filter: str) -> dict:
        self.calls.append(("delete", filter))
        self.deleted_filters.append(filter)
        return {}


class _FakeGateway:
    chunk_collection_name = "kb_chunks"
    item_name_collection_name = "kb_item_names"

    def __init__(self, client: _FakeMilvus):
        self.milvus_client = client


def _row(chunk_id: str) -> dict:
    return {
        "chunk_id": chunk_id,
        "doc_id": "d1",
        "parent_id": "p1",
        "file_title": "手册",
        "content": "正文",
        "dense_vector": [0.1, 0.2],
        "sparse_vector": {1: 0.5},
    }


def _patch(monkeypatch, client: _FakeMilvus) -> None:
    monkeypatch.setattr(index_service, "milvus_gateway", _FakeGateway(client))
    # 隔离父块与对账，聚焦子块写入顺序
    monkeypatch.setattr(index_service, "index_parent_chunks", lambda state: 0)
    monkeypatch.setattr(index_service, "reconcile_after_import", lambda state: {})


def test_writes_new_before_deleting_old(monkeypatch):
    """写入必须先于删除，避免「删成功、插失败」的数据空窗。"""
    client = _FakeMilvus(existing_ids=("keep", "stale1"))
    _patch(monkeypatch, client)

    index_service.index_chunks(
        {"file_title": "手册", "embeddings_content": [_row("keep"), _row("new1")], "parent_chunks": []}
    )

    ops = [name for name, _ in client.calls]
    assert ops[0] == "upsert", f"首次操作应为 upsert，实际 {ops}"
    assert "delete" not in ops[: ops.index("upsert") + 1], "删除不得发生在写入之前"


def test_stale_chunks_are_cleaned_but_kept_ones_survive(monkeypatch):
    """只清理本次未再产出的旧 id；仍在本次结果内的 id 必须保留。"""
    client = _FakeMilvus(existing_ids=("keep", "stale1"))
    _patch(monkeypatch, client)

    index_service.index_chunks(
        {"file_title": "手册", "embeddings_content": [_row("keep"), _row("new1")], "parent_chunks": []}
    )

    joined = " ".join(client.deleted_filters)
    assert "stale1" in joined, "未被本次产出的旧块应被清理"
    assert "'keep'" not in joined, "本次仍产出的块不得被删除"
    assert "'new1'" not in joined, "新写入的块不得被删除"


def test_no_delete_when_nothing_is_stale(monkeypatch):
    """重复导入同一文件：id 完全一致，不应产生任何删除。"""
    client = _FakeMilvus(existing_ids=("a", "b"))
    _patch(monkeypatch, client)

    index_service.index_chunks(
        {"file_title": "手册", "embeddings_content": [_row("a"), _row("b")], "parent_chunks": []}
    )

    assert client.deleted_filters == []


def test_write_count_mismatch_fails_loudly(monkeypatch):
    """写入条数与期望不符必须失败，不能静默当成成功。"""
    client = _FakeMilvus(upsert_delta=-1)
    _patch(monkeypatch, client)

    with pytest.raises(ValueError):
        index_service.index_chunks(
            {"file_title": "手册", "embeddings_content": [_row("a"), _row("b")], "parent_chunks": []}
        )


def test_cleanup_query_failure_does_not_raise(monkeypatch):
    """清理阶段的查询失败只告警：新数据已安全写入，不得因清理失败而失败。"""
    client = _FakeMilvus(existing_ids=("stale1",))
    _patch(monkeypatch, client)

    def _boom(**kwargs):
        raise RuntimeError("查询不可用")

    monkeypatch.setattr(client, "query", _boom)

    index_service.index_chunks(
        {"file_title": "手册", "embeddings_content": [_row("a")], "parent_chunks": []}
    )

    assert client.deleted_filters == []
