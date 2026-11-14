import pytest
from fastapi.testclient import TestClient

from app.api.http.query_server import app
from app.api.routers import query
from app.shared.config import settings
from app.shared.runtime.logger import node_log
from app.shared.runtime.query_metrics import QueryMetrics, current_metrics


def test_query_records_receipt_before_dispatch(monkeypatch):
    monkeypatch.setattr(query, "reserve_query", lambda *args: "turn")
    captured = {}

    def invoke(**kwargs):
        captured.update(kwargs)
        return {"answer": "答复"}

    monkeypatch.setattr(query, "invoke_query_graph", invoke)
    response = TestClient(app).post("/api/query", json={"query": "问题"})
    assert response.status_code == 200
    assert captured["queued_at"] > 0


def test_query_metrics_include_queue_and_stage_duration():
    metrics = QueryMetrics("session", "turn", started=20.0, queued_at=19.75)

    @node_log("node_rerank")
    def rerank(state):
        return state

    token = current_metrics.set(metrics)
    try:
        rerank({})
    finally:
        current_metrics.reset(token)

    report = metrics.document("rag", "completed")
    assert report["queue_ms"] == 250.0
    assert report["stage_ms"]["rerank"] >= 0


def test_health_reports_query_provenance_on_request(monkeypatch):
    from app.api.routers.health import query_provenance

    monkeypatch.setattr(settings.evaluation, "service_commit", "build-123")
    monkeypatch.setattr(settings.evaluation, "knowledge_base_version", "kb-abc")
    monkeypatch.setattr(settings.evaluation, "query_worker_count", 2)
    monkeypatch.setattr(app.state, "evaluation", query_provenance("redis"), raising=False)

    client = TestClient(app)
    assert "evaluation" not in client.get("/api/health").json()
    provenance = client.get("/api/health?include_evidence=true").json()["evaluation"]
    assert provenance["service_commit"] == "build-123"
    assert len(provenance["service_source_sha256"]) == 64
    assert provenance["knowledge_base_version"] == "kb-abc"
    assert provenance["worker_count"] == 2
    assert provenance["runtime_backend"] == "redis"


@pytest.mark.parametrize("git_available", [True, False])
def test_source_provenance_tracks_runtime_files_with_or_without_git(tmp_path, monkeypatch, git_available):
    from app.api.routers import health

    code = tmp_path / "app" / "api" / "handler.py"
    prompt = tmp_path / "app" / "resources" / "prompts" / "answer.prompt"
    evaluation = tmp_path / "app" / "rag_eval" / "scorer.py"
    secret = tmp_path / "app" / ".env"
    for path in (code, prompt, evaluation, secret):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("initial", encoding="utf-8")
    monkeypatch.setattr(health, "_ROOT", tmp_path, raising=False)

    def git(command, *_args):
        if git_available and command == "ls-files":
            return "app/resources/prompts/answer.prompt\0app/api/handler.py\0app/rag_eval/scorer.py\0app/.env\0"
        return None

    monkeypatch.setattr(health, "_git", git)
    before = health.query_provenance("memory")["service_source_sha256"]
    evaluation.write_text("new scorer", encoding="utf-8")
    secret.write_text("new secret", encoding="utf-8")
    assert health.query_provenance("memory")["service_source_sha256"] == before
    prompt.write_text("new prompt", encoding="utf-8")
    after = health.query_provenance("memory")["service_source_sha256"]
    assert len(before) == len(after) == 64
    assert before != after
