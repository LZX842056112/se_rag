"""配置层测试：env 解析、单一配置源与关键取值。"""
from __future__ import annotations

from app.shared.config import settings
from app.shared.config.common import env_bool, env_float, env_int, env_str


def test_env_helpers_parse_and_fallback(monkeypatch):
    monkeypatch.setenv("T_ENV_STR", "abc")
    monkeypatch.setenv("T_ENV_BOOL", "Yes")
    monkeypatch.setenv("T_ENV_INT", "12")
    monkeypatch.setenv("T_ENV_FLOAT", "0.75")
    assert env_str("T_ENV_STR") == "abc"
    assert env_bool("T_ENV_BOOL") is True
    assert env_int("T_ENV_INT") == 12
    assert env_float("T_ENV_FLOAT") == 0.75

    # 缺失 / 非法值回退默认值
    assert env_str("T_ENV_MISSING", "d") == "d"
    monkeypatch.setenv("T_ENV_INT_BAD", "not-a-number")
    assert env_int("T_ENV_INT_BAD", 5) == 5
    monkeypatch.setenv("T_ENV_BOOL_BAD", "maybe")
    assert env_bool("T_ENV_BOOL_BAD", True) is True


def test_evolution_collection_has_single_source():
    """Milvus 与自进化配置必须指向同一个集合名（历史问题：两处各读一次 env）。"""
    assert settings.milvus.evolution_collection == settings.evolution.collection


def test_settings_domains_present():
    for name in ("llm", "embedding", "reranker", "milvus", "minio", "mineru", "mcp", "mongo", "api", "evolution", "runtime"):
        assert getattr(settings, name) is not None


def test_task_state_ttl_is_positive():
    assert settings.runtime.task_state_ttl_seconds > 0
