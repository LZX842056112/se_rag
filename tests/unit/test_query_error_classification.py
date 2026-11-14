"""查询链路错误分类回归：外部依赖故障要映射成稳定机器码，便于运维排障。

背景：历史实现把所有异常统一为 ``query_failed``，无法区分「Milvus 挂了」
「LLM 超时」「重排 OOM」「Mongo 挂」。分类码经 SSE error 事件与任务状态暴露，
HTTP 契约不变（非流式仍返回 ``query_failed`` / 500）。
"""
from __future__ import annotations

import grpc
import httpx
from openai import APIConnectionError
import pytest
from pymongo.errors import ServerSelectionTimeoutError

from app.rag.query import pipeline
from app.rag.query.errors import DEFAULT_CODE, classify_query_error
from app.rag.query.lifecycle import query_is_active
from app.shared.utils import task_state
from app.shared.utils.sse_broker import SSEEvent


def test_grpc_error_maps_to_milvus_unavailable():
    """gRPC 层异常（Milvus 传输故障）归为向量库不可用。"""
    code, _ = classify_query_error(grpc.RpcError("Milvus 连接失败"))
    assert code == "milvus_unavailable"


def test_pymongo_error_maps_to_mongo_unavailable():
    """pymongo 异常归为 Mongo 不可用。"""
    code, _ = classify_query_error(ServerSelectionTimeoutError("mongo down"))
    assert code == "mongo_unavailable"


def test_timeout_error_maps_to_llm_timeout():
    """超时归为 LLM 超时（查询侧主要的长等待点是模型客户端）。"""
    code, _ = classify_query_error(TimeoutError("read timeout"))
    assert code == "llm_timeout"


def test_keyword_fallback_classifies_plain_exception():
    """类型无法判定时按消息关键字兜底（网关包装后常退化为普通异常）。"""
    code, _ = classify_query_error(RuntimeError("Milvus 混合检索执行失败"))
    assert code == "milvus_unavailable"


def test_unclassifiable_error_keeps_default_code():
    """无法归类时保持现有口径，不引入新契约。"""
    code, message = classify_query_error(RuntimeError("完全未知的故障"))
    assert code == DEFAULT_CODE
    assert "完全未知的故障" in message


@pytest.mark.parametrize(("error", "code", "is_stream"), [
    (RuntimeError("Milvus 混合检索执行失败"), "milvus_unavailable", False),
    (TimeoutError("query deadline exceeded"), "llm_timeout", True),
])
def test_pipeline_exposes_classified_code_and_ends_failed_turn(monkeypatch, error, code, is_stream):
    """图执行失败后，发布分类错误，结束 SSE，释放会话锁。"""
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        pipeline, "publish", lambda sid, ev, payload: events.append((ev, payload))
    )

    def _boom(_state):
        raise error

    monkeypatch.setattr(pipeline.query_app, "invoke", _boom)

    result = pipeline.invoke_query_graph("s_err", "q", is_stream=is_stream)

    assert result is None
    assert task_state.get_task_error("s_err")[0] == code
    assert task_state.get_task_status("s_err") == task_state.TASK_STATUS_FAILED
    assert not query_is_active("s_err")
    assert [event for event, _ in events] == [SSEEvent.ERROR, SSEEvent.CLOSE]
    assert events[0][1]["code"] == code


def test_pipeline_persists_model_failure_diagnostics(monkeypatch):
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    error = APIConnectionError(request=request)
    error.__cause__ = httpx.RemoteProtocolError("server closed the response", request=request)

    def fail(_):
        raise error

    monkeypatch.setattr(pipeline.query_app, "invoke", fail)
    events = []
    monkeypatch.setattr(pipeline, "publish", lambda sid, event, payload: events.append(event))
    saved = []
    monkeypatch.setattr(pipeline, "record_query", lambda *args, **kwargs: saved.append(kwargs))

    assert pipeline.invoke_query_graph("s_model_err", "q", is_stream=True) is None
    assert saved[0]["error"] == {
        "code": "llm_failed",
        "type": "APIConnectionError",
        "cause_type": "RemoteProtocolError",
    }
    assert events == [SSEEvent.ERROR, SSEEvent.CLOSE]
