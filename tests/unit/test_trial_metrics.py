"""内部试用指标：点踩率、token、case_id 覆盖。"""
from types import SimpleNamespace


def collection(rows):
    return SimpleNamespace(find=lambda *a, **kw: SimpleNamespace(limit=lambda n: rows))


def test_trial_report_adds_downvote_tokens_and_case_alignment(monkeypatch):
    import app.evolution.trial_metrics as metrics
    runs = [
        {"intent": "rag", "answer_id": "a1", "case_id": "case-1", "session_id": "s1",
         "query_hash": "q1", "duration_ms": 1000, "first_token_ms": 200, "cost_usd": None,
         "input_tokens": 100, "output_tokens": 20, "llm_calls": 2, "handoff": False},
        {"intent": "rag", "answer_id": "a2", "case_id": "", "session_id": "s2",
         "query_hash": "q2", "duration_ms": 2000, "first_token_ms": 300, "cost_usd": None,
         "input_tokens": 200, "output_tokens": 30, "llm_calls": 3, "handoff": False},
    ]
    feedback = [
        {"answer_id": "a1", "thumbs": -1, "resolved": False, "case_id": "case-1"},
        {"answer_id": "a2", "thumbs": 1, "resolved": True, "case_id": ""},
    ]
    monkeypatch.setattr(metrics, "get_collection", lambda name: collection(
        runs if name == "query_runs" else feedback if name == "query_feedback" else []))
    monkeypatch.setattr(metrics.settings.mongo, "query_runs_collection", "query_runs")
    monkeypatch.setattr(metrics.settings.mongo, "query_feedback_collection", "query_feedback")
    monkeypatch.setattr(metrics.settings.mongo, "query_issues_collection", "query_issues")
    result = metrics.report(24)
    assert result["rag_queries"] == 2
    assert result["downvote_count"] == 1
    assert result["downvote_rate"] == 0.5
    assert result["downvote_feedback_coverage"] == 0.5
    assert result["upvote_count"] == 1
    assert result["case_id_coverage"] == 0.5
    assert result["feedback_case_id_coverage"] == 0.5
    assert result["input_tokens_total"] == 300
    assert result["output_tokens_total"] == 50
    assert result["routes"]["rag"]["cost_coverage"] == 0.0
