"""主体名写入顺序回归：`kb_item_names` 是自增主键集合，不能 upsert。

回归背景：历史实现是 `delete(file_title)` → `insert`。删除成功、插入失败时该文档
会彻底没有主体名，查询期就永远无法按主体过滤命中它。
"""
from __future__ import annotations

from app.rag.import_ import item_name_service
from app.rag.item_name import catalog as catalog_module
from app.rag.item_name import match as match_module


class _FakeMilvus:
    def __init__(self, insert_ids: list | None = None):
        self.calls: list[tuple[str, object]] = []
        self.deleted_filters: list[str] = []
        self._insert_ids = insert_ids if insert_ids is not None else [101]

    def insert(self, collection_name: str, data: list[dict]) -> dict:
        self.calls.append(("insert", len(data)))
        return {"insert_count": len(data), "ids": list(self._insert_ids)}

    def delete(self, collection_name: str, filter: str) -> dict:
        self.calls.append(("delete", filter))
        self.deleted_filters.append(filter)
        return {}


class _FakeGateway:
    item_name_collection_name = "kb_item_names"

    def __init__(self, client: _FakeMilvus):
        self.milvus_client = client


def _patch(monkeypatch, client: _FakeMilvus) -> None:
    monkeypatch.setattr(item_name_service, "milvus_gateway", _FakeGateway(client))
    monkeypatch.setattr(
        item_name_service.llm_providers,
        "generate_embeddings",
        lambda texts: {"dense": [[0.1]], "sparse": [{1: 0.5}]},
    )


def test_inserts_before_deleting_and_keeps_new_pk(monkeypatch):
    """必须先插入新记录，再删除旧记录，且删除条件要排除刚写入的自增 pk。"""
    client = _FakeMilvus(insert_ids=[101])
    _patch(monkeypatch, client)

    item_name_service.upsert_item_name("HAK 180 烫金机", "hak180")

    ops = [name for name, _ in client.calls]
    assert ops == ["insert", "delete"], f"调用顺序应为 insert→delete，实际 {ops}"
    assert client.deleted_filters, "应产生一次旧记录清理"
    filter_text = client.deleted_filters[0]
    assert "file_title == 'hak180'" in filter_text
    assert "pk not in [101]" in filter_text, f"删除条件必须排除新 pk，实际 {filter_text}"


def test_falls_back_when_driver_returns_no_ids(monkeypatch):
    """驱动不返回 ids 时退回「先删后插」，保证最终仍只有一条记录。"""
    client = _FakeMilvus(insert_ids=[])
    _patch(monkeypatch, client)

    item_name_service.upsert_item_name("HAK 180 烫金机", "hak180")

    ops = [name for name, _ in client.calls]
    assert ops == ["insert", "delete", "insert"], f"兜底路径应为 insert→delete→insert，实际 {ops}"
    assert client.deleted_filters[0] == "file_title == 'hak180'"


def _catalog(monkeypatch, names: list[str]) -> None:
    monkeypatch.setattr(
        catalog_module,
        "_CACHE",
        {"names": names, "raws": names, "ts": 9e9},
    )


def test_write_merge_keeps_different_model(monkeypatch):
    """库内只有一个主体时，不同型号不能因向量相似度高而被误并。"""
    _catalog(monkeypatch, ["Brother HAK 180 烫金机"])
    called = []
    monkeypatch.setattr(
        match_module,
        "search_by_item_names",
        lambda names: called.append(names) or {},
    )

    resolved, evidence = match_module.resolve_existing_item_name(
        "Brother HL-3040CN网络打印服务器"
    )

    assert resolved == "Brother HL-3040CN网络打印服务器"
    assert evidence is None
    assert called == [], "写入端不应再调用向量检索做主体归并"


def test_write_merge_reuses_same_entity_by_suffix(monkeypatch):
    """品牌前缀差异（短名是长名后缀）仍复用库内标准名。"""
    _catalog(monkeypatch, ["Brother HAK 180 烫金机"])

    resolved, evidence = match_module.resolve_existing_item_name("HAK 180 烫金机")

    assert resolved == "Brother HAK 180 烫金机"
    assert evidence["matched_by"] == "catalog_entity"


def test_write_merge_reuses_exact_normalized_name(monkeypatch):
    """仅空格差异的写法规格化后视为精确同名。"""
    _catalog(monkeypatch, ["Brother HAK 180 烫金机"])

    resolved, evidence = match_module.resolve_existing_item_name("Brother HAK 180烫金机")

    assert resolved == "Brother HAK 180 烫金机"
    assert evidence["matched_by"] == "catalog_exact"
