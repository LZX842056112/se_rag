"""HTTP 客户端：请求字段、证据解析与错误处理。"""
from __future__ import annotations

import pytest

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

    def get(self, url, timeout=None):
        return self.response

    def close(self):
        return None


def _client(response: _Response) -> tuple[QueryClient, _Session]:
    client = QueryClient("http://127.0.0.1:8001")
    session = _Session(response)
    client.session = session
    return client, session


def test_ask_sends_evidence_flag_and_parses_contexts():
    response = _Response(payload={
        "answer": "答案",
        "evidence": [{"chunk_id": "c1", "content": "正文一"}, {"chunk_id": "c2", "content": " "}],
        "retrieval_layers": {"reranked": ["c1"]},
        "citations": [{"faq_id": "c1"}],
        "item_names": ["主体"],
        "groundedness": 0.9,
    })
    client, session = _client(response)

    result = client.ask("怎么设置")

    url, payload, timeout = session.posts[0]
    assert url.endswith("/api/query")
    assert payload["is_stream"] is False
    assert payload["include_evidence"] is True
    assert timeout == client.timeout_seconds
    assert result.answer == "答案"
    assert result.contexts == ["正文一"]
    assert result.retrieval_layers == {"reranked": ["c1"]}


def test_health_explains_how_to_start_service():
    """服务未启动时给出可执行的启动命令，而不是原始连接异常。"""
    import requests

    class _Down(_Session):
        def get(self, url, timeout=None):
            raise requests.ConnectionError("refused")

    client = QueryClient("http://127.0.0.1:8001")
    client.session = _Down(_Response())
    with pytest.raises(EvalHTTPError, match="query_server"):
        client.health()


def test_ask_raises_readable_error_for_non_200():
    client, _ = _client(_Response(422, {"message": "参数校验失败"}))
    with pytest.raises(EvalHTTPError, match="参数校验失败"):
        client.ask("q")


def test_ask_rejects_non_json_response():
    class _Broken(_Session):
        def post(self, url, json=None, timeout=None):
            return type("R", (), {"status_code": 200, "json": lambda self: (_ for _ in ()).throw(ValueError())})()

    client = QueryClient("http://127.0.0.1:8001")
    client.session = _Broken(_Response())
    with pytest.raises(EvalHTTPError, match="不是 JSON"):
        client.ask("q")
