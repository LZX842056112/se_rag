"""HTTP 客户端：请求字段、证据解析与错误处理。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import requests

from app.rag_eval.http_client import EvalHTTPError, QueryClient


class _Response:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class _Session:
    def __init__(self, response: _Response):
        self.response = response
        self.posts: list[tuple[str, dict, float]] = []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json, timeout))
        return self.response

    def get(self, url, timeout=None, params=None):
        return self.response

    def close(self):
        return None


def _client(response: _Response, monkeypatch) -> tuple[QueryClient, _Session]:
    session = _Session(response)
    monkeypatch.setattr(requests, "Session", lambda: session)
    return QueryClient("http://127.0.0.1:8001"), session


def test_ask_sends_evidence_flag_and_parses_contexts(monkeypatch):
    response = _Response(payload={
        "answer": "答案",
        "evidence": [{"chunk_id": "c1", "content": "正文一"}, {"chunk_id": "c2", "content": " "}],
        "retrieval_layers": {"reranked": ["c1"]},
        "evaluation": {"query_mode": "legacy", "outcome": "answered", "contexts": ["已授权父块", "网页"]},
        "citations": [{"faq_id": "c1"}],
        "item_names": ["主体"],
        "groundedness": 0.9,
    })
    client, session = _client(response, monkeypatch)

    result = client.ask("怎么设置")

    url, payload, timeout = session.posts[0]
    assert url.endswith("/api/query")
    assert payload["is_stream"] is False
    assert payload["include_evidence"] is True
    assert timeout == client.timeout_seconds
    assert result.answer == "答案"
    assert result.contexts == ["已授权父块", "网页"]
    assert result.retrieval_layers == {"reranked": ["c1"]}
    assert result.query_mode == "legacy"
    assert result.outcome == "answered"


def test_ask_leaves_contexts_empty_without_evaluation_evidence(monkeypatch):
    client, _ = _client(_Response(payload={
        "answer": "答案",
        "evidence": [{"chunk_id": "c1", "content": "原始切片"}],
    }), monkeypatch)

    assert client.ask("q").contexts == []


def test_health_explains_how_to_start_service(monkeypatch):
    """服务未启动时给出可执行的启动命令，而不是原始连接异常。"""
    class _Down(_Session):
        def get(self, url, timeout=None, params=None):
            raise requests.ConnectionError("refused")

    client = QueryClient("http://127.0.0.1:8001")
    monkeypatch.setattr(requests, "Session", lambda: _Down(_Response()))
    with pytest.raises(EvalHTTPError, match="query_server"):
        client.health()


def test_ask_raises_readable_error_for_non_200(monkeypatch):
    client, _ = _client(_Response(422, {"message": "参数校验失败"}), monkeypatch)
    with pytest.raises(EvalHTTPError, match="参数校验失败") as failure:
        client.ask("q")
    assert failure.value.http_attempts == 1
    assert failure.value.http_failure_reasons == ("http_422",)


def test_ask_rejects_non_json_response(monkeypatch):
    class _Broken(_Session):
        def post(self, url, json=None, timeout=None):
            return type("R", (), {"status_code": 200, "json": lambda self: (_ for _ in ()).throw(ValueError())})()

    client = QueryClient("http://127.0.0.1:8001")
    monkeypatch.setattr(requests, "Session", lambda: _Broken(_Response()))
    with pytest.raises(EvalHTTPError, match="不是 JSON") as failure:
        client.ask("q")
    assert failure.value.http_attempts == 1
    assert failure.value.http_failure_reasons == ("invalid_response",)


def test_ask_records_failed_attempt_before_success(monkeypatch):
    client, session = _client(_Response(payload={"answer": "a"}), monkeypatch)
    responses = iter([_Response(503), session.response])
    monkeypatch.setattr(session, "post", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr("app.rag_eval.http_client.time.sleep", lambda _seconds: None)

    result = client.ask("q")

    assert result.answer == "a"
    assert result.http_attempts == 2
    assert result.http_failure_reasons == ("http_503",)


def test_ask_records_attempts_when_retries_are_exhausted(monkeypatch):
    client, session = _client(_Response(), monkeypatch)
    errors = iter([requests.ConnectionError("reset"), requests.Timeout("timeout")])

    def fail(*args, **kwargs):
        raise next(errors)

    monkeypatch.setattr(session, "post", fail)
    monkeypatch.setattr("app.rag_eval.http_client.time.sleep", lambda _seconds: None)

    with pytest.raises(EvalHTTPError, match="timeout") as failure:
        client.ask("q")

    assert failure.value.http_attempts == 2
    assert failure.value.http_failure_reasons == ("connection_error", "timeout")


def test_concurrent_requests_do_not_share_session_state(monkeypatch):
    pending = Barrier(2)

    class Session(_Session):
        def post(self, url, json=None, timeout=None):
            self.cookie = json["query"]
            pending.wait(timeout=2)
            return _Response(payload={"answer": self.cookie})

    monkeypatch.setattr(requests, "Session", lambda: Session(_Response()))
    client = QueryClient("http://127.0.0.1:8001")
    try:
        with ThreadPoolExecutor(2) as pool:
            assert [result.answer for result in pool.map(client.ask, ["a", "b"])] == ["a", "b"]
    finally:
        client.close()
