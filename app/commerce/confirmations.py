"""Share pending confirmations and submission outcomes between query workers."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

from pymongo import ReturnDocument

from app.api.errors import ApiError
from app.commerce.models import ActionResult, ConfirmationRequest, OrderDetail
from app.shared.clients.mongo import get_collection


_COLLECTION = "commerce_confirmations"
_APPROVAL_PERIOD = timedelta(minutes=5)
_RETRY_PERIOD = timedelta(hours=24)
_SUBMISSION_LEASE = timedelta(seconds=30)
_TERMINAL_STATES = {"succeeded", "failed", "cancelled"}


def _now() -> datetime:
    return datetime.now(UTC)


def _mongo_now() -> datetime:
    now = _now().replace(tzinfo=None)
    return now.replace(microsecond=now.microsecond // 1000 * 1000)


def _owned(user_id: str, confirmation_id: str) -> dict:
    return {"_id": confirmation_id, "owner": sha256(user_id.encode()).hexdigest()}


def create(user_id: str, body: ConfirmationRequest, order: OrderDetail) -> dict:
    collection = get_collection(_COLLECTION)
    collection.create_index("delete_at", expireAfterSeconds=0)
    now = _mongo_now()
    doc = {
        **_owned(user_id, str(uuid4())),
        "operation": body.operation,
        "order_id": body.order_id,
        "preview": order.model_dump(mode="json"),
        "payload": body.action_payload(),
        "status": "pending",
        "expires_at": now + _APPROVAL_PERIOD,
        "delete_at": now + _RETRY_PERIOD,
    }
    collection.insert_one(doc)
    return doc


def get(user_id: str, confirmation_id: str) -> dict:
    doc = get_collection(_COLLECTION).find_one(_owned(user_id, confirmation_id))
    if doc is None:
        raise ApiError("commerce_not_found", "找不到该商城确认请求", 404)
    return doc


def _state(doc: dict, now: datetime) -> str:
    status = doc["status"]
    if status in _TERMINAL_STATES:
        return status
    if status == "pending":
        return status if doc["expires_at"] > now else "expired"
    if doc["retry_until"] <= now:
        return "expired"
    if status == "submitting" and doc["lease_until"] <= now:
        return "unknown"
    return status


def state(doc: dict) -> str:
    return _state(doc, _mongo_now())


def _busy() -> ApiError:
    return ApiError("confirmation_in_progress", "正在提交此请求，请稍后查看确认状态", 409)


def cancel(user_id: str, confirmation_id: str) -> dict:
    now = _mongo_now()
    doc = get_collection(_COLLECTION).find_one_and_update(
        {**_owned(user_id, confirmation_id), "status": "pending", "expires_at": {"$gt": now}},
        {"$set": {"status": "cancelled"}},
        return_document=ReturnDocument.AFTER,
    )
    if doc is not None:
        return doc

    doc = get(user_id, confirmation_id)
    current = _state(doc, now)
    if current == "cancelled":
        return doc
    if current == "expired":
        raise ApiError("confirmation_expired", "确认请求已过期", 410)
    if current == "submitting":
        raise _busy()
    if current == "unknown":
        raise ApiError("confirmation_not_cancellable", "提交结果暂不明确。请查看或重试该请求。", 409)
    raise ApiError("confirmation_not_cancellable", "请求已提交。请查看确认状态。", 409)


def claim(user_id: str, confirmation_id: str) -> tuple[dict, str | None]:
    doc = get(user_id, confirmation_id)
    now = _mongo_now()
    state = _state(doc, now)
    if state in _TERMINAL_STATES:
        return doc, None
    if state == "expired":
        raise ApiError("confirmation_expired", "确认请求已过期", 410)
    if state == "submitting":
        raise _busy()

    lease = str(uuid4())
    match = {**_owned(user_id, confirmation_id), "status": doc["status"]}
    updates = {"status": "submitting", "lease": lease, "lease_until": now + _SUBMISSION_LEASE}
    if state == "pending":
        match["expires_at"] = {"$gt": now}
        updates["retry_until"] = updates["delete_at"] = now + _RETRY_PERIOD
    else:
        match["retry_until"] = {"$gt": now}
        match["lease"] = doc["lease"]
        if doc["status"] == "submitting":
            match["lease_until"] = {"$lte": now}
    claimed = get_collection(_COLLECTION).find_one_and_update(
        match, {"$set": updates}, return_document=ReturnDocument.AFTER,
    )
    if claimed is not None:
        return claimed, lease

    # A different worker may have completed while this request attempted to acquire the lease.
    current = get(user_id, confirmation_id)
    if current["status"] in _TERMINAL_STATES:
        return current, None
    raise _busy()


def complete(
    user_id: str, confirmation_id: str, lease: str, *,
    result: ActionResult | None = None, error: ApiError | None = None,
) -> dict:
    if result is not None:
        updates = {"status": "succeeded", "result": result.model_dump(mode="json")}
    elif error is not None:
        updates = {"status": "failed", "error": {
            "code": error.code, "message": error.message, "status_code": error.status_code,
        }}
    else:
        updates = {"status": "unknown"}
    updated = get_collection(_COLLECTION).find_one_and_update(
        {**_owned(user_id, confirmation_id), "status": "submitting", "lease": lease},
        {"$set": updates, "$unset": {"lease_until": ""}},
        return_document=ReturnDocument.AFTER,
    )
    return updated if updated is not None else get(user_id, confirmation_id)


def view(doc: dict) -> dict:
    status = state(doc)
    retry_until = doc.get("retry_until")
    body = {
        "confirmation_id": doc["_id"],
        "operation": doc["operation"],
        "order_id": doc["order_id"],
        "status": status,
        "expires_at": doc["expires_at"].replace(tzinfo=UTC),
        "retry_until": retry_until.replace(tzinfo=UTC) if retry_until is not None else None,
        "retryable": status in {"pending", "unknown"},
        "preview": {**doc["preview"], **doc["payload"]},
    }
    if "result" in doc:
        body["result"] = doc["result"]
    if "error" in doc:
        error = doc["error"]
        body["error"] = {"code": error["code"], "message": error["message"]}
    return body
