import asyncio
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import fakeredis
import mongomock
import pytest
from fastapi.testclient import TestClient

from app.shared.clients import redis_gateway
from app.shared.config import settings


@pytest.fixture(params=["memory", "redis"])
def backend(request, monkeypatch):
    monkeypatch.setattr(settings.runtime, "state_backend", request.param)
    if request.param == "redis":
        client = fakeredis.FakeRedis(decode_responses=True)
        monkeypatch.setattr(redis_gateway, "get_redis_client", lambda: client)


def test_workflow_subscribers_see_parallel_nodes_and_retry(backend):
    from app.shared.runtime.workflow_monitor import Run, observe
    from app.shared.runtime.workflow_events import subscribe, list_runs

    async def check():
        run = Run("test:parallel", "test-id")
        run.begin("pending", "测试运行")
        async with subscribe(run.scope) as first, subscribe(run.scope) as second:
            assert (await asyncio.to_thread(list_runs, run.scope))[0]["status"] == "pending"
            barrier = threading.Barrier(3)
            release = threading.Event()

            def node(name, fail=False):
                with run.activate(), observe(name):
                    barrier.wait(timeout=5)
                    assert release.wait(5)
                    if fail:
                        raise ValueError("private error")

            with ThreadPoolExecutor() as pool:
                a = pool.submit(node, "left", True)
                b = pool.submit(node, "right")
                try:
                    await asyncio.to_thread(barrier.wait, 5)
                    for receiver in (first, second):
                        while True:
                            event, data = await asyncio.wait_for(receiver.get(), 3)
                            assert event == "run"
                            if set(data["nodes"]) == {"left", "right"}:
                                assert all(node["status"] == "running" for node in data["nodes"].values())
                                break
                finally:
                    release.set()
                with pytest.raises(ValueError):
                    a.result(timeout=5)
                b.result(timeout=5)
            failed = list_runs(run.scope)[0]
            assert failed["nodes"]["left"]["status"] == "failed"
            assert failed["nodes"]["right"]["status"] == "completed"
            assert "private error" not in str(failed)
            run.status("retrying")
            assert list_runs(run.scope)[0]["nodes"]["left"]["status"] == "retrying"
            run.begin("processing")
            retry = list_runs(run.scope)[0]
            assert retry["id"] == failed["id"]
            assert retry["attempt"] == 2
            assert retry["revision"] > failed["revision"]
            assert retry["nodes"] == {}
            run.remove()
            assert list_runs(run.scope) == []

    asyncio.run(check())


def test_workflow_list_keeps_active_runs_and_latest_50_ended(backend):
    from app.shared.runtime.workflow_monitor import Run
    from app.shared.runtime.workflow_events import list_runs

    Run("test:retention", "active").begin("waiting")
    for index in range(52):
        run = Run("test:retention", str(index))
        run.begin("processing")
        run.status("completed")
    rows = list_runs("test:retention")
    assert rows[0]["id"] == "active"
    assert [row["id"] for row in rows[1:]] == [str(i) for i in range(51, 1, -1)]


def test_long_running_node_can_finish_after_snapshot_expiry(backend, monkeypatch):
    import time
    from app.shared.runtime.workflow_monitor import Run
    from app.shared.runtime.workflow_events import list_runs

    now = time.time()
    monkeypatch.setattr(settings.runtime, "task_state_ttl_seconds", 10)
    monkeypatch.setattr(time, "time", lambda: now)
    scope = "test:expiry:" + uuid.uuid4().hex
    run = Run(scope, "run")
    run.begin("processing")
    run.node("long", "running")
    now += 11
    assert list_runs(scope) == []
    run.node("long", "completed")
    run.status("completed")
    assert list_runs(scope)[0]["status"] == "completed"

    stale = Run(scope, "run")
    run.remove()
    stale.begin("processing")
    assert list_runs(scope) == []


