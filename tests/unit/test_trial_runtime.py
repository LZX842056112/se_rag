"""模型预算、业务政策与运行保障。"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.api.errors import ApiError
from app.rag.query import lifecycle, intent_route_service
from app.rag.query.business import classify_business, policy_supported
from app.shared.runtime.query_metrics import QueryMetrics, measure_client
from app.shared.config import settings
from app.evolution.tuning import controller


def test_session_admission_rejects_overlap_and_releases():
    turn = lifecycle.reserve_query("busy-test", "q")
    try:
        with pytest.raises(ApiError) as exc:
            lifecycle.reserve_query("busy-test", "other")
        assert exc.value.status_code == 409
    finally:
        lifecycle.release_query("busy-test", turn)
    second = lifecycle.reserve_query("busy-test", "q")
    assert second != turn
    lifecycle.release_query("busy-test", second)


def test_classifier_deadline_is_total(monkeypatch):
    monkeypatch.setattr(intent_route_service, "INTENT_TIMEOUT_SECONDS", 0.02)
    def slow(_):
        time.sleep(.15)
        return SimpleNamespace(content='{"intent":"chitchat"}')
    monkeypatch.setattr(intent_route_service.llm_providers, "chat", lambda **kw: SimpleNamespace(invoke=slow))
    start = time.perf_counter()
    assert intent_route_service.call_llm_classify_intent("", "hello") == "rag"
    assert time.perf_counter() - start < .12


@pytest.mark.parametrize("query,tag", [("HAK180是什么", "product_knowledge"), ("怎么安装", "troubleshooting"), ("现在多少钱", "pricing"), ("可以退货吗", "after_sales"), ("我要投诉，根本没解决", "complaint")])
def test_business_labels(query, tag):
    assert classify_business(query) == tag


def test_manual_cannot_authorize_price_and_expired_policy_is_rejected():
    assert not policy_supported([{"text": "价格100元", "document_kind": "manual"}], now=100)
    assert not policy_supported([{"document_kind": "policy", "document_version": "v1", "valid_until": 99}], now=100)
    assert policy_supported([{"document_kind": "policy", "document_version": "v1", "valid_until": 101}], now=100)


def test_call_metrics_include_tokens_cost_and_first_text():
    metrics = QueryMetrics("s", "t")
    response = SimpleNamespace(content="answer", usage_metadata={"input_tokens": 100, "output_tokens": 20})
    client = measure_client(SimpleNamespace(invoke=lambda *a, **kw: response), metrics, "model", {"model": {"input": 1, "output": 2}})
    assert client.invoke([]) is response
    metrics.first_text()
    result = metrics.document("rag", "completed")
    assert result["llm_calls"] == 1
    assert result["input_tokens"] == 100
    assert result["output_tokens"] == 20
    assert result["cost_usd"] == pytest.approx(.00014)
    assert result["first_token_ms"] is not None


def test_unknown_model_price_is_unknown_not_zero():
    metrics = QueryMetrics("s", "t")
    client = measure_client(SimpleNamespace(invoke=lambda *a, **kw: SimpleNamespace(content="a", usage_metadata={})), metrics, "unknown", {})
    client.invoke([])
    assert metrics.document("rag", "completed")["cost_usd"] is None


def test_auto_tuning_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(settings.evolution, "enabled", True)
    monkeypatch.setattr(settings.evolution, "auto_tune_enabled", False)
    monkeypatch.setattr(controller, "compute_snapshot", lambda: pytest.fail("disabled tuner must not run"))
    assert controller.adjust_step() is False


def test_tuner_restores_stable_snapshot_instead_of_minimums(monkeypatch):
    monkeypatch.setattr(settings.evolution, "enabled", True)
    monkeypatch.setattr(settings.evolution, "auto_tune_enabled", True)
    monkeypatch.setattr(controller, "_baseline_adopt_rate", lambda _: .8)
    stable={"RRF_K":60,"RRF_TOP":5,"RERANK_TOP_K":6}
    class Cursor:
        def sort(self,*a): return self
        def limit(self,*a): return [{"params_snapshot":stable}]
    monkeypatch.setattr(controller, "evolution_repo", SimpleNamespace(
        fb_events=SimpleNamespace(count_documents=lambda q:100),
        k_metrics=SimpleNamespace(find=lambda q:Cursor())))
    changed={}
    monkeypatch.setattr(controller.param_registry,"set_param",lambda key,value,**kw:changed.update({key:value}))
    assert controller.adjust_step(SimpleNamespace(ts=100,adopt_rate=.1)) is True
    assert changed==stable


def test_hyde_accepts_measured_query_client(monkeypatch):
    from app.rag.query import hyde_search_service
    metrics=QueryMetrics("s", "t")
    client=measure_client(SimpleNamespace(invoke=lambda *a,**kw:SimpleNamespace(content="hypothesis",usage_metadata={})),metrics,"model",{})
    monkeypatch.setattr(hyde_search_service.llm_providers,"chat",lambda:client)
    assert hyde_search_service.call_llm_answer("q") == "hypothesis"
    assert metrics.llm_calls == 1


@pytest.mark.parametrize("fields", [
    {"original_query":"HAK180", "rewrite_hint":"多少钱", "rewritten_query":"HAK180多少钱"},
    {"original_query":"HAK180", "rewritten_query":"HAK180退款政策"},
])
def test_policy_gate_survives_subject_choice_and_pending_resolution(fields):
    from app.rag.query.business import apply_business_policy
    state={"business_tag":"product_knowledge","item_names":["HAK180"],"reranked_docs":[{"text":"价格100元","document_kind":"manual"}],**fields}
    apply_business_policy(state)
    assert state["handoff_required"] is True
    assert state["business_tag"] in ("pricing", "after_sales")
    assert state["reranked_docs"] == []


def test_policy_answer_does_not_reuse_old_assistant_facts(monkeypatch):
    from app.rag.query import answer_service
    monkeypatch.setattr(answer_service,"build_history_context",lambda *a,**kw:pytest.fail("policy answers must not use old assistant facts"))
    prompt=answer_service.load_answer_prompt({"session_id":"s","rewritten_query":"HAK180价格","item_names":["HAK180"],"business_tag":"pricing","reranked_docs":[{"text":"当前价格200元","document_kind":"policy","document_version":"v2","valid_until":9999999999}]})
    assert "200" in prompt


def test_policy_answer_omits_stale_parent_background(monkeypatch):
    from app.rag.query import answer_service
    prompt=answer_service.load_answer_prompt({"session_id":"s","rewritten_query":"价格","item_names":["HAK180"],"business_tag":"pricing","reranked_docs":[{"text":"当前价格200元","parent_content":"旧价格SECRET999","document_kind":"policy"}]})
    assert "SECRET999" not in prompt
    assert "200" in prompt


def test_chitchat_failure_is_recorded_as_degraded(monkeypatch):
    from app.rag.query import chitchat_service
    from app.shared.runtime.query_metrics import current_metrics
    metrics=QueryMetrics("s", "t")
    token=current_metrics.set(metrics)
    def fail(): raise RuntimeError("offline")
    monkeypatch.setattr(chitchat_service.llm_providers,"chat",fail)
    try:
        state={"session_id":"s"}
        chitchat_service.call_llm_chitchat(state,"prompt")
        assert state["answer"]
        assert metrics.document("chitchat","completed")["degraded"] is True
    finally:
        current_metrics.reset(token)


def test_invalid_model_price_does_not_break_query_json():
    metrics=QueryMetrics("s","t")
    response=SimpleNamespace(content="a",usage_metadata={"input_tokens":1,"output_tokens":1})
    client=measure_client(SimpleNamespace(invoke=lambda *a,**kw:response),metrics,"model",{"model":{"input":float("nan"),"output":1}})
    client.invoke([])
    assert metrics.document("rag","completed")["cost_usd"] is None
