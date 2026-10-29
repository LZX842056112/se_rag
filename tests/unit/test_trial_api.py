from types import SimpleNamespace
import mongomock
import pytest
from fastapi.testclient import TestClient
from app.api.http import query_server
from app.api import session_access
from app.rag.query import pipeline, lifecycle
from app.shared.clients import mongo, history_repository as history_module
from app.shared.config import settings
from app.evolution import repositories as evolution_module, issues

@pytest.fixture
def api(monkeypatch):
    db=mongomock.MongoClient().trial
    for module in (mongo, history_module, session_access, evolution_module, issues):
        monkeypatch.setattr(module,"get_collection",lambda name:db[name])
    monkeypatch.setattr(settings.api,"session_ownership_enabled",True)
    monkeypatch.setattr(settings.evolution,"enabled",True)
    monkeypatch.setenv("EVOLUTION_ADMIN_TOKEN","test-admin")
    def run(state):
        intent="chitchat" if state["original_query"]=="你好" else "rag"
        repo=history_module.history_repository
        repo.save_message(session_id=state["session_id"],role="user",text=state["original_query"],intent=intent,turn_id=state["turn_id"])
        state["answer_id"]=repo.save_message(session_id=state["session_id"],role="assistant",text="answer",query=state["original_query"],intent=intent,turn_id=state["turn_id"],groundedness=None)
        state.update(answer="answer",intent=intent)
        return state
    monkeypatch.setattr(pipeline,"query_app",SimpleNamespace(invoke=run))
    return TestClient(query_server.app), TestClient(query_server.app), db


def test_api_ownership_ids_and_history(api):
    owner, other, db=api
    response=owner.post("/api/query",json={"query":"你好","session_id":"owned"})
    assert response.status_code==200, response.text
    result=response.json()
    assert result["answer_id"] and result["turn_id"]
    cookie=response.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    history=owner.get("/api/history/owned").json()["items"]
    assert history[0]["id"]==result["answer_id"]
    assert history[0]["groundedness"] is None
    assert history[0]["turn_id"]==result["turn_id"]
    for path in ("/api/history/owned","/api/query/status/owned","/api/stream/owned"):
        assert other.get(path).status_code==404
    assert other.delete("/api/history/owned").status_code==404
    assert owner.post("/api/evolution/feedback",json={"session_id":"owned","answer_id":result["answer_id"],"thumbs":-1}).status_code==200
    assert db[settings.mongo.fb_events_collection].count_documents({})==0


def test_approval_reads_require_admin_token(api):
    owner,_,_=api
    for path in ("/api/evolution/candidates","/api/evolution/status","/api/evolution/issues","/api/evolution/trial-metrics"):
        assert owner.get(path).status_code==401
        response=owner.get(path,headers={"X-Internal-Token":"test-admin"})
        assert response.status_code==200,response.text


def test_feedback_choices_survive_history_reload(api):
    owner, _, db = api
    answer = owner.post("/api/query", json={"query": "功率是多少", "session_id": "feedback-refresh"}).json()
    body = {"session_id": "feedback-refresh", "answer_id": answer["answer_id"]}
    assert owner.post("/api/evolution/feedback", json={**body, "thumbs": 1}).status_code == 200
    assert owner.post("/api/evolution/feedback", json={**body, "resolved": False}).status_code == 200
    history = owner.get("/api/history/feedback-refresh").json()["items"]
    assistant = next(item for item in history if item["role"] == "assistant")
    assert assistant["feedback"] == {"thumbs": 1, "resolved": False}
    assert next(item for item in history if item["role"] == "user")["feedback"] is None
    assert db[settings.mongo.query_feedback_collection].count_documents({}) == 1


@pytest.mark.parametrize("legacy_intent", [None, ""])
def test_legacy_answer_feedback_is_saved_without_creating_knowledge_signals(api, legacy_intent):
    from bson import ObjectId
    owner, _, db = api
    answer = owner.post("/api/query", json={"query": "旧版问题", "session_id": "legacy-feedback"}).json()
    selector = {"_id": ObjectId(answer["answer_id"])}
    update = {"$unset": {"intent": ""}} if legacy_intent is None else {"$set": {"intent": legacy_intent}}
    db[settings.mongo.chat_message_collection].update_one(selector, update)
    assert owner.post("/api/evolution/feedback", json={
        "session_id": "legacy-feedback", "answer_id": answer["answer_id"], "thumbs": -1,
    }).status_code == 200
    history = owner.get("/api/history/legacy-feedback").json()["items"]
    assert next(item for item in history if item["role"] == "assistant")["feedback"] == {"thumbs": -1, "resolved": None}
    assert db[settings.mongo.fb_events_collection].count_documents({}) == 0
    assert db[settings.mongo.query_issues_collection].count_documents({}) == 0


