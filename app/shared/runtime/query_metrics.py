"""按查询记录模型调用、耗时和已配置的估算费用。未知费用保持空值。"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import json
import math
import threading
import time
from typing import Any

from app.shared.config import settings
from app.shared.runtime.logger import logger


@dataclass
class QueryMetrics:
    session_id: str
    turn_id: str
    started: float = field(default_factory=time.perf_counter)
    first_token_ms: float | None = None
    classification_ms: float = 0.0
    llm_calls: int = 0
    completed_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost: float = 0.0
    cost_known: bool = True
    degraded_reasons: list[str] = field(default_factory=list)
    lock: Any = field(default_factory=threading.RLock, repr=False)

    def first_text(self):
        with self.lock:
            if self.first_token_ms is None:
                self.first_token_ms = (time.perf_counter() - self.started) * 1000

    def document(self, intent: str, status: str) -> dict:
        with self.lock:
            return {
                "session_id": self.session_id, "turn_id": self.turn_id,
                "intent": intent, "status": status, "ts": time.time(),
                "duration_ms": (time.perf_counter() - self.started) * 1000,
                "first_token_ms": self.first_token_ms,
                "classification_ms": self.classification_ms,
                "llm_calls": self.llm_calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "cost_usd": self.cost if self.cost_known and self.llm_calls == self.completed_calls else None,
                "degraded": bool(self.degraded_reasons),
                "degraded_reasons": list(self.degraded_reasons),
            }


current_metrics: ContextVar[QueryMetrics | None] = ContextVar("query_metrics", default=None)


def mark_degraded(reason: str) -> None:
    metrics = current_metrics.get()
    if metrics:
        with metrics.lock:
            if reason not in metrics.degraded_reasons:
                metrics.degraded_reasons.append(reason)


def measure_client(client, metrics: QueryMetrics, model: str, prices: dict):
    """包装本轮客户端，缓存客户端本身保持不变。"""
    class MeasuredClient:
        def __getattr__(self, name):
            return getattr(client, name)

        def _start(self):
            with metrics.lock:
                metrics.llm_calls += 1

        def _finish(self, usage):
            usage = usage or {}
            with metrics.lock:
                metrics.completed_calls += 1
                inp, out = usage.get("input_tokens"), usage.get("output_tokens")
                if inp is None or out is None:
                    metrics.cost_known = False
                    return
                metrics.input_tokens += inp
                metrics.output_tokens += out
                price = prices.get(model)
                if not isinstance(price, dict) or not all(type(price.get(k)) in (int, float) and math.isfinite(price[k]) and price[k] >= 0 for k in ("input", "output")):
                    metrics.cost_known = False
                    return
                metrics.cost += (inp * price["input"] + out * price["output"]) / 1_000_000

        def invoke(self, *args, **kwargs):
            self._start()
            usage = None
            try:
                result = client.invoke(*args, **kwargs)
                usage = getattr(result, "usage_metadata", None)
                return result
            finally:
                self._finish(usage)

        def stream(self, *args, **kwargs):
            self._start()
            usage = None
            try:
                for chunk in client.stream(*args, **kwargs):
                    usage = getattr(chunk, "usage_metadata", None) or usage
                    yield chunk
            finally:
                self._finish(usage)

    return MeasuredClient()


def instrument_client(client, model: str | None = None):
    metrics = current_metrics.get()
    if metrics is None:
        return client
    try:
        prices = json.loads(settings.runtime.model_prices_json)
        if not isinstance(prices, dict):
            prices = {}
    except (ValueError, TypeError):
        prices = {}
    model = model or getattr(client, "model_name", None) or settings.llm.llm_model
    return measure_client(client, metrics, model, prices)


def record_query(metrics: QueryMetrics, state: dict, status: str, query: str):
    from app.shared.clients.mongo import get_collection
    doc = metrics.document(state.get("intent", "rag"), status)
    doc.update({
        "answer_id": state.get("answer_id") or "",
        "business_tag": state.get("business_tag", ""),
        "handoff": bool(state.get("handoff_required")),
        "query_hash": hashlib.sha256("".join(query.lower().split()).encode()).hexdigest(),
        "query_mode": state.get('query_mode', 'legacy'),
        "agentic_stop_reason": state.get('agentic_stop_reason', ''),
        "agentic_metrics": state.get('agentic_metrics', {}),
    })
    try:
        get_collection(settings.mongo.query_runs_collection).update_one(
            {"turn_id": metrics.turn_id}, {"$setOnInsert": doc}, upsert=True,
        )
    except Exception as exc:
        logger.warning(f"保存查询指标失败：{exc}")
    return doc
