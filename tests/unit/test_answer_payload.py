"""HTTP 与 SSE 共用字段，但保持各自既有的空值和独有字段。"""
from copy import deepcopy
from types import SimpleNamespace

from fastapi import BackgroundTasks


COMMON = {
    "answer": "答", "answer_id": "", "business_tag": "", "handoff_required": False,
    "issue_id": "", "metrics": {}, "image_urls": [], "item_names": [], "citations": [],
    "groundedness": None, "item_name_options": [], "intent": "rag",
}


def test_common_fields_keep_defaults_without_adding_transport_fields():
    from app.rag.query.answer_payload import answer_fields
    assert answer_fields({"answer": "答"}) == COMMON
    explicit = {**COMMON, "answer_id": None, "groundedness": None, "intent": ""}
    assert answer_fields(explicit) == {**COMMON, "answer_id": None}


def test_common_fields_keep_actual_citations_and_image_selection():
    from app.rag.query.answer_payload import answer_fields
    citation = {"source": "kb", "chunk_id": "child-1", "page": 3, "version": "v1"}
    state = {"answer": "答", "citations": [citation], "image_urls": ["https://example.com/step.jpg"],
             "answer_contexts": ["父章节正文"], "evidence_manifest": [{"source_id": "parent-1"}]}
    original = deepcopy(state)
    result = answer_fields(state)
    assert result["citations"] == [citation]
    assert result["image_urls"] == state["image_urls"]
    assert "answer_contexts" not in result and "evidence_manifest" not in result
    assert state == original


def test_http_keeps_null_answer_id_normalization_and_own_fields(monkeypatch):
    from app.api.routers import query as api
    from app.api.schema.query_schema import QueryRequestSchema
    monkeypatch.setattr(api, "authorize_session", lambda *a, **k: None)
    monkeypatch.setattr(api, "reserve_query", lambda *a: "turn-a")
    monkeypatch.setattr(api, "get_done_task_list", lambda *a: ["已完成"])
    monkeypatch.setattr(api, "invoke_query_graph", lambda **k: {"answer": "答", "answer_id": None})
    response = api.query(BackgroundTasks(), QueryRequestSchema(query="问", session_id="session-a", is_stream=False))
    assert response.model_dump() == {
        **COMMON, "message": "已完成问的内容检索", "session_id": "session-a",
        "done_list": ["已完成"], "retrieval_signals": {}, "turn_id": "turn-a",
    }


def test_sse_keeps_null_answer_id_and_finishes_once(monkeypatch):
    from app.rag.query import pipeline
    from app.shared.config import settings
    from app.shared.utils.sse_broker import SSEEvent
    monkeypatch.setattr(settings.agentic, "mode", "legacy")
    monkeypatch.setattr(settings.evolution, "write_enabled", False)
    monkeypatch.setattr(pipeline, "query_app", SimpleNamespace(invoke=lambda state: {"answer": "答", "answer_id": None}))
    monkeypatch.setattr(pipeline, "register_unresolved", lambda state: "")
    monkeypatch.setattr(pipeline, "record_query", lambda *a: None)
    monkeypatch.setattr(pipeline.QueryMetrics, "document", lambda *a: {})
    monkeypatch.setattr(pipeline, "update_task_status", lambda *a, **k: None)
    events, released = [], []
    monkeypatch.setattr(pipeline, "publish", lambda session, kind, payload: events.append((kind, payload)))
    monkeypatch.setattr(pipeline, "release_query", lambda *args: released.append(args))
    assert pipeline.invoke_query_graph("session-a", "问", False, turn_id="turn-a") is not None
    assert events == [
        (SSEEvent.FINAL, {**COMMON, "answer_id": None, "turn_id": "turn-a", "status": "completed"}),
        (SSEEvent.CLOSE, {}),
    ]
    assert released == [("session-a", "turn-a")]
