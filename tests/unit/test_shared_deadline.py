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
