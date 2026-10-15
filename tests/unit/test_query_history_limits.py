"""会话历史的 limit 钳制与上下文排序回归。

回归背景：
- ``GET /api/history/{sid}`` 的 ``limit`` 历史上无上限，``?limit=1000000`` 可把全量
  消息拖进内存与响应体；
- ``build_history_context`` 按 ``list_recent`` 的「新 → 旧」原样编号，LLM 看到的是
  倒序时间线（序号 1 = 最新），违背对话自然因果顺序，指代消解易误判。
"""
from __future__ import annotations

from app.api.routers import query as query_router
from app.rag.query import history_utils
from app.rag.query.history_utils import build_history_context


def _fake_list_recent(recorder: list):
    def _list_recent(session_id: str, limit: int = 10):
        recorder.append(limit)
        return []
    return _list_recent


def test_history_limit_clamped_to_max(monkeypatch):
    """超大 limit 必须被钳制，不得透传到仓储层。"""
    seen: list[int] = []
    monkeypatch.setattr(query_router.history_repository, "list_recent", _fake_list_recent(seen))

    query_router.get_history("s1", limit=1000000)

    assert seen == [query_router._HISTORY_MAX_LIMIT]


def test_history_limit_lower_bound_clamped(monkeypatch):
    """0 / 负数 limit 也要钳到下限，不得把无意义值透传。"""
    seen: list[int] = []
    monkeypatch.setattr(query_router.history_repository, "list_recent", _fake_list_recent(seen))

    query_router.get_history("s1", limit=0)
    query_router.get_history("s1", limit=-5)

    assert seen == [1, 1]


def test_history_default_and_normal_limit_untouched(monkeypatch):
    """默认值与正常范围内的取值不得被改变。"""
    seen: list[int] = []
    monkeypatch.setattr(query_router.history_repository, "list_recent", _fake_list_recent(seen))

    query_router.get_history("s1")
    query_router.get_history("s1", limit=50)

    assert seen == [10, 50]


def test_build_history_context_numbers_oldest_first(monkeypatch):
    """上下文时序必须是旧 → 新：序号 1 对应最早一条。"""
    # list_recent 的对外语义是新 → 旧（ts 倒序），此处按该顺序提供三条
    rows = [
        {"role": "user", "rewritten_query": "最新问题", "item_names": ["A"], "text": "", "ts": 3.0},
        {"role": "assistant", "text": "最新回答", "item_names": ["A"], "rewritten_query": "", "ts": 2.5},
        {"role": "user", "rewritten_query": "最早问题", "item_names": ["A"], "text": "", "ts": 1.0},
    ]
    monkeypatch.setattr(history_utils.history_repository, "list_recent", lambda **kw: list(rows))

    text = build_history_context("s1", limit=10)
    lines = [line for line in text.splitlines() if line.strip()]

    assert "序号:1" in lines[0] and "最早问题" in lines[0], f"首行应为最早一条，实际 {lines[0]}"
    assert "最早问题" not in "\n".join(lines[1:])
    assert "最新问题" in lines[-1], f"末行应为最新一条，实际 {lines[-1]}"


def test_list_recent_keeps_newest_first_order(monkeypatch):
    """对外接口仍是新 → 旧：仓储排序不被本次改动影响（防止误改 list_recent）。"""
    import inspect

    from app.shared.clients import history_repository as repo_module

    source = inspect.getsource(repo_module.HistoryRepository.list_recent)
    assert 'sort("ts", -1)' in source, "list_recent 的排序方向不得被改动"
