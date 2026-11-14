"""共享请求预算不依赖查询业务包，且不会跨线程或调用泄漏。"""
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys

import pytest


def test_shared_deadline_import_does_not_load_business_or_settings():
    code = (
        "import sys; import app.shared.runtime.deadline; "
        "assert not any(n.startswith(('app.rag', 'app.shared.config')) for n in sys.modules)"
    )
    assert subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0


def test_remaining_and_timeout_use_one_thread_local_budget(monkeypatch):
    from app.shared.runtime import deadline
    monkeypatch.setattr(deadline.time, "monotonic", lambda: 10.0)
    assert deadline.remaining() is None
    assert deadline.bounded_timeout(30) == 30
    token = deadline.query_deadline.set(15.0)
    try:
        assert deadline.remaining() == 5.0
        assert deadline.bounded_timeout(30) == 5.0
        assert deadline.bounded_timeout(2) == 2.0
        with ThreadPoolExecutor(max_workers=1) as pool:
            assert pool.submit(deadline.remaining).result() is None
    finally:
        deadline.query_deadline.reset(token)
    assert deadline.remaining() is None


def test_expired_deadline_never_starts_another_request(monkeypatch):
    from app.shared.runtime import deadline
    monkeypatch.setattr(deadline.time, "monotonic", lambda: 10.0)
    token = deadline.query_deadline.set(10.0)
    try:
        with pytest.raises(TimeoutError, match="deadline exceeded"):
            deadline.bounded_timeout(30)
    finally:
        deadline.query_deadline.reset(token)


@pytest.mark.parametrize("mode", ["legacy", "agentic"])
def test_query_wrapper_restores_existing_deadline_after_failure(monkeypatch, mode):
    from app.shared.config import settings
    from app.shared.runtime import deadline
    from app.rag.agentic.deadline import bounded_query
    monkeypatch.setattr(settings.agentic, "mode", mode)
    monkeypatch.setattr(settings.agentic, "gray_percent", 100)
    seen = []

    @bounded_query
    def run(session_id):
        seen.append(deadline.query_deadline.get())
        raise RuntimeError("failed")

    token = deadline.query_deadline.set(123.0)
    try:
        with pytest.raises(RuntimeError, match="failed"):
            run("test-session")
        assert deadline.query_deadline.get() == 123.0
        assert (seen[0] == 123.0) is (mode == "legacy")
    finally:
        deadline.query_deadline.reset(token)


@pytest.mark.parametrize("outer_seconds", [None, 17.0])
def test_legacy_query_dispatch_uses_bounded_llm_client(monkeypatch, outer_seconds):
    import time
    from types import SimpleNamespace

    from app.rag.query import pipeline
    from app.rag.query.lifecycle import query_is_active
    from app.shared.config import settings
    from app.shared.models import providers
    from app.shared.runtime.deadline import query_deadline

    monkeypatch.setattr(settings.agentic, "mode", "legacy")
    monkeypatch.setattr(settings.evolution, "write_enabled", False)
    monkeypatch.setattr(pipeline, "record_query", lambda *args, **kwargs: None)
    seen = {}

    def get_client(**options):
        seen.update(options)
        return SimpleNamespace()

    def invoke(state):
        providers.llm_providers.chat()
        return {**state, "answer": "本地资料中的答案"}

    monkeypatch.setattr(providers, "get_llm_client", get_client)
    monkeypatch.setattr(pipeline, "query_app", SimpleNamespace(invoke=invoke))
    outer_deadline = time.monotonic() + outer_seconds if outer_seconds is not None else None
    token = query_deadline.set(outer_deadline)
    try:
        result = pipeline.invoke_query_graph("test-legacy-deadline", "HAK180 接线方式", False)
        assert result["answer"] == "本地资料中的答案"
        assert 0 < seen.get("timeout_seconds", 0) <= (outer_seconds or 45.0) + 1e-9
        assert seen["max_retries"] == 0
        assert query_deadline.get() == outer_deadline
        assert not query_is_active("test-legacy-deadline")
    finally:
        query_deadline.reset(token)


def test_legacy_query_preserves_mongo_server_selection_timeout(monkeypatch):
    from pymongo import MongoClient

    from app.rag.agentic.deadline import bounded_query
    from app.shared.config import settings

    monkeypatch.setattr(settings.agentic, "mode", "legacy")
    with MongoClient(connect=False, serverSelectionTimeoutMS=250) as client:
        @bounded_query
        def run(session_id):
            return client._topology.get_server_selection_timeout()

        assert run("test-legacy-mongo-timeout") == 0.25


def test_hyde_leaves_reserved_time_for_answer_generation(monkeypatch):
    from app.process.query.agent.nodes.node_search_embedding_hyde import node_search_embedding_hyde
    from app.rag.query import hyde_search_service as hyde
    from app.shared.runtime import deadline

    monkeypatch.setattr(deadline.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(hyde, "HYDE_GATE_ENABLED", False)
    monkeypatch.setattr(hyde, "search_chunks", lambda *a, **kw: [{"entity": {"chunk_id": "c1"}}])
    monkeypatch.setattr(hyde, "call_llm_answer", lambda question: "假设性答案")
    token = deadline.query_deadline.set(112.0)
    try:
        assert node_search_embedding_hyde({"rewritten_query": "q", "item_names": ["A"]}) == {
            "hyde_embedding_chunks": [],
        }
        assert deadline.query_deadline.get() == 112.0
    finally:
        deadline.query_deadline.reset(token)
