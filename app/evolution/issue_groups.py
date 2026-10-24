"""把同一上下文的登记记录组成一个待办，保留每次登记及处理历史。"""
from __future__ import annotations

import re
import unicodedata

from bson import ObjectId

from app.rag.query.business import classify_business
from app.shared.config import settings

ISSUE_SCAN_LIMIT = 10000
ACTIVE_STATUSES = ["open", "in_progress"]


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", str(value or ""))).casefold()


def with_context(rows: list[dict], get_collection) -> list[dict]:
    """旧登记缺少产品时，批量读取关联回答或缺口。同会话校验不能省略。"""
    sources = {}
    for field, collection_name in (("answer_id", settings.mongo.chat_message_collection),
                                   ("gap_id", settings.mongo.k_gaps_collection)):
        ids = {ObjectId(row[field]) for row in rows if row.get("item_names") is None
               and ObjectId.is_valid(row.get(field, ""))}
        if not ids:
            continue
        query = {"_id": {"$in": list(ids)}}
        if field == "answer_id":
            query["role"] = "assistant"
        docs = get_collection(collection_name).find(query, {"session_id": 1, "item_names": 1})
        sources[field] = {(str(doc["_id"]), doc.get("session_id")): doc.get("item_names") or [] for doc in docs}
    enriched = []
    for row in rows:
        names = row.get("item_names")
        if names is None:
            names = next((sources[field][(row.get(field), row.get("session_id"))]
                          for field in sources if (row.get(field), row.get("session_id")) in sources[field]), [])
        enriched.append({**row, "item_names": names,
                         "business_tag": row.get("business_tag") or classify_business(row.get("query") or "")})
    return enriched


def context_key(row: dict) -> tuple | None:
    """只为会话、问题和产品均明确的记录生成跨登记上下文键。"""
    text = row.get("original_query") or row.get("query") or ""
    query = " ".join(unicodedata.normalize("NFKC", str(text)).casefold().split()).rstrip("?!。").rstrip()
    products = tuple(sorted({_normalize(name) for name in row.get("item_names") or [] if _normalize(name)}))
    if not row.get("session_id") or not query or not products:
        return None
    return row["session_id"], query, products, row.get("business_tag") or classify_business(str(text))


def group_key(row: dict) -> tuple:
    query = " ".join(unicodedata.normalize("NFKC", str(row.get("query") or "")).casefold().split()).rstrip("?!。").rstrip()
    # 无法确认会话或问题的旧记录各自保留，避免错误归并。
    session = row.get("session_id") or str(row["_id"])
    query = query or str(row["_id"])
    products = tuple(sorted({_normalize(name) for name in row.get("item_names") or [] if _normalize(name)}))
    if not products:
        products = ("unknown-product:" + str(row["_id"]),)
    phase = ("active",)
    if row.get("status") == "resolved":
        phase = ("resolved", row.get("completed_at") or str(row["_id"]))
    elif row.get("status") == "dismissed":
        phase = ("dismissed", row.get("dismissed_by_gap_id") or str(row["_id"]))
    return (*(context_key(row) or (session, query, products, row.get("business_tag"))), phase)


def group_rows(rows: list[dict]) -> list[list[dict]]:
    groups = {}
    for row in rows:
        groups.setdefault(group_key(row), []).append(row)
    return list(groups.values())


def public_group(rows: list[dict]) -> dict:
    representative = min(rows, key=lambda row: (row.get("status") != "in_progress", not bool(row.get("owner")),
                                                row.get("created_at") or 0, str(row["_id"])))
    summary = {key: value for key, value in representative.items() if key != "_id"}
    fields = ("issue_type", "status", "owner", "resolution", "created_at", "completed_at", "handling_history")
    return {**summary, "id": str(representative["_id"]), "record_count": len(rows),
            "issue_types": sorted({row.get("issue_type") or "missing_material" for row in rows}),
            "created_at": min(row.get("created_at") or 0 for row in rows),
            "last_seen_at": max(row.get("created_at") or 0 for row in rows),
            "related_records": [{"id": str(row["_id"]), **{key: row.get(key) for key in fields}} for row in rows]}