def test_automatic_failure_signal_is_not_a_user_feedback_choice(api):
    owner, _, db = api
    answer = owner.post("/api/query", json={"query": "功率是多少", "session_id": "auto-feedback"}).json()
    db[settings.mongo.fb_events_collection].insert_one({
        "session_id": "auto-feedback", "answer_id": answer["answer_id"], "thumbs": -1,
    })
    db[settings.mongo.query_feedback_collection].insert_one({
        "session_id": "another-session", "answer_id": answer["answer_id"], "thumbs": 1,
    })
    history = owner.get("/api/history/auto-feedback").json()["items"]
    assert next(item for item in history if item["role"] == "assistant")["feedback"] is None


def test_history_feedback_reads_partial_legacy_rows_without_changing_dimensions(api):
    owner, _, db = api
    answer = owner.post("/api/query", json={"query": "功率是多少", "session_id": "partial-feedback"}).json()
    common = {"session_id": "partial-feedback", "answer_id": answer["answer_id"]}
    db[settings.mongo.query_feedback_collection].insert_many([
        {**common, "thumbs": -1, "ts": 1},
        {**common, "resolved": True, "ts": 2},
        {**common, "thumbs": "invalid", "resolved": "false", "ts": 3},
    ])
    history = owner.get("/api/history/partial-feedback").json()["items"]
    assert next(item for item in history if item["role"] == "assistant")["feedback"] == {"thumbs": -1, "resolved": True}


def test_active_query_rejects_history_clear_and_second_post(api):
    owner,_,_=api
    assert owner.post("/api/query",json={"query":"q","session_id":"busy-api"}).status_code==200
    turn=lifecycle.reserve_query("busy-api","next")
    try:
        assert owner.post("/api/query",json={"query":"overlap","session_id":"busy-api"}).status_code==409
        assert owner.delete("/api/history/busy-api").status_code==409
    finally:
        lifecycle.release_query("busy-api",turn)


