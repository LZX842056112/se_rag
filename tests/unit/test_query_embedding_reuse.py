"""性能回归：单次提问的查询向量只编码一次（知识库 + 自进化两路复用）。"""
from __future__ import annotations

from app.process.query.agent.nodes import node_search_embedding as node_module
from app.shared.config import settings
from app.shared.models import llm_providers


def test_rewritten_query_is_embedded_once(monkeypatch):
    calls: list[str] = []

    def fake_embed_text(text: str):
        calls.append(text)
        return {"dense": [[0.0] * 4], "sparse": [{1: 0.5}]}

    monkeypatch.setattr(llm_providers, "embed_text", fake_embed_text)
    monkeypatch.setattr(settings.evolution, "enabled", True)

    # 两路检索都返回空结果，只关心向量化调用次数
    monkeypatch.setattr(node_module, "search_by_embedding", lambda state, embedding=None: [])
    monkeypatch.setattr(node_module, "search_evolution_items",
                        lambda **_kwargs: [])

    state = {
        "session_id": "s1",
        "rewritten_query": "HAK 180 的电源参数",
        "item_names": ["HAK 180"],
        "is_stream": False,
    }
    result = node_module.node_search_embedding(state)

    assert calls == ["HAK 180 的电源参数"], "查询文本应只向量化一次"
    assert result["embedding_chunks"] == [] and result["evolution_chunks"] == []


def test_evolution_recall_failure_degrades_to_empty(monkeypatch):
    """自进化召回异常时必须降级为空，不得中断查询链路。"""
    monkeypatch.setattr(llm_providers, "embed_text", lambda text: {"dense": [[0.0]], "sparse": [{1: 0.1}]})
    monkeypatch.setattr(settings.evolution, "enabled", True)
    monkeypatch.setattr(node_module, "search_by_embedding", lambda state, embedding=None: [])

    def boom(**_kwargs):
        raise RuntimeError("milvus down")

    monkeypatch.setattr(node_module, "search_evolution_items", boom)
    result = node_module.node_search_embedding({
        "session_id": "s2",
        "rewritten_query": "任意问题",
        "item_names": ["任意主体"],
    })
    assert result["evolution_chunks"] == []