def test_workflow_graphs_include_conditional_edges_parallel_join_and_agentic_loop():
    from app.api.workflow_graphs import workflow_graph

    imports = workflow_graph("import")
    edges = {(edge["source"], edge["target"]) for edge in imports["edges"]}
    assert ("node_entry", "node_pdf_to_md") in edges
    assert ("node_entry", "node_md_img") in edges
    query = workflow_graph("query")
    edges = {(edge["source"], edge["target"]) for edge in query["edges"]}
    assert ("node_search_embedding", "node_rrf") in edges
    assert ("node_search_embedding_hyde", "node_rrf") in edges
    assert ("node_search_embedding", "node_web_search_mcp") in edges
    assert ("agentic:verify", "agentic:draft") in edges
    assert ("agentic:bootstrap", "agentic:verify") not in edges
    for graph in (imports, query, workflow_graph("evolution-cycle"), workflow_graph("evolution-candidate")):
        ids = {node["id"] for node in graph["nodes"]}
        assert all(edge["source"] in ids and edge["target"] in ids for edge in graph["edges"])


def test_import_automatic_retry_resets_failed_node_and_keeps_task_id(monkeypatch):
    from app.rag.import_ import pipeline
    from app.shared.runtime.workflow_events import list_runs
    from app.shared.runtime.workflow_monitor import IMPORT, observe

    task_id = str(uuid.uuid4())
    monkeypatch.setattr(pipeline, "_acquire_slot", lambda _task_id: True)
    monkeypatch.setattr(pipeline, "_release_slot", lambda _task_id: None)
    monkeypatch.setattr(pipeline, "save_snapshot", lambda *_args: None)
    monkeypatch.setattr(pipeline, "classify_error", lambda _exc: ("mineru_timeout", "timeout"))
    monkeypatch.setattr(pipeline, "is_retryable", lambda _code: True)
    monkeypatch.setattr(settings.import_, "max_retry_attempts", 2)
    monkeypatch.setattr(settings.import_, "retry_backoff_seconds", 0)
    attempts = []

    def process(_state):
        row = next(row for row in list_runs(IMPORT) if row["id"] == task_id)
        attempts.append(row["attempt"])
        with observe("node_pdf_to_md"):
            if len(attempts) == 1:
                raise TimeoutError()

    monkeypatch.setattr(pipeline, "_run_graph_once", process)
    pipeline.invoke_import_graph(task_id, "test.pdf", ".")
    row = next(row for row in list_runs(IMPORT) if row["id"] == task_id)
    assert attempts == [1, 2]
    assert row["status"] == "completed"
    assert row["nodes"]["node_pdf_to_md"]["status"] == "completed"
    assert row["nodes"]["node_pdf_to_md"]["visits"] == 1


def test_scheduled_cycle_marks_disabled_stages_and_reports_real_publication(monkeypatch):
    from app.evolution import governance, scheduler
    from app.shared.runtime.workflow_events import list_runs
    from app.shared.runtime.workflow_monitor import CYCLE

    monkeypatch.setattr(settings.evolution, "write_enabled", True)
    monkeypatch.setattr(settings.evolution, "backtest_enabled", False)
    monkeypatch.setattr(settings.evolution, "auto_tune_enabled", False)
    monkeypatch.setattr(scheduler, "reconcile_registrations", lambda _limit: None)
    monkeypatch.setattr(scheduler, "scan_unresolved_feedbacks", lambda **_kwargs: [])
    monkeypatch.setattr(scheduler, "run_scan_and_generate_once", lambda: {"failed": 0})
    monkeypatch.setattr(scheduler, "_LAST_METRIC_TS", float("inf"))
    published = []

    def publish():
        row = list_runs(CYCLE)[0]
        published.append(row["nodes"]["publication"]["status"])
        return {"failed": 0}

    monkeypatch.setattr(governance, "run_publication_tasks", publish)
    monkeypatch.setattr(scheduler, "run_metrics_and_tuning_once", lambda: pytest.fail("metric executed"))
    asyncio.run(scheduler.run_scheduled_cycle())
    row = list_runs(CYCLE)[0]
    assert published == ["running"]
    assert row["status"] == "completed"
    assert {key: row["nodes"][key]["status"] for key in ("metrics", "tuning", "backtest")} == {
        "metrics": "skipped", "tuning": "skipped", "backtest": "skipped",
    }