def test_gap_listing_and_manual_review_require_admin(api):
    owner, _, db = api
    gap_id = db[settings.mongo.k_gaps_collection].insert_one({
        "session_id": "s", "query": "HAK180的功率是多少？", "item_names": ["HAK180"],
        "gap_kind": "reading_miss", "status": "strategy_pending", "ts": 1,
        "processing_reason": "阅读遗漏，等待核实",
    }).inserted_id
    path = "/api/evolution/gaps/" + str(gap_id) + "/review"
    body = {"disposition": "knowledge_missing", "reason": "已核对手册，需要补充事实"}
    assert owner.get("/api/evolution/gaps").status_code == 401
    assert owner.post(path, json=body).status_code == 401
    headers = {"X-Internal-Token": "test-admin"}
    response = owner.get("/api/evolution/gaps", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["processing_reason"] == "阅读遗漏，等待核实"
    assert owner.post(path, json={"disposition": "knowledge_missing", "reason": ""}, headers=headers).status_code == 422
    response = owner.post(path, json=body, headers=headers)
    assert response.status_code == 200, response.text
    candidates = owner.get("/api/evolution/candidates", headers=headers).json()["items"]
    assert len(candidates) == 1 and candidates[0]["status"] == "need_info"
    assert candidates[0]["reason"]


def test_unlinked_candidate_is_hidden_uncounted_and_cannot_be_approved(api):
    owner, _, db = api
    cid = db[settings.mongo.k_candidates_collection].insert_one({
        "faq_question": "HAK180的功率？", "faq_answer": "额定功率为100W。", "status": "draft",
        "registration_status": "staged", "source_refs": ["chunk1"], "item_names": ["HAK180"],
    }).inserted_id
    headers = {"X-Internal-Token": "test-admin"}
    assert owner.get("/api/evolution/candidates", headers=headers).json()["items"] == []
    assert owner.get("/api/evolution/status", headers=headers).json()["candidates"] == {}
    response = owner.post(f"/api/evolution/candidates/{cid}/approve", json={}, headers=headers)
    assert response.status_code == 400
    assert "登记" in response.json()["message"]


def test_admin_resolves_duplicate_issue_records_as_one_work_item(api):
    owner, _, db = api
    for kind in ["missing_material", "reading_failure", "missing_material"]:
        db[settings.mongo.query_issues_collection].insert_one({
            "session_id": "same-session", "query": "烫金机有蓝牙吗", "item_names": ["HAK180"],
            "issue_type": kind, "status": "open", "owner": "", "resolution": "", "created_at": 1,
        })
    headers = {"X-Internal-Token": "test-admin"}
    path = "/api/evolution/issues?status=unresolved"
    assert owner.get(path).status_code == 401
    rows = owner.get(path, headers=headers).json()["items"]
    assert len(rows) == 1 and rows[0]["record_count"] == 3
    update_path = "/api/evolution/issues/" + rows[0]["id"]
    body = {"owner": "员工A", "status": "resolved", "resolution": "已修复并核对答案"}
    assert owner.post(update_path, json=body).status_code == 401
    assert owner.post(update_path, json=body, headers=headers).status_code == 200
    assert owner.get(path, headers=headers).json()["items"] == []
    completed = owner.get("/api/evolution/issues?status=resolved", headers=headers).json()["items"]
    assert len(completed) == 1 and completed[0]["record_count"] == 3


def test_manual_gap_ignore_is_applied_to_existing_repeat_issue_in_admin_api(api):
    owner, _, db = api
    headers = {"X-Internal-Token": "test-admin"}
    gap_id = db[settings.mongo.k_gaps_collection].insert_one({
        "session_id": "s", "query": "烫金机有蓝牙吗", "item_names": ["HAK180"],
        "gap_kind": "reading_miss", "status": "strategy_pending",
    }).inserted_id
    response = owner.post(f"/api/evolution/gaps/{gap_id}/review", headers=headers,
                          json={"disposition": "dismissed", "reason": "不用"})
    assert response.status_code == 200, response.text
    issue_id = db[settings.mongo.query_issues_collection].insert_one({
        "session_id": "s", "query": "烫金机有蓝牙吗", "item_names": ["HAK180"],
        "status": "open", "created_at": 1, "owner": "", "resolution": "",
    }).inserted_id
    assert owner.get("/api/evolution/issues?status=unresolved", headers=headers).json()["items"] == []
    rows = owner.get("/api/evolution/issues?status=dismissed", headers=headers).json()["items"]
    assert len(rows) == 1 and rows[0]["suppression_reason"] == "不用"
    assert rows[0]["dismissed_by_gap_id"] == str(gap_id)
    response = owner.post(f"/api/evolution/issues/{issue_id}", headers=headers,
                          json={"owner": "员工A", "status": "resolved", "resolution": "误操作"})
    assert response.status_code == 400 and "忽略" in response.json()["message"]


def test_admin_can_fill_and_approve_manual_knowledge_without_chunk_ids(api, monkeypatch):
    from app.evolution import governance
    owner, _, db = api
    headers = {"X-Internal-Token": "test-admin"}
    captured = []
    monkeypatch.setattr(governance, "index_upsert", lambda evo_id, item: captured.append(item) or True)
    monkeypatch.setattr(governance, "resolve_source", lambda ref: pytest.fail("人工补充不应要求来源分块"))
    cid = db[settings.mongo.k_candidates_collection].insert_one({
        "faq_question": "测试产品支持蓝牙吗", "faq_answer": "", "item_names": ["测试产品"],
        "status": "need_info", "source_refs": [], "source_versions": [], "content_version": 1,
    }).inserted_id
    path = f"/api/evolution/candidates/{cid}"
    body = {"faq_question": "测试产品支持蓝牙吗", "faq_answer": "测试产品支持蓝牙连接。",
            "source_refs": [], "item_names": ["测试产品"]}
    assert owner.post(path + "/edit", json=body).status_code == 401
    assert owner.post(path + "/edit", json=body, headers=headers).status_code == 200
    items = owner.get("/api/evolution/candidates", headers=headers).json()["items"]
    assert items[0]["knowledge_origin"] == "manual" and items[0]["status"] == "draft"
    assert owner.post(path + "/approve", json={}).status_code == 401
    response = owner.post(path + "/approve", json={}, headers=headers)
    assert response.status_code == 200, response.text
    assert len(captured) == 1 and captured[0].knowledge_origin == "manual"
    assert governance.is_retrievable(db[settings.mongo.k_candidates_collection].find_one({"_id": cid}))
