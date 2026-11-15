"""HyDE 分流开关回归：默认关闭时行为零变化；开启后短事实型查询跳过假设答案生成。

背景：HyDE 每次无条件调用 LLM 生成假设答案。对短事实型查询（「配对码是多少」
「额定功率」）有害——模型可能生成具体但错误的答案，把检索语义拉向错误区域，
且 RRF 给 HyDE 与知识库等权，噪声可与真实召回等权融合。故加默认关闭的开关，
不改变既有检索效果。
"""
from __future__ import annotations

import pytest

from app.process.query.agent.nodes.node_search_embedding_hyde import node_search_embedding_hyde
from app.rag.query import hyde_search_service as hyde
from app.rag.query import chunk_search
from app.shared.runtime.query_metrics import QueryMetrics, current_metrics


def test_gate_disabled_by_default_never_skips(monkeypatch):
    """开关关闭时恒不跳过——保证默认行为零变化。"""
    monkeypatch.setattr(hyde, "HYDE_GATE_ENABLED", False)
    assert hyde._skip_hyde("HAK180 配对码是多少") is False


def test_gate_skips_short_factual_query(monkeypatch):
    """开启后：短 + 事实型标记 → 跳过。"""
    monkeypatch.setattr(hyde, "HYDE_GATE_ENABLED", True)
    monkeypatch.setattr(hyde, "HYDE_GATE_SHORT_CHARS", 24)
    assert hyde._skip_hyde("HAK180 配对码是多少") is True


def test_gate_keeps_long_query(monkeypatch):
    """开启后：超长查询即使含事实词也不跳过（长句更可能有词汇鸿沟）。"""
    monkeypatch.setattr(hyde, "HYDE_GATE_ENABLED", True)
    monkeypatch.setattr(hyde, "HYDE_GATE_SHORT_CHARS", 10)
    assert hyde._skip_hyde("这台机器在连续工作八小时后为什么会出现温度报警") is False


def test_gate_keeps_short_non_factual_query(monkeypatch):
    """开启后：短但非事实型（口语化提问）仍走 HyDE。"""
    monkeypatch.setattr(hyde, "HYDE_GATE_ENABLED", True)
    assert hyde._skip_hyde("怎么安装") is False


def test_search_by_hyde_skips_llm_when_gated(monkeypatch):
    """分流命中时不调用 LLM，直接返回该路空结果。"""
    monkeypatch.setattr(hyde, "HYDE_GATE_ENABLED", True)
    calls = {"n": 0}

    def _count(_query):
        calls["n"] += 1
        return "假设答案"

    monkeypatch.setattr(hyde, "call_llm_answer", _count)

    result = hyde.search_by_hyde({"item_names": ["HAK 180"], "rewritten_query": "配对码是多少"})

    assert result == []
    assert calls["n"] == 0, "分流命中时不得调用假设答案生成"


@pytest.mark.parametrize(("gate", "milvus_result", "outcome"), [
    (True, None, "skipped"), (False, None, "failed"), (False, [[]], "empty"),
    (False, [[{"entity": {"chunk_id": "a"}}]], "succeeded"),
])
def test_hyde_reports_gate_and_milvus_outcomes(monkeypatch, gate, milvus_result, outcome):
    monkeypatch.setattr(hyde, "HYDE_GATE_ENABLED", gate)
    monkeypatch.setattr(hyde, "HYDE_GATE_SHORT_CHARS", 24)
    monkeypatch.setattr(hyde, "call_llm_answer", lambda _query: "假设答案")
    monkeypatch.setattr(chunk_search, "expand_item_names", lambda names: names)
    monkeypatch.setattr(chunk_search.llm_providers, "embed_text", lambda _: {"dense": [[0.1]], "sparse": [{1: 0.5}]})
    monkeypatch.setattr(chunk_search.milvus_gateway, "hybrid_search", lambda **_: milvus_result)
    metrics = QueryMetrics("s", "t")
    token = current_metrics.set(metrics)
    try:
        result = node_search_embedding_hyde({"item_names": ["HAK 180"], "rewritten_query": "配对码是多少"})
    finally:
        current_metrics.reset(token)
    assert result.get("hyde_executed", False) is not gate
    assert metrics.document("rag", "ok")["stage_outcomes"] == {"hyde": outcome}
    assert bool(result["hyde_embedding_chunks"]) == (outcome == "succeeded")
