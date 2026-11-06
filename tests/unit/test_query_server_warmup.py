"""启动预热回归：冷启动成本必须移出首个查询的请求预算。

回归背景：首个 Agentic 查询把 60 秒预算耗在 BGE-M3/重排模型加载上，预算到期后
Milvus、Mongo 与 LLM 调用全部抛 `agentic query deadline exceeded`，整轮问答失败。
"""
from __future__ import annotations

from app.api.http import query_server
from app.shared.models import embedding, reranker


def test_warmup_loads_embedding_and_reranker(monkeypatch):
    """两个单例都要在启动阶段加载，否则首个查询仍要等冷启动。"""
    calls: list[str] = []
    monkeypatch.setattr(embedding, "get_bge_m3_ef", lambda: calls.append("embedding"))
    monkeypatch.setattr(reranker, "get_reranker_model", lambda: calls.append("reranker"))

    query_server.warmup_models()

    assert calls == ["embedding", "reranker"]


def test_warmup_failure_does_not_block_startup(monkeypatch):
    """模型缺失或显存不足时只告警，服务仍要能启动。"""
    def _boom():
        raise RuntimeError("模型权重缺失")

    monkeypatch.setattr(embedding, "get_bge_m3_ef", _boom)

    query_server.warmup_models()  # 不应抛出异常
