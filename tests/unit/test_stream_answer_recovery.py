from types import SimpleNamespace

import httpx
from openai import APIConnectionError, BadRequestError
import pytest

from app.rag.query import answer_service, chitchat_service
from app.shared.runtime.deadline import deadline_window
from app.shared.runtime.query_metrics import QueryMetrics, current_metrics
from app.shared.utils.sse_broker import SSEEvent


@pytest.mark.parametrize("partial", [False, True])
def test_stream_recovers_only_before_text(monkeypatch, partial):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    connection_error = APIConnectionError(request=request)
    calls, deltas = [], []

    def stream(_):
        calls.append(1)
        if len(calls) == 1:
            yield SimpleNamespace(content="")
            if partial:
                yield SimpleNamespace(content="已输出")
            raise connection_error
        yield SimpleNamespace(content="重连后完成")

    monkeypatch.setattr(answer_service.llm_providers, "chat", lambda: SimpleNamespace(stream=stream))

    def publish(_, event, data):
        assert event == SSEEvent.DELTA
        deltas.append(data["delta"])

    monkeypatch.setattr(answer_service, "publish", publish)
    state = {"session_id": "test", "is_stream": True}
    metrics = QueryMetrics("test", "turn")
    token = current_metrics.set(metrics)
    try:
        with deadline_window(10):
            if partial:
                with pytest.raises(APIConnectionError):
                    answer_service.call_llm_deal_answer(state, "prompt")
            else:
                answer_service.call_llm_deal_answer(state, "prompt")
    finally:
        current_metrics.reset(token)

    if partial:
        assert len(calls) == 1
        assert deltas == ["已输出"]
        assert "answer" not in state
        assert metrics.degraded_reasons == []
    else:
        assert len(calls) == 2
        assert state["answer"] == "重连后完成"
        assert deltas == [state["answer"]]
        assert metrics.degraded_reasons == ["llm_stream_retry"]


@pytest.mark.parametrize("retryable", [False, True])
def test_stream_connection_retries_are_bounded(monkeypatch, retryable):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    error = (httpx.RemoteProtocolError("stream ended", request=request) if retryable
             else BadRequestError("invalid", response=httpx.Response(400, request=request), body=None))
    calls = []

    def stream(_):
        calls.append(1)
        raise error

    monkeypatch.setattr(answer_service.llm_providers, "chat", lambda: SimpleNamespace(stream=stream))
    state = {"session_id": "test", "is_stream": True}
    with deadline_window(10), pytest.raises(type(error)):
        answer_service.call_llm_deal_answer(state, "prompt")

    assert len(calls) == (2 if retryable else 1)
    assert "answer" not in state


def test_chitchat_does_not_replace_a_partial_stream_with_a_fallback(monkeypatch):
    error = APIConnectionError(request=httpx.Request("POST", "https://example.test/chat"))
    deltas = []

    def stream(_):
        yield SimpleNamespace(content="已输出")
        raise error

    monkeypatch.setattr(chitchat_service.llm_providers, "chat", lambda: SimpleNamespace(stream=stream))
    monkeypatch.setattr(chitchat_service, "publish", lambda session, event, data: deltas.append(data["delta"]))
    state = {"session_id": "test", "is_stream": True}
    with deadline_window(10), pytest.raises(APIConnectionError):
        chitchat_service.call_llm_chitchat(state, "prompt")

    assert deltas == ["已输出"]
    assert "answer" not in state
