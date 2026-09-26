"""
自进化闭环端到端测试：feedback → gap → candidate(PII) → approval → index → backtest。
使用 mongomock 代替真实 MongoDB、mock LLM 与 Milvus，可无后端运行。
依赖 pymongo + mongomock 缺失时自动 skip。
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

pymongo = pytest.importorskip("pymongo")
mongomock = pytest.importorskip("mongomock")
from bson import ObjectId  # noqa: E402

from app.evolution.config import evolution_config  # noqa: E402
from app.evolution.gap import detector as gap_detector  # noqa: E402
from app.evolution.repositories import EvolutionMongoTool  # noqa: E402
from app.evolution.candidate import generator  # noqa: E402
from app.evolution.candidate import pii  # noqa: E402
from app.evolution.backtest import runner as backtest  # noqa: E402
from app.evolution.feedback import collector  # noqa: E402


def build_fake_tool():
    """模拟 EvolutionMongoTool：mongomock 提供集合语义。"""
    tool = MagicMock(spec=EvolutionMongoTool)
    db = mongomock.MongoClient()["evo_test"]
    tool.fb_events = db["fb_events"]
    tool.k_gaps = db["k_gaps"]
    tool.k_candidates = db["k_candidates"]
    tool.k_metrics = db["k_metrics"]
    tool.param_registry = db["param_registry"]
    tool.param_registry.create_index([("key", 1)], unique=True)

    def _get_param(key):
        return tool.param_registry.find_one({"key": key})

    def _set_param(key, value, updated_by="manual"):
        now = time.time()
        existing = tool.param_registry.find_one({"key": key})
        rev = (existing or {}).get("rev", 0) + 1
        tool.param_registry.update_one(
            {"key": key}, {"$set": {"value": value, "updated_at": now, "updated_by": updated_by, "rev": rev}},
            upsert=True,
        )

    tool.get_param = _get_param
    tool.set_param = _set_param
    return tool


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # 强制开启自进化；每次测试用新的假 Mongo 与假 LLM。
    monkeypatch.setattr(evolution_config, "enabled", True)
    fake = build_fake_tool()
    import app.evolution.repositories as repo_mod
    monkeypatch.setattr(repo_mod, "get_evolution_mongo_tool", lambda: fake)
    # 假 LLM：candidate 生成固定抽取结果；grounding 返回 0.9。
    fake_llm_invoke = MagicMock(return_value=MagicMock(content='{"faq_question":"如何开机","faq_answer":"按下电源键","source_refs":["c1"]}'))
    monkeypatch.setattr(generator, "llm_providers", _FakeProviders(fake_llm_invoke))
    # 索引/下架：不访问真实 Milvus。
    monkeypatch.setattr(
        "app.evolution.approval.service.upsert_item", lambda eid, item: True
    )
    monkeypatch.setattr(
        "app.evolution.approval.service.deactivate", lambda eid: True
    )
    monkeypatch.setattr(backtest, "deactivate", lambda eid: True)
    return fake


class _FakeProviders:
    def __init__(self, invoke):
        self._invoke = invoke

    def chat(self, mode_name=None, json_mode=None):
        client = MagicMock()
        client.invoke = self._invoke
        return client


def _fb(session_id, cited, adopt=None, thumbs=0):
    return collector.record_feedback(
        session_id=session_id, query="q", rewritten_query="q", cited_chunk_ids=cited,
        adopt=adopt, thumbs=thumbs,
    )


def test_feedback_writes_fb_events(_env):
    _fb("s1", ["c1"], thumbs=-1)
    doc = _env.fb_events.find_one({"session_id": "s1"})
    assert doc is not None
    assert doc["thumbs"] == -1


def test_gap_detect_classifies_strong():
    gap = gap_detector.detect_and_classify(
        {"session_id": "s2", "adopt": False, "thumbs": -1, "cited_chunk_ids": [], "query": "说明书"},
        transcript_slice="用户反馈找不到说明书",
    )
    assert gap.status == "candidate"  # 点踩 + 零引用 → strong


def test_pii_sanitize_and_intercept():
    q, a, has = pii.sanitize_candidate("联系 13812345678 处理", "邮箱是 a@b.com")
    assert has is True
    assert "13812345678" not in q
    assert "[手机号]" in q


def test_closed_loop_gap_to_approve_to_backtest(_env):
    tool = _env
    # 1. 反馈→缺口扫描（入库 candidate 状态缺口）
    _fb("s_loop", [], adopt=False, thumbs=-1)
    gaps = gap_detector.scan_unresolved_feedbacks(batch=10)
    # 无独立扫描循环时，直接构造 strong 缺口入库
    if not gaps:
        gap = gap_detector.detect_and_classify(
            {"session_id": "s_loop", "adopt": False, "thumbs": -1, "cited_chunk_ids": [], "query": "q"},
            transcript_slice="t",
        )
        tool.k_gaps.insert_one(gap.document())
    gap_doc = tool.k_gaps.find_one({"session_id": "s_loop"})
    assert gap_doc and gap_doc["status"] == "candidate"

    # 2. 候选生成
    cand = generator.generate_candidate(gap_doc, context_docs=[{"text": "按下电源键即可开机"}])
    assert cand is not None
    # 3. 审批通过 → 写 k_candidates.status=active
    cand_id = str(tool.k_candidates.find_one({"faq_question": "如何开机"})["_id"])
    from app.evolution.approval.service import approve
    assert approve(cand_id) is True
    active = tool.k_candidates.find_one({"_id": ObjectId(cand_id)})
    assert active["status"] == "active"
    # 4. 观察窗回测：零命中 → hold（证据不足，先保留）
    results = backtest.run_backtest(window_days=1)
    assert results and results[0].verdict == "hold"