"""评测用 HTTP 客户端：调用查询服务的非流式接口。

设计要点：
- 用 ``requests.Session`` 复用连接并自动保存会话 Cookie（会话归属校验需要）；
- 请求固定 ``is_stream=false`` 且 ``include_evidence=true``，一次拿到答案、引用、
  证据正文与分层 chunk_id；
- 只做有界重试（超时/连接错误/5xx），4xx 立即失败，避免把参数错误当网络抖动。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests


class EvalHTTPError(RuntimeError):
    """查询服务返回错误或响应不可用。"""


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


class QueryClient:
    """查询服务客户端。"""

    def __init__(self, base_url: str, timeout_seconds: float = 180.0, retries: int = 1):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.retries = max(0, int(retries))
        self.session = requests.Session()

    def health(self) -> dict:
        """健康检查：确认服务已启动。"""
        try:
            response = self.session.get(f"{self.base_url}/api/health", timeout=10)
        except requests.RequestException as exc:
            raise EvalHTTPError(
                f"无法连接查询服务 {self.base_url}。请先启动："
                "uv run python -m app.api.http.query_server"
            ) from exc
        if response.status_code != 200:
            raise EvalHTTPError(f"健康检查失败：HTTP {response.status_code}")
        return response.json()

    def ask(self, question: str, session_id: str | None = None) -> QueryResult:
        """提交一个问题并返回评测字段。"""
        payload = {"query": question, "is_stream": False, "include_evidence": True}
        if session_id:
            payload["session_id"] = session_id
        started = time.perf_counter()
        body = self._post_json("/api/query", payload)
        latency_ms = (time.perf_counter() - started) * 1000.0

        answer = str(body.get("answer") or "")
        evidence = body.get("evidence") or []
        contexts = [str(item.get("content") or "") for item in evidence if str(item.get("content") or "").strip()]
        return QueryResult(
            answer=answer,
            cases=body,
            citations=body.get("citations") or [],
            item_names=body.get("item_names") or [],
            contexts=contexts,
            evidence=evidence,
            retrieval_layers=body.get("retrieval_layers") or {},
            intent=str(body.get("intent") or "rag"),
            groundedness=body.get("groundedness"),
            latency_ms=round(latency_ms, 1),
        )

    def _post_json(self, path: str, payload: dict) -> dict:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                response = self.session.post(
                    f"{self.base_url}{path}", json=payload, timeout=self.timeout_seconds
                )
            except requests.RequestException as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(1.0 + attempt)
                    continue
                raise EvalHTTPError(f"请求查询服务失败（{self.base_url}）：{exc}") from exc

            if response.status_code >= 500 and attempt < self.retries:
                last_error = EvalHTTPError(f"查询服务返回 HTTP {response.status_code}")
                time.sleep(1.0 + attempt)
                continue
            if response.status_code != 200:
                raise EvalHTTPError(_error_message(response))
            try:
                return response.json()
            except ValueError as exc:
                raise EvalHTTPError("查询服务返回的不是 JSON") from exc
        raise EvalHTTPError(f"请求查询服务失败：{last_error}")

    def close(self) -> None:
        self.session.close()


def _error_message(response: requests.Response) -> str:
    """优先取统一错误体的 message 字段。"""
    try:
        body = response.json()
    except ValueError:
        body = {}
    message = str((body or {}).get("message") or "").strip()
    return message or f"查询服务返回 HTTP {response.status_code}"
