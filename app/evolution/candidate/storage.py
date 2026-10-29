"""同一缺口共用候选主键。重试不会覆盖已编辑或审批的候选。"""
from __future__ import annotations

import hashlib

from bson import ObjectId

from app.evolution.models import KnowledgeCandidate
from app.evolution.repositories import evolution_repo
from app.evolution.gap.registration import registration_fields, require_current_claim


def store_candidate(candidate: KnowledgeCandidate) -> dict:
    fields = registration_fields(candidate.gap_id)
    collection = evolution_repo.k_candidates
    if not candidate.gap_id:
        doc = candidate.model_dump()
        doc["_id"] = collection.insert_one(doc).inserted_id
        return doc
    existing = collection.find_one({"gap_id": candidate.gap_id})
    if existing:
        require_current_claim()
        return existing
    oid = ObjectId(hashlib.sha256(("gap-candidate:" + candidate.gap_id).encode()).hexdigest()[:24])
    collection.update_one({"_id": oid}, {"$setOnInsert": {**candidate.model_dump(), **fields}}, upsert=True)
    require_current_claim()
    return collection.find_one({"_id": oid})
