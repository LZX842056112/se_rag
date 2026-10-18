import json
from pathlib import Path
from app.rag_eval.full_flow import validate_cases, evaluate_cases


def test_draft_corpus_contains_at_least_200_unique_cases():
    cases=json.loads(Path("app/rag_eval/artifacts/intent_trial_draft.json").read_text(encoding="utf-8"))
    info=validate_cases(cases)
    assert info["case_count"] >= 200
    assert set(info["categories"]) >= {"product", "chitchat", "mixed", "followup", "complaint"}
    assert info["ready_for_acceptance"] is False


def test_full_flow_replays_history_without_injecting_gold():
    calls=[]
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"session_id":"s", "intent":"rag", "answer":"安装说明", "item_names":["HAK180"], "metrics":{"llm_calls":2,"cost_usd":None}}
    class Client:
        def post(self, path, json):
            calls.append(json)
            assert "item_names" not in json and "rewritten_query" not in json
            return Response()
    cases=[{"case_id":"one", "category":"followup", "query":"怎么安装", "history":["HAK180是什么"], "expected_intent":"rag", "expected_items":["HAK180"], "must_contain":["安装"], "critical":True, "annotation_status":"reviewed", "reviewed_by":"tester"}]
    result=evaluate_cases(cases,Client(),revision="test")
    assert len(calls)==2
    assert calls[0]["query"]=="HAK180是什么"
    assert result["summary"]["knowledge_recall"]==1
    assert result["routing_gate_passed"] is False  # 少于200条，不能声称通过验收。
