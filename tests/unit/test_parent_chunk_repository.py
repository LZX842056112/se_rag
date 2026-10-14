"""父块写入原子性回归：按 parent_id upsert，再清理本次未产出的旧父块。

回归背景：历史实现是 `delete_many({doc_id})` → `insert_many`。删除成功、插入失败会让
该文档的章节背景全部消失。`parent_id` 上有唯一索引，因此不能盲目 insert-then-delete。
"""
from __future__ import annotations

import mongomock

from app.shared.clients import parent_chunk_repository as repo_module


def _repo(monkeypatch) -> object:
    collection = mongomock.MongoClient()["test_db"]["kb_parent_chunks"]
    monkeypatch.setattr(
        repo_module.ParentChunkRepository, "_collection", property(lambda self: collection)
    )
    repo = repo_module.ParentChunkRepository()
    repo._test_collection = collection  # noqa: SLF001 - 测试内直连以便断言
    return repo


def test_second_write_overwrites_and_prunes_stale(monkeypatch):
    """第二次写入只应保留本次产出的父块，且内容被覆盖。"""
    repo = _repo(monkeypatch)

    repo.replace_document_parents(
        "d1",
        [
            {"parent_id": "p1", "doc_id": "d1", "content": "v1"},
            {"parent_id": "p2", "doc_id": "d1", "content": "v2"},
        ],
    )
    repo.replace_document_parents("d1", [{"parent_id": "p1", "doc_id": "d1", "content": "v1b"}])

    rows = list(repo._test_collection.find({"doc_id": "d1"}))
    assert [row["parent_id"] for row in rows] == ["p1"], "本次未产出的 p2 应被清理"
    assert rows[0]["content"] == "v1b", "同 parent_id 的内容应被覆盖"


def test_duplicate_parent_id_does_not_raise(monkeypatch):
    """唯一索引下重复写入同 parent_id 不得抛 DuplicateKeyError。"""
    repo = _repo(monkeypatch)

    for _ in range(3):
        repo.replace_document_parents("d1", [{"parent_id": "p1", "doc_id": "d1", "content": "x"}])

    assert repo.count_by_doc("d1") == 1


def test_empty_parents_clears_document(monkeypatch):
    """父块为空时保持原「清空该文档」语义。"""
    repo = _repo(monkeypatch)

    repo.replace_document_parents("d1", [{"parent_id": "p1", "doc_id": "d1", "content": "v"}])
    written = repo.replace_document_parents("d1", [])

    assert written == 0
    assert repo.count_by_doc("d1") == 0
