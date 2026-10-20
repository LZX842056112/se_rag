"""Collect real query results in isolated evaluation resources."""
from __future__ import annotations

import time
import uuid


def require_isolated_resources(settings) -> dict:
    """Fail before graph imports or connections when resource isolation is absent."""
    resources = {
        "mongo_database": settings.mongo.db_name,
        "chunks_collection": settings.milvus.chunks_collection,
        "item_name_collection": settings.milvus.item_name_collection,
    }
    if any(not isinstance(name, str) or not name.startswith("eval_") for name in resources.values()):
        raise ValueError("采集要求 MONGO_DB_NAME、CHUNKS_COLLECTION、ITEM_NAME_COLLECTION 均使用 eval_ 前缀的独立测试资源")
    if resources["chunks_collection"] == resources["item_name_collection"]:
        raise ValueError("测试切片集合与产品集合必须不同")
    if settings.evolution.enabled:
        raise ValueError("离线采集要求 EVOLUTION_ENABLED=false，避免评测触发知识自进化")
    return resources


def collect_answers(cases: list[dict], invoke) -> list[dict]:
    """Pass only the user's question to the normal query pipeline; never pass gold."""
    rows = []
    for case in cases:
        session = "eval-" + uuid.uuid4().hex
        started = time.perf_counter()
        row = {"case_id": case["case_id"], "query": case["query"], "session_id": session,
               "actual_intent": None, "response": "", "retrieved_contexts": [],
               "query_metrics": {}, "error": None}
        try:
            state = invoke(session_id=session, original_query=case["query"], is_stream=False)
            if state is None:
                row["error"] = "query_failed"
            else:
                row.update(actual_intent=state.get("intent"), response=state.get("answer") or "",
                           retrieved_contexts=list(state.get("answer_contexts") or []),
                           query_metrics=state.get("metrics") or {})
        except Exception as exc:
            # Exception text can contain an API key, request body, or endpoint credentials.
            row["error"] = type(exc).__name__
        row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        rows.append(row)
    return rows
