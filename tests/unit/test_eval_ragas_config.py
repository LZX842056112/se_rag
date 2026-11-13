"""RAGAS judge 配置校验（不导入 RAGAS）。"""
from __future__ import annotations

import pytest

from app.rag_eval.ragas_metrics import judge_config
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
