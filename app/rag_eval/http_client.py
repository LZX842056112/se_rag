"""评测用 HTTP 客户端：调用查询服务的非流式接口。

设计要点：
- 每个请求线程使用独立的 ``requests.Session``，复用连接与 Cookie；
- 请求固定 ``is_stream=false`` 且 ``include_evidence=true``，一次拿到答案、引用、
  证据正文与分层 chunk_id；
- 只做有界重试（超时/连接错误/5xx），4xx 立即失败，避免把参数错误当网络抖动。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock, local
from typing import Any

import requests

from app.rag_eval.dataset import OUTCOMES

QUERY_MODES = ("legacy", "agentic", "none")


class EvalHTTPError(RuntimeError):
    """查询服务返回错误或响应不可用。"""

    def __init__(self, message: str = "", *, http_attempts: int = 1, http_failure_reasons: tuple[str, ...] = ()):
        super().__init__(message)
        self.http_attempts = http_attempts
        self.http_failure_reasons = http_failure_reasons


@dataclass
class QueryResult:
    """单题查询结果（评测所需的字段）。"""

    answer: str
    cases: dict[str, Any] = field(default_factory=dict)
    citations: list[dict] = field(default_factory=list)
    item_names: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    retrieval_layers: dict[str, list[str]] = field(default_factory=dict)
    intent: str = "rag"
    groundedness: float | None = None
    latency_ms: float = 0.0
    query_mode: str | None = None
    outcome: str | None = None
    evaluation_schema_version: int | None = None
    server_metrics: dict = field(default_factory=dict)
    http_attempts: int = 1
    http_failure_reasons: tuple[str, ...] = ()


class QueryClient:
    """查询服务客户端。"""

    def __init__(self, base_url: str, timeout_seconds: float = 180.0, retries: int = 1):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retries = max(0, int(retries))
        self._local = local()
        self._sessions: list[requests.Session] = []
        self._lock = Lock()

    @property
    def session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            self._local.session = session
            with self._lock:
                self._sessions.append(session)
        return session

    def health(self) -> dict:
        """健康检查：确认服务已启动。"""
        try:
            response = self.session.get(
                f"{self.base_url}/api/health", timeout=10, params={"include_evidence": "true"},
            )
        except requests.RequestException as exc:
            raise EvalHTTPError(
                f"无法连接查询服务 {self.base_url}。请先启动："
                "uv run python -m app.api.http.query_server"
            ) from exc
        if response.status_code != 200:
            raise EvalHTTPError(f"健康检查失败：HTTP {response.status_code}")
        return _response_object(response)

    def ask(self, question: str, session_id: str | None = None) -> QueryResult:
        """提交一个问题并返回评测字段。"""
        payload = {"query": question, "is_stream": False, "include_evidence": True}
        if session_id:
            payload["session_id"] = session_id
        started = time.perf_counter()
        body, attempts, failure_reasons = self._post_json("/api/query", payload)
        latency_ms = (time.perf_counter() - started) * 1000.0

        answer = str(body.get("answer") or "")
        evaluation = _object(body.get("evaluation"))
        version = evaluation.get("schema_version", 1) if evaluation else None
        mode = evaluation.get("query_mode")
        outcome = evaluation.get("outcome")
        layers = {
            name: ids for name, ids in _object(body.get("retrieval_layers")).items()
            if isinstance(ids, list) and all(isinstance(item, str) and item.strip() for item in ids)
        }
        return QueryResult(
            answer=answer,
            cases=body,
            citations=body.get("citations") or [],
            item_names=_strings(body.get("item_names")),
            contexts=_strings(evaluation.get("contexts")),
            evidence=body.get("evidence") or [],
            retrieval_layers=layers,
            intent=str(body.get("intent") or "rag"),
            groundedness=body.get("groundedness"),
            latency_ms=round(latency_ms, 1),
            query_mode=mode if mode in QUERY_MODES else None,
            outcome=outcome if outcome in OUTCOMES else None,
            evaluation_schema_version=version if type(version) is int and version > 0 else None,
            server_metrics=_object(body.get("metrics")),
            http_attempts=attempts,
            http_failure_reasons=failure_reasons,
        )

    def _post_json(self, path: str, payload: dict) -> tuple[dict, int, tuple[str, ...]]:
        failures: list[str] = []
        max_attempts = self.retries + 1
        for attempt in range(1, max_attempts + 1):
            try:
                response = self.session.post(
                    f"{self.base_url}{path}", json=payload, timeout=self.timeout_seconds
                )
            except requests.RequestException as exc:
                if isinstance(exc, requests.Timeout):
                    failures.append("timeout")
                elif isinstance(exc, requests.ConnectionError):
                    failures.append("connection_error")
                else:
                    failures.append("request_error")
                if attempt == max_attempts:
                    raise EvalHTTPError(
                        f"请求查询服务失败（{self.base_url}）：{exc}",
                        http_attempts=attempt, http_failure_reasons=tuple(failures),
                    ) from exc
            else:
                if response.status_code == 200:
                    try:
                        return _response_object(response), attempt, tuple(failures)
                    except EvalHTTPError as exc:
                        failures.append("invalid_response")
                        raise EvalHTTPError(
                            str(exc), http_attempts=attempt, http_failure_reasons=tuple(failures),
                        ) from exc
                failures.append(f"http_{response.status_code}")
                if response.status_code < 500 or attempt == max_attempts:
                    raise EvalHTTPError(
                        _error_message(response), http_attempts=attempt, http_failure_reasons=tuple(failures),
                    )
            time.sleep(attempt)
        raise AssertionError("HTTP 重试循环没有返回结果")

    def close(self) -> None:
        for session in self._sessions:
            session.close()
        self._sessions.clear()
        self._local = local()


def _object(value) -> dict:
    return value if isinstance(value, dict) else {}


def _strings(value) -> list[str]:
    return [text for text in value if isinstance(text, str) and text.strip()] if isinstance(value, list) else []


def _response_object(response: requests.Response) -> dict:
    try:
        body = response.json()
    except ValueError as exc:
        raise EvalHTTPError("查询服务返回的不是 JSON") from exc
    if not isinstance(body, dict):
        raise EvalHTTPError("查询服务返回的 JSON 必须是对象")
    return body


def _error_message(response: requests.Response) -> str:
    """优先取统一错误体的 message 字段。"""
    try:
        body = response.json()
    except ValueError:
        body = {}
    message = str(_object(body).get("message") or "").strip()
    return message or f"查询服务返回 HTTP {response.status_code}"
