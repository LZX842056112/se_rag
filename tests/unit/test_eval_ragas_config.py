"""RAGAS judge 配置、分数和重试。"""
from __future__ import annotations

import json

import pytest

from app.rag_eval.ragas_metrics import aggregate_ragas, judge_config
from app.shared.config import settings


def test_judge_model_must_be_explicit(monkeypatch):
    monkeypatch.setattr(settings.evaluation, "model", "")
    with pytest.raises(ValueError, match="EVAL_LLM_MODEL"):
        judge_config(settings)


def test_judge_config_falls_back_to_chat_provider(monkeypatch):
    monkeypatch.setattr(settings.evaluation, "model", "judge-model")
    monkeypatch.setattr(settings.evaluation, "base_url", "")
    monkeypatch.setattr(settings.evaluation, "api_key", "")
    monkeypatch.setattr(settings.llm, "base_url", "http://llm.local/v1")
    monkeypatch.setattr(settings.llm, "api_key", "secret")

    config, metadata = judge_config(settings)

    assert config.base_url == "http://llm.local/v1"
    assert config.api_key == "secret"
    assert metadata["judge_model"] == "judge-model"
    assert "api_key" not in metadata


def test_judge_timeout_must_be_positive(monkeypatch):
    monkeypatch.setattr(settings.evaluation, "model", "judge-model")
    monkeypatch.setattr(settings.evaluation, "base_url", "http://llm.local/v1")
    monkeypatch.setattr(settings.evaluation, "api_key", "secret")
    monkeypatch.setattr(settings.evaluation, "timeout_seconds", 0)
    with pytest.raises(ValueError, match="EVAL_LLM_TIMEOUT_SECONDS"):
        judge_config(settings)


def test_ragas_aggregation_counts_only_finite_scores():
    rows = [
        {"case_id": "a", "ragas": {"faithfulness": 0.8}},
        {"case_id": "b", "ragas": {"faithfulness": float("nan")}},
        {"case_id": "c", "ragas": {"faithfulness": float("inf")}},
    ]

    summary = aggregate_ragas(rows)
    assert summary["faithfulness"] == 0.8
    assert summary["faithfulness_n"] == 1


@pytest.mark.parametrize(("judge_result", "expected_score"), [
    ("recovered", 1.0), ("truncated_again", None), ("invalid_request", None),
])
def test_scorer_retries_only_output_truncation_with_same_inputs(monkeypatch, judge_result, expected_score):
    monkeypatch.setenv("RAGAS_DO_NOT_TRACK", "true")
    pytest.importorskip("ragas")
    import httpx
    from openai import AsyncOpenAI

    from app.rag_eval.ragas_metrics import RagasScorer

    monkeypatch.setattr(settings.evaluation, "model", "judge-model")
    monkeypatch.setattr(settings.evaluation, "base_url", "https://judge.invalid/v1")
    monkeypatch.setattr(settings.evaluation, "api_key", "test-key")
    answer = "必须使用本设备随附的电源线。"
    outputs = {
        "StatementGeneratorOutput": {"statements": [answer]},
        "NLIStatementOutput": {"statements": [{"statement": answer, "verdict": 1, "reason": "手册明确规定"}]},
        "AnswerRelevanceOutput": {"question": "", "noncommittal": 0},
        "ContextPrecisionOutput": {"reason": "手册明确规定", "verdict": 1},
        "ContextRecallOutput": {"classifications": [{"statement": answer, "reason": "手册明确规定", "attributed": 1}]},
    }
    requests = []

    def judge(request):
        body = json.loads(request.content)
        messages = "\n".join(message["content"] for message in body["messages"])
        name = next(name for name in outputs if name in messages)
        requests.append((name, body))
        finish_reason = "stop"
        if name == "NLIStatementOutput":
            if judge_result == "invalid_request":
                return httpx.Response(400, json={"error": {"message": "Unsupported max_tokens parameter"}})
            if body["max_tokens"] == 4096 or judge_result == "truncated_again":
                finish_reason = "length"
        return httpx.Response(200, json={
            "id": "completion", "object": "chat.completion", "created": 1, "model": body["model"],
            "choices": [{"index": 0, "finish_reason": finish_reason, "message": {
                "role": "assistant",
                "content": json.dumps(outputs[name], ensure_ascii=False) if finish_reason == "stop" else "{",
            }}],
        })

    original_init = AsyncOpenAI.__init__

    def local_judge(self, **kwargs):
        original_init(self, http_client=httpx.AsyncClient(transport=httpx.MockTransport(judge)), **kwargs)

    monkeypatch.setattr(AsyncOpenAI, "__init__", local_judge)
    scorer = RagasScorer(settings)
    try:
        scores = scorer.score("必须使用哪种电源线？", answer, ["请仅使用本设备随附的电源线。"], answer)
    finally:
        scorer.close()

    assert scores["faithfulness"] == expected_score
    assert scores["answer_relevancy"] == 0.0
    assert scores["context_precision"] == scores["context_recall"] == 1.0
    assert all(body["model"] == "judge-model" for _, body in requests)
    if judge_result == "invalid_request":
        assert "Unsupported max_tokens parameter" in scores["error"]
        assert "retries" not in scores
        assert all(body["max_tokens"] == 4096 for _, body in requests)
        return
    assert scores["retries"] == [{
        "metric": "faithfulness", "max_tokens": 8192, "previous_error": "max_tokens",
        "status": "scored" if expected_score is not None else "scorer_failed",
    }]
    assert [name for name, body in requests if body["max_tokens"] == 8192] == [
        "StatementGeneratorOutput", "NLIStatementOutput",
    ]
    for name in ("StatementGeneratorOutput", "NLIStatementOutput"):
        first, retry = [body for called, body in requests if called == name]
        assert first["messages"] == retry["messages"]
