"""会话归属与单进程部署回归。"""
from types import SimpleNamespace
import mongomock
import pytest
from app.api.errors import ApiError
from app.api import session_access
from app.shared.config import settings
from app.shared.runtime.single_worker import SingleWorkerLease

@pytest.fixture
def database(monkeypatch):
    db = mongomock.MongoClient().trial
    monkeypatch.setattr(session_access, "get_collection", lambda name: db[name])
    monkeypatch.setattr(settings.api, "session_ownership_enabled", True)
    return db

def request(owner):
    return SimpleNamespace(state=SimpleNamespace(owner_token=owner))

def test_owner_binding_survives_requests_and_blocks_other_browser(database):
    session_access.authorize_session(request("a" * 64), "s", create=True)
    session_access.authorize_session(request("a" * 64), "s")
    with pytest.raises(ApiError) as exc:
        session_access.authorize_session(request("b" * 64), "s")
    assert exc.value.status_code == 404

def test_legacy_session_cannot_be_claimed(database):
    database[settings.mongo.chat_message_collection].insert_one({"session_id":"legacy", "text":"private"})
    with pytest.raises(ApiError):
        session_access.authorize_session(request("a" * 64), "legacy", create=True)

def test_unknown_session_read_does_not_claim_it(database):
    session_access.authorize_session(request("a" * 64), "new")
    assert database[settings.mongo.query_sessions_collection].count_documents({}) == 0

def test_second_worker_is_rejected(tmp_path):
    first=SingleWorkerLease(tmp_path / "worker.lock")
    second=SingleWorkerLease(tmp_path / "worker.lock")
    first.acquire()
    try:
        with pytest.raises(RuntimeError):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()
