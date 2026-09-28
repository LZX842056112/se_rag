"""自进化持久层测试（mongomock）：集合名取自配置、参数注册表读写、反馈幂等。"""
from __future__ import annotations

import mongomock
import pytest

from app.evolution import repositories as repositories_module
from app.evolution.feedback.collector import record_feedback
from app.evolution.models import FeedbackEvent
from app.shared.config import settings


@pytest.fixture
def fake_mongo(monkeypatch):
    """用 mongomock 替换集合获取函数，避免真实连库。"""
    client = mongomock.MongoClient()
    db = client["test_db"]
    monkeypatch.setattr(repositories_module, "get_collection", lambda name: db[name])
    return db


def test_collections_come_from_settings(fake_mongo):
    repo = repositories_module.EvolutionRepository()
    assert repo.fb_events.name == settings.mongo.fb_events_collection
    assert repo.k_candidates.name == settings.mongo.k_candidates_collection
    assert repo.k_metrics.name == settings.mongo.k_metrics_collection


def test_param_registry_roundtrip(fake_mongo):
    repo = repositories_module.EvolutionRepository()
    repo.set_param("RRF_K", 80, updated_by="manual")
    repo.set_param("RRF_K", 90, updated_by="metric")
    record = repo.get_param("RRF_K")
    assert record["value"] == 90
    assert record["rev"] == 2
    assert repo.all_params() == {"RRF_K": 90}


def test_feedback_is_idempotent_within_window(fake_mongo, monkeypatch):
    monkeypatch.setattr(settings.evolution, "enabled", True)
    event = FeedbackEvent(session_id="s1", query="q1", thumbs=-1, source="kb")
    record_feedback(event)
    record_feedback(event)  # 30 秒窗口内重复提交不应重复落库
    assert fake_mongo[settings.mongo.fb_events_collection].count_documents({}) == 1


def test_feedback_skipped_when_evolution_disabled(fake_mongo, monkeypatch):
    monkeypatch.setattr(settings.evolution, "enabled", False)
    record_feedback(FeedbackEvent(session_id="s2", query="q2", thumbs=1))
    assert fake_mongo[settings.mongo.fb_events_collection].count_documents({}) == 0