def test_scheduler_cancellation_during_publication_does_not_start_gap_scan(monkeypatch):
    from app.evolution import governance, scheduler
    from app.shared.runtime.workflow_events import list_runs
    from app.shared.runtime.workflow_monitor import CYCLE

    started, release = threading.Event(), threading.Event()
    ran = []

    def publication():
        started.set()
        assert release.wait(5)
        return {"failed": 0}

    monkeypatch.setattr(governance, "run_publication_tasks", publication)
    monkeypatch.setattr(scheduler, "run_evolution_cycle_once", lambda: ran.append(True))

    async def check():
        task = asyncio.create_task(scheduler.run_scheduled_cycle())
        try:
            assert await asyncio.to_thread(started.wait, 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    asyncio.run(check())
    assert ran == []
    assert list_runs(CYCLE)[0]["status"] == "interrupted"


def test_workflow_routes_and_independent_authorization(monkeypatch):
    from app.api import session_access
    from app.api.http import import_server, query_server

    mongo = mongomock.MongoClient().test
    monkeypatch.setattr(session_access, "get_collection", lambda name: mongo[name])
    monkeypatch.setattr(settings.api, "session_ownership_enabled", True)
    monkeypatch.setattr(settings.api, "app_env", "dev")
    monkeypatch.setattr(settings.import_, "admin_token", "test-import")
    monkeypatch.setenv("EVOLUTION_ADMIN_TOKEN", "test-evolution")
    client = TestClient(query_server.app)
    assert client.get("/workflows").status_code == 200
    assert client.get("/static/workflows.js").status_code == 200
    assert client.get("/api/workflows/query/events").status_code == 422
    assert client.get("/api/workflows/evolution-cycle/events", headers={"X-Internal-Token": "test-import"}).status_code == 401
    imports = TestClient(import_server.app)
    assert imports.get("/api/workflows/import/events", headers={"X-Internal-Token": "test-evolution"}).status_code == 401
    assert client.post("/api/workflows/internal/candidate-refresh", headers={"X-Internal-Token": "test-evolution"}).status_code == 401
    assert client.post("/api/workflows/internal/candidate-refresh", headers={"X-Internal-Token": "test-import"}).status_code == 204
    monkeypatch.setattr(settings.api, "session_ownership_enabled", False)
    assert client.get("/api/workflows/query/events?session_id=shared").status_code == 503


def test_query_scope_uses_browser_owner_for_first_run(monkeypatch):
    from app.api import session_access
    from app.api.http.query_server import app
    from app.api.routers import query
    from app.shared.runtime.workflow_events import list_runs, subscribe
    from app.shared.runtime.workflow_monitor import query_scope

    mongo = mongomock.MongoClient().test
    monkeypatch.setattr(session_access, "get_collection", lambda name: mongo[name])
    monkeypatch.setattr(settings.api, "session_ownership_enabled", True)
    monkeypatch.setattr(settings.api, "app_env", "dev")
    monkeypatch.setattr(query, "invoke_query_graph", lambda **_: {"answer": "ok"})
    alice = TestClient(app)
    bob = TestClient(app)
    sid = uuid.uuid4().hex
    alice.get("/workflows")
    bob.get("/workflows")
    alice_scope = query_scope(sid, session_access.hashlib.sha256(alice.cookies["kb_owner"].encode()).hexdigest())
    bob_scope = query_scope(sid, session_access.hashlib.sha256(bob.cookies["kb_owner"].encode()).hexdigest())

    async def check():
        async with subscribe(alice_scope) as receiver, subscribe(bob_scope) as other:
            response = await asyncio.to_thread(alice.post, "/api/query", json={"query": "test", "session_id": sid})
            assert response.status_code == 200
            event, row = await asyncio.wait_for(receiver.get(), 2)
            assert event == "run" and row["id"] == response.json()["turn_id"]
            assert list_runs(bob_scope) == []
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(other.get(), 0.02)
        assert bob.get(f"/api/workflows/query/events?session_id={sid}").status_code == 404

    asyncio.run(check())


def test_candidate_stream_refresh_contains_run_updates_and_removals():
    from app.api.routers.workflows import _events
    from app.shared.runtime.workflow_events import write
    from app.shared.runtime.workflow_monitor import CANDIDATE

    rows = []

    async def check():
        stream = _events(CANDIDATE, CANDIDATE, lambda: rows.copy())
        try:
            assert (await anext(stream)).startswith("event: snapshot\n")
            rows[:] = [{"id": "feedback", "status": "waiting", "revision": 0, "attempt": 1, "updated_at": 1}]
            write(CANDIDATE, "refresh")
            change = await asyncio.wait_for(anext(stream), 2)
            assert change.startswith("event: run\n")
            assert '"revision": 0' not in change
            rows.clear()
            write(CANDIDATE, "refresh")
            assert (await asyncio.wait_for(anext(stream), 2)).startswith("event: remove\n")
        finally:
            await stream.aclose()

    asyncio.run(check())


@pytest.mark.parametrize("suffix", ["md", "pdf"])
def test_import_node_states_follow_real_markdown_and_pdf_routes(suffix, monkeypatch):
    from importlib import import_module

    from app.rag.import_ import pipeline
    from app.shared.runtime.workflow_events import get_run
    from app.shared.runtime.workflow_monitor import IMPORT

    monkeypatch.setattr(pipeline, "_acquire_slot", lambda _task_id: True)
    monkeypatch.setattr(pipeline, "_release_slot", lambda _task_id: None)
    monkeypatch.setattr(pipeline, "save_snapshot", lambda *_args: None)
    for module, service in {
        "node_pdf_to_md": "parse_pdf_to_markdown",
        "node_md_img": "enrich_markdown_images",
        "node_document_split": "split_document",
        "node_item_name_recognition": "recognize_and_index_item_name",
        "node_bge_embedding": "generate_chunk_embeddings",
        "node_import_milvus": "index_chunks",
    }.items():
        monkeypatch.setattr(import_module(f"app.process.import_.agent.nodes.{module}"), service, lambda state: state)

    task_id = str(uuid.uuid4())
    pipeline.invoke_import_graph(task_id, f"input.{suffix}", ".")
    row = get_run(IMPORT, task_id)
    assert row["status"] == "completed"
    common = {"node_entry", "node_md_img", "node_document_split", "node_item_name_recognition",
              "node_bge_embedding", "node_import_milvus", "__start__", "__end__"}
    expected = common | ({"node_pdf_to_md"} if suffix == "pdf" else set())
    assert {key for key, value in row["nodes"].items() if value["status"] == "completed"} == expected


def test_agentic_repeated_node_is_observed_in_real_compiled_subgraph():
    from app.rag.agentic.controller import _graph
    from app.shared.runtime.workflow_events import get_run
    from app.shared.runtime.workflow_monitor import Run

    class StubRun:
        route = "bootstrap"
        trace = []
        remaining_drafts = 2

        def step(self, stage):
            if stage == "verify":
                self.remaining_drafts -= 1
                self.route = "draft" if self.remaining_drafts else "finish"
            else:
                self.route = {"bootstrap": "assess", "assess": "draft", "draft": "verify"}[stage]

    monitor = Run("test:agentic", uuid.uuid4().hex)
    monitor.begin("processing")
    with monitor.activate():
        _graph.invoke({"runtime": StubRun()})
    row = get_run(monitor.scope, monitor.id)
    assert row["nodes"]["agentic:draft"] == {"status": "completed", "visits": 2, "detail": None}
    assert row["nodes"]["agentic:verify"]["visits"] == 2
    assert row["nodes"]["agentic:__end__"]["status"] == "completed"
