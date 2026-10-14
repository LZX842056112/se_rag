"""导入对账回归：必须容忍 Milvus 写入可见性延迟，不得把延迟误报成「对账不符」。

回归背景（真实事故）：导入完成后 `upsert_count=16` 已成功，但紧接着的对账查询在 55ms 后
执行，读到 0 条并打出 ERROR「对账不符：Milvus 子块 0 条 != 期望 16 条」——纯属假警报。
根因是 Milvus 的 `query` 对尚未 seal 的 growing segment 存在可见性延迟（本仓
`rag_eval/runner` 已记录同一现象：仅靠短睡眠重试可能稳定读到 0 行）。

第二个缺陷：MinIO 观察项误用 `md_path` 的 stem。`md_path` 在上游已被替换为 `<stem>_new.md`，
因此日志打出 `stem=hak180产品安全手册_new`，与实际上传前缀 `hak180产品安全手册` 不符。
"""
from __future__ import annotations

import pytest

from app.rag.import_ import reconcile as rec


class _FakeMilvus:
    """按脚本返回查询结果，并记录 flush 次数。"""

    def __init__(self, results: list[list[str]]):
        self.results = list(results)
        self.queries = 0
        self.flushed = 0

    def query(self, **_kwargs) -> list[dict]:
        self.queries += 1
        index = min(self.queries - 1, len(self.results) - 1)
        return [{"chunk_id": cid} for cid in self.results[index]]

    def flush(self, collection_name: str | None = None) -> None:
        self.flushed += 1


class _FakeGateway:
    chunk_collection_name = "kb_chunks"

    def __init__(self, client):
        self.milvus_client = client


def _patch(monkeypatch, results: list[list[str]]):
    client = _FakeMilvus(results)
    monkeypatch.setattr(rec, "milvus_gateway", _FakeGateway(client))
    monkeypatch.setattr(rec, "_MILVUS_QUERY_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(rec, "_count_images", lambda stem: None)
    monkeypatch.setattr(rec.settings.import_, "reconcile_enabled", True)
    return client


def _state(chunk_ids: list[str]) -> dict:
    return {
        "file_title": "hak180产品安全手册",
        "chunks": [{"chunk_id": cid} for cid in chunk_ids],
        "parent_chunks": [],
    }


def test_visibility_lag_is_not_reported_as_mismatch(monkeypatch):
    """首次查询读不到（可见性延迟），flush 重试后读到 → 不得报差异。"""
    client = _patch(monkeypatch, [[], [], ["a", "b", "c"]])

    diffs = rec.reconcile_after_import(_state(["a", "b", "c"]))

    assert diffs == {}, f"可见性延迟不应产生差异，实际 {diffs}"
    assert client.flushed == 1, "第二次尝试前应 flush 一次"
    assert client.queries >= 2


def test_stale_rows_alone_do_not_fake_a_pass(monkeypatch):
    """只有上一次导入的旧块可见时，集合比对必须仍判定为「缺失」，而不是假通过。"""
    client = _patch(monkeypatch, [["old1", "old2", "old3"]])

    diffs = rec.reconcile_after_import(_state(["new1", "new2", "new3"]))

    assert "milvus_chunks" in diffs
    assert diffs["milvus_chunks"]["missing"] == 3
    assert client.queries == rec._MILVUS_QUERY_ATTEMPTS, "应重试到上限才判定失败"


def test_partial_visibility_is_reported(monkeypatch):
    """部分子块可见 → 差异里要给出缺失条数。"""
    _patch(monkeypatch, [["a", "b"]])

    diffs = rec.reconcile_after_import(_state(["a", "b", "c"]))

    assert diffs["milvus_chunks"] == {"expected": 3, "missing": 1}


def test_all_visible_passes_without_extra_retry(monkeypatch):
    """一次就全部可见时不 flush、不重试（避免给导入热路径增加无谓开销）。"""
    client = _patch(monkeypatch, [["a", "b"]])

    diffs = rec.reconcile_after_import(_state(["a", "b"]))

    assert diffs == {}
    assert client.queries == 1
    assert client.flushed == 0


def test_query_error_skips_reconcile_without_diff(monkeypatch):
    """查询持续报错时按「无法判定」处理，不得报差异。"""
    client = _patch(monkeypatch, [[]])

    def _boom(**_kwargs):
        client.queries += 1
        raise RuntimeError("Milvus 不可用")

    monkeypatch.setattr(client, "query", _boom)

    diffs = rec.reconcile_after_import(_state(["a"]))

    assert diffs == {}


def test_query_limit_is_not_judged(monkeypatch):
    """命中查询上限时无法判断完整性，只观察、不报差异。"""
    monkeypatch.setattr(rec, "_RECONCILE_QUERY_LIMIT", 3)
    _patch(monkeypatch, [["a", "b", "c"]])

    diffs = rec.reconcile_after_import(_state(["a", "b", "c", "d"]))

    assert diffs == {}


def test_minio_observation_uses_original_file_title(monkeypatch):
    """MinIO 观察项必须用原始 file_title 前缀（md_path 已被替换为 `<stem>_new.md`）。"""
    _patch(monkeypatch, [["a"]])
    seen: list[str] = []
    monkeypatch.setattr(rec, "_count_images", lambda stem: seen.append(stem) or 2)

    rec.reconcile_after_import({
        "file_title": "hak180产品安全手册",
        "chunks": [{"chunk_id": "a"}],
        "parent_chunks": [],
        "md_path": "/tmp/hak180产品安全手册_new.md",
    })

    assert seen == ["hak180产品安全手册"], f"应使用原始 stem，实际 {seen}"


def test_mongo_parent_mismatch_is_reported(monkeypatch):
    """Mongo 父块条数不符仍要报差异（Mongo 强一致，无可见性延迟）。"""
    _patch(monkeypatch, [["a"]])
    monkeypatch.setattr(rec, "_count_parent_chunks", lambda doc_id: 5)

    diffs = rec.reconcile_after_import({
        "file_title": "手册",
        "chunks": [{"chunk_id": "a"}],
        "parent_chunks": [{"parent_id": "p1", "doc_id": "d1"}],
    })

    assert diffs["mongo_parents"] == {"expected": 1, "actual": 5}


def test_disabled_reconcile_does_nothing(monkeypatch):
    client = _patch(monkeypatch, [["a"]])
    monkeypatch.setattr(rec.settings.import_, "reconcile_enabled", False)

    assert rec.reconcile_after_import(_state(["a"])) == {}
    assert client.queries == 0
