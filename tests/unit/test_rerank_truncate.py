"""重排输入长度控制：LLM 压缩不足时必须按 token 硬截断。"""
from __future__ import annotations

from app.rag.query import rerank_service
from app.shared.models import llm_providers


class _FakeTokenizer:
    """以空白切词模拟 tokenizer；encode/decode 互逆，便于断言 token 数。"""

    def encode(self, text: str, add_special_tokens: bool = False,
               truncation: bool = False, max_length: int | None = None) -> list[str]:
        tokens = str(text or "").split()
        if truncation and max_length is not None:
            return tokens[:max_length]
        return tokens

    def decode(self, tokens: list[str]) -> str:
        return " ".join(tokens)


class _FakeReranker:
    tokenizer = _FakeTokenizer()


def test_overlong_answer_is_hard_truncated(monkeypatch):
    monkeypatch.setattr(llm_providers, "reranker_model", lambda: _FakeReranker())
    # 压缩无效（原样返回超长文本）时，仍必须满足 token 预算
    monkeypatch.setattr(rerank_service, "_summarize_for_rerank", lambda query, answer, limit: answer)
    monkeypatch.setattr(llm_providers, "chat", lambda *a, **k: None)  # 避免真实建链

    docs = [{"text": " ".join(f"w{i}" for i in range(600))}]
    pairs = rerank_service.create_question_answer_lists("q1 q2", docs)

    assert len(pairs) == 1
    assert len(pairs[0][1].split()) <= rerank_service.RERANK_MAX_INPUT_TOKENS - 4 - 2


def test_short_answer_is_untouched(monkeypatch):
    monkeypatch.setattr(llm_providers, "reranker_model", lambda: _FakeReranker())
    monkeypatch.setattr(llm_providers, "chat", lambda *a, **k: None)

    pairs = rerank_service.create_question_answer_lists("q1", [{"text": "很短的一段答案"}])
    assert pairs[0][1] == "很短的一段答案"
