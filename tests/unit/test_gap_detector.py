"""缺口检测测试：点踩事件（adopt=None, thumbs<0）必须进入缺口扫描。"""
from __future__ import annotations

import time

import mongomock
import pytest

from app.evolution import repositories as repositories_module
from app.evolution.gap.detector import grade_signals, scan_unresolved_feedbacks
from app.evolution.models import GapSignal
from app.shared.config import settings


@pytest.fixture
def fake_mongo(monkeypatch):
    """用 mongomock 替换集合适配器，避免真实连库。"""
    client = mongomock.MongoClient()
    db = client["test_db"]
    monkeypatch.setattr(repositories_module, "get_collection", lambda name: db[name])
    return db


def test_scan_consumes_thumbs_down_event(fake_mongo, monkeypatch):
    """回归 D1：用户点踩（adopt 未设置）必须被扫描消费并产出 strong 缺口。"""
    monkeypatch.setattr(settings.evolution, "enabled", True)
    fake_mongo[settings.mongo.fb_events_collection].insert_one({
        "session_id": "sess-thumbs",
        "query": "e2e_ui 保修期是多少？",
        "cited_chunk_ids": ["c1"],
        "adopt": None,
        "thumbs": -1,
        "source": "kb",
        "ts": time.time(),
    })

    gaps = scan_unresolved_feedbacks(batch=10)

    assert len(gaps) == 1, "点踩事件未被缺口扫描消费"
    assert gaps[0].status == "candidate"
    assert gaps[0].confidence >= settings.evolution.gap_strong_threshold
    assert fake_mongo[settings.mongo.k_gaps_collection].count_documents({}) == 1


def test_scan_ignores_positive_feedback(fake_mongo, monkeypatch):
    monkeypatch.setattr(settings.evolution, "enabled", True)
    fake_mongo[settings.mongo.fb_events_collection].insert_one({
        "session_id": "sess-like",
        "query": "e2e_ui 温度范围？",
        "cited_chunk_ids": ["c1"],
        "adopt": True,
        "thumbs": 1,
        "source": "kb",
        "ts": time.time(),
    })
    assert scan_unresolved_feedbacks(batch=10) == []


def test_grade_signals_thresholds():
    grade, confidence = grade_signals(GapSignal(user=1.0, generation=1.0))
    assert grade == "strong" and confidence == pytest.approx(0.7, abs=1e-6)
    assert grade_signals(GapSignal())[0] == "none"
