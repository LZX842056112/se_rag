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


def test_active_query_rejects_history_clear_and_second_post(api):
    owner,_,_=api
    assert owner.post("/api/query",json={"query":"q","session_id":"busy-api"}).status_code==200
    turn=lifecycle.reserve_query("busy-api","next")
    try:
        assert owner.post("/api/query",json={"query":"overlap","session_id":"busy-api"}).status_code==409
        assert owner.delete("/api/history/busy-api").status_code==409
    finally:
        lifecycle.release_query("busy-api",turn)
