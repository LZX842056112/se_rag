"""接地性「未评估」语义测试。

回归背景：证据为空 / 模型调用失败 / 输出不可解析时，旧实现一律返回 0.0，
前端渲染「回答置信度 0%」，与「答案确实不接地」不可区分（实测被当成系统故障）。
现在统一返回 ``None``，前端显示「未评估」。
"""
from __future__ import annotations

from app.evolution.online_eval.grounding import compute_groundedness
from app.rag.query.answer_service import backfill_evolution_outputs
from app.shared.config import settings
from app.shared.models import llm_providers


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content


class _Chain:
    def __init__(self, content: str = "", error: Exception | None = None) -> None:
        self._content = content
        self._error = error

    def invoke(self, _messages):
        if self._error is not None:
            raise self._error
        return _Response(self._content)


def test_missing_evidence_or_answer_is_not_scored():
    assert compute_groundedness("答案", []) is None
    assert compute_groundedness("", ["证据"]) is None


def test_llm_failure_degrades_to_none(monkeypatch):
    monkeypatch.setattr(llm_providers, "chat", lambda *a, **k: _Chain(error=RuntimeError("模型不可用")))
    assert compute_groundedness("答案", ["证据"]) is None


def test_unparsable_output_degrades_to_none(monkeypatch):
    monkeypatch.setattr(llm_providers, "chat", lambda *a, **k: _Chain(content="这不是 JSON"))
    assert compute_groundedness("答案", ["证据"]) is None


def test_successful_evaluation_returns_score(monkeypatch):
    monkeypatch.setattr(
        llm_providers, "chat",
        lambda *a, **k: _Chain(content='{"groundedness": 0.8, "unsupported": []}'),
    )
    assert compute_groundedness("答案", ["证据"]) == 0.8


def test_backfill_marks_unevaluated_when_evolution_disabled(monkeypatch):
    """未开启自进化时不做接地性评估 → None（而不是 0，避免误报「完全不接地」）。"""
    monkeypatch.setattr(settings.evolution, "enabled", False)
    state = {
        "answer": "答案",
        "reranked_docs": [
            {"chunk_id": 1, "type": "milvus", "source": "milvus", "text": "正文"},
            {"chunk_id": None, "type": "web", "source": "web", "url": "https://example.com/a",
             "title": "网页", "text": "联网片段"},
        ],
    }
    backfill_evolution_outputs(state)
    assert state["groundedness"] is None
    # 联网来源参与引用，但不进 cited_chunk_ids（不能把 URL 当成知识库主键）
    assert [c["source"] for c in state["citations"]] == ["kb", "web"]
    assert state["cited_chunk_ids"] == ["1"]
    assert state["retrieval_signals"]["web_hit"] is True
