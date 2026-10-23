"""在隔离的评估资源中采集真实查询结果。"""
from __future__ import annotations

import time
import uuid


def require_isolated_resources(settings) -> dict:
    """在没有资源隔离时，先在导入图或建立连接之前失败。"""
    resources = {
        "mongo_database": settings.mongo.db_name,
        "chunks_collection": settings.milvus.chunks_collection,
        "item_name_collection": settings.milvus.item_name_collection,
    }
    if any(not isinstance(name, str) or not name.startswith("eval_") for name in resources.values()):
        raise ValueError("采集要求 MONGO_DB_NAME、CHUNKS_COLLECTION、ITEM_NAME_COLLECTION 均使用 eval_ 前缀的独立测试资源")
    if resources["chunks_collection"] == resources["item_name_collection"]:
        raise ValueError("测试切片集合与产品集合必须不同")
    evolution = settings.evolution
    writes = getattr(evolution, "write_enabled", None)
    recall = getattr(evolution, "recall_enabled", None)
    writes = writes if writes is not None else evolution.enabled
    recall = recall if recall is not None else evolution.enabled
    if writes:
        raise ValueError("离线采集要求关闭知识写入：EVOLUTION_ENABLED=false 或 EVOLUTION_WRITE_ENABLED=false")
    if (evolution.enabled or recall) and (getattr(evolution, "scheduler_enabled", False)
                                         or getattr(evolution, "auto_tune_enabled", False)):
        raise ValueError("离线采集要求关闭进化调度和自动调参")
    if recall:
        name = getattr(evolution, "collection", "")
        if not isinstance(name, str) or not name.startswith("eval_") or name in resources.values():
            raise ValueError("已审批 FAQ 必须使用独立的 eval_ 集合")
        resources["evolution_collection"] = name
    return resources


def collect_answers(cases: list[dict], invoke) -> list[dict]:
    """只把用户的问题传给正常的查询管道；绝不传入参考答案。"""
    rows = []
    for case in cases:
        session = "eval-" + uuid.uuid4().hex
        started = time.perf_counter()
        row = {"case_id": case["case_id"], "query": case["query"], "session_id": session,
               "actual_intent": None, "response": "", "retrieved_contexts": [],
               "query_metrics": {}, "agentic_trace": [], "evidence_manifest": [],
               "agentic_metrics": {}, "agentic_stop_reason": None, "error": None}
        try:
            state = invoke(session_id=session, original_query=case["query"], is_stream=False)
            if state is None:
                row["error"] = "query_failed"
            else:
                row.update(actual_intent=state.get("intent"), response=state.get("answer") or "",
                           retrieved_contexts=list(state.get("answer_contexts") or []),
                           query_metrics=state.get("metrics") or {},
                           agentic_trace=list(state.get("agentic_trace") or []),
                           evidence_manifest=list(state.get("evidence_manifest") or []),
                           agentic_metrics=state.get("agentic_metrics") or {},
                           agentic_stop_reason=state.get("agentic_stop_reason"))
        except Exception as exc:
            # 异常文本可能包含 API 密钥、请求体或端点凭据。
            row["error"] = type(exc).__name__
        row["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        rows.append(row)
    return rows