"""Keep one active and one paused commerce draft outside knowledge history."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from app.api.errors import ApiError
from app.commerce import confirmations
from app.commerce.models import TaskDraft
from app.shared.clients import mongo


_COLLECTION = "commerce_tasks"
_TTL = timedelta(minutes=15)
_OPEN_CONFIRMATIONS = {"pending", "submitting", "unknown"}


@dataclass(frozen=True)
class Snapshot:
    key: str
    revision: str | None = None
    active: TaskDraft | None = None
    paused: TaskDraft | None = None


def account_scope(owner_token: str, user_id: str) -> str:
    return sha256(f"commerce-account\0{owner_token}\0{user_id}".encode()).hexdigest()


def key(scope: str, session_id: str) -> str:
    return sha256(f"{scope}\0{session_id}".encode()).hexdigest()


def _open_draft(user_id: str, draft: TaskDraft | None) -> TaskDraft | None:
    if draft is not None and draft.confirmation_id is not None:
        try:
            doc = confirmations.get(user_id, draft.confirmation_id)
        except ApiError as exc:
            if exc.code == "commerce_not_found":
                return None
            raise
        if confirmations.state(doc) not in _OPEN_CONFIRMATIONS:
            return None
    return draft


def load(task_key: str, user_id: str) -> Snapshot:
    doc = mongo.get_collection(_COLLECTION).find_one({"_id": task_key})
    if doc is None:
        return Snapshot(task_key)
    if doc["expires_at"] <= confirmations._mongo_now():
        return Snapshot(task_key, doc["revision"])
    active, paused = (TaskDraft.model_validate(doc[slot]) if doc.get(slot) else None
                      for slot in ("active", "paused"))
    return Snapshot(task_key, doc["revision"], _open_draft(user_id, active), _open_draft(user_id, paused))


def save(
    previous: Snapshot, active: TaskDraft | None, paused: TaskDraft | None,
) -> Snapshot:
    if (active, paused) == (previous.active, previous.paused):
        return previous
    collection = mongo.get_collection(_COLLECTION)
    revision = str(uuid4())
    doc = {
        "_id": previous.key, "revision": revision, "expires_at": confirmations._mongo_now() + _TTL,
        "active": active.model_dump(exclude_none=True) if active else None,
        "paused": paused.model_dump(exclude_none=True) if paused else None,
    }
    try:
        if previous.revision is None:
            collection.create_index("expires_at", expireAfterSeconds=0)
            collection.insert_one(doc)
        elif not collection.replace_one(
            {"_id": previous.key, "revision": previous.revision}, doc,
        ).matched_count:
            raise _conflict()
    except DuplicateKeyError as exc:
        raise _conflict() from exc
    return Snapshot(previous.key, revision, active, paused)


def _conflict() -> ApiError:
    return ApiError("commerce_task_conflict", "任务已更新。请查看当前任务后重试。", 409)
