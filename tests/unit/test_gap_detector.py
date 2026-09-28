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


def _insert_fb(db, *, session: str, query: str, ts: float | None = None) -> None:
    db[settings.mongo.fb_events_collection].insert_one({
        "session_id": session, "query": query, "cited_chunk_ids": [], "adopt": False,
        "thumbs": -1, "source": "kb", "ts": ts if ts is not None else time.time(),
    })


def test_dedup_is_question_scoped_not_session_scoped(fake_mongo, monkeypatch):
    """回归：同一会话里，前一个问题留下 candidate 缺口后，后续新问题仍必须被扫到。

    真实事故：按 session 去重且缺口状态不复位，导致同一会话「后续问题永远扫不到缺口」。
    """
    monkeypatch.setattr(settings.evolution, "enabled", True)
    now = time.time()
    # 该会话已有一个「蓝牙配对码」缺口，且状态停在 candidate（候选通过后未复位的历史数据）
    fake_mongo[settings.mongo.k_gaps_collection].insert_one({
        "session_id": "sess-1", "query": "HAK 180 烫金机的蓝牙配对码是多少？",
        "status": "candidate", "confidence": 0.7, "ts": now - 600,
    })
    # 同一会话提出新问题
    _insert_fb(fake_mongo, session="sess-1", query="HAK180烫金机滚筒压力校准需要哪些工具？")

    gaps = scan_unresolved_feedbacks(batch=10)
    assert len(gaps) == 1, "同一会话的后续新问题被误判为重复"
    assert gaps[0].query.startswith("HAK180烫金机滚筒压力校准")
    assert gaps[0].gap_id, "扫描产出的缺口应带上 Mongo 主键，供候选回写状态"


def test_dedup_skips_same_question_in_window(fake_mongo, monkeypatch):
    monkeypatch.setattr(settings.evolution, "enabled", True)
    _insert_fb(fake_mongo, session="sess-2", query="同一个问题？")
    first = scan_unresolved_feedbacks(batch=10)
    assert len(first) == 1
    _insert_fb(fake_mongo, session="sess-3", query="同一个问题？")  # 不同会话、同一问题
    assert scan_unresolved_feedbacks(batch=10) == []


def test_grade_signals_thresholds():
    grade, confidence = grade_signals(GapSignal(user=1.0, generation=1.0))
    assert grade == "strong" and confidence == pytest.approx(0.7, abs=1e-6)
    assert grade_signals(GapSignal())[0] == "none"
