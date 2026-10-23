"""来源校验与可恢复的发布。导入期不进行任何网络访问。"""
from __future__ import annotations

import hashlib
import math
import time
import uuid
from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument

from app.evolution.flags import writes_enabled
from app.evolution.models import KnowledgeCandidate
from app.evolution.quality import looks_like_non_answer
from app.evolution.repositories import evolution_repo

INDEX_VERSION = "faq-v1"
GAP_KINDS = {"retrieval_miss", "reading_miss", "generation_error", "knowledge_missing"}


def judge_support(answer: str, texts: list[str]) -> float | None:
    """计算答案相对给定证据文本的支持度得分（复用在线接地性评估）。"""
    from app.evolution.online_eval.grounding import compute_groundedness
    return compute_groundedness(answer, texts)


def index_upsert(evo_id, candidate):
    """将候选项写入在线索引（按确定性 ID 幂等 upsert）。"""
    from app.evolution.index.update import upsert_item
    return upsert_item(evo_id, candidate)


def index_deactivate(evo_id):
    """下架索引中指定的条目（按主键删除）。"""
    from app.evolution.index.update import deactivate
    return deactivate(evo_id)


def resolve_source(ref: str) -> dict | None:
    """读取权威的当前块；绝不接受模型提供的元数据。"""
    from app.shared.clients.milvus_gateway import eq_expr, milvus_gateway
    from app.shared.config import settings
    rows = milvus_gateway.milvus_client.query(
        collection_name=milvus_gateway.chunk_collection_name,
        filter=eq_expr("chunk_id", ref), output_fields=["*"], limit=1,
        timeout=settings.milvus.timeout_seconds,
    )
    return rows[0] if rows else None


def validate_sources(doc: dict, *, check_support: bool = True, source_resolver=None) -> list[dict]:
    """校验候选项的全部来源：存在性、版本、有效性与产品归属。

    返回元数据快照（不含正文 text）。可选校验答案是否能在来源中核验。
    """
    refs = doc.get("source_refs") or []
    if not refs or not doc.get("item_names"):
        raise ValueError("来源和产品不能为空")
    expected = {str(s.get("chunk_id")): s for s in doc.get("source_versions") or []}
    sources = []
    for ref in dict.fromkeys(refs):
        row = (source_resolver or resolve_source)(str(ref))
        if not row or not row.get("doc_id") or not row.get("document_version"):
            raise ValueError("来源不存在或没有版本")
        if row.get("valid_until") and float(row["valid_until"]) <= time.time():
            raise ValueError("来源已过期")
        if row.get("status") in {"revoked", "deprecated", "deleted"}:
            raise ValueError("来源已撤回")
        from app.shared.utils.text import is_same_entity, split_subjects
        source_name = str(row.get("item_name") or "")
        if source_name != "default_item_name" and not any(
            is_same_entity(subject, product) for subject in split_subjects(source_name) for product in doc["item_names"]
        ):
            raise ValueError("来源产品不匹配")
        prior = expected.get(str(ref))
        digest = hashlib.sha256(str(row.get("content", "")).encode()).hexdigest()
        if prior and (prior.get("document_version") != row["document_version"] or
                      prior.get("doc_id", row["doc_id"]) != row["doc_id"]):
            raise ValueError("来源版本已变化")
        if prior and prior.get("content_sha256") and prior["content_sha256"] != digest:
            raise ValueError("来源正文已变化")
        sources.append({"chunk_id": str(ref), "doc_id": row["doc_id"],
                        "document_version": row["document_version"],
                        "content_sha256": digest,
                        "text": str(row.get("content", ""))})
    if check_support:
        answer = str(doc.get("faq_answer") or "").strip()
        if looks_like_non_answer(answer):
            raise ValueError("答案必须能在来源正文中核验")
        if not any(answer in s["text"] for s in sources):
            from app.shared.config import settings
            score = judge_support(answer, [s["text"] for s in sources])
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score) or not 0 <= score <= 1 or score < settings.evolution.groundedness_min:
                raise ValueError("答案支持度不足或无法核验")
    return [{k: v for k, v in source.items() if k != "text"} for source in sources]


def content_hash(doc: dict) -> str:
    """对候选项的核心内容字段做确定性哈希，作为发布内容的指纹。"""
    import json
    payload = {key: doc.get(key) for key in ("faq_question", "faq_answer", "source_refs", "source_versions", "item_names")}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def queue_publication(candidate_id: str) -> str:
    """把候选入队到发布任务（幂等，返回任务键）。"""
    cid = ObjectId(candidate_id)
    doc = evolution_repo.k_candidates.find_one({"_id": cid})
    if not doc:
        raise ValueError("候选不存在")
    digest = content_hash(doc)
    key = f"{candidate_id}:{doc.get('content_version', 1)}:{digest}:{INDEX_VERSION}"
    evolution_repo.publication_tasks.update_one({"_id": key}, {"$setOnInsert": {
        "candidate_id": cid, "content_version": doc.get("content_version", 1),
        "digest": digest, "index_version": INDEX_VERSION, "state": "pending",
        "lease_until": 0, "attempts": 0, "next_retry": 0, "created_at": time.time(),
    }}, upsert=True)
    return key


def approve_candidate(candidate_id: str, reason: str = "") -> tuple[bool, str]:
    """审批并发布候选：校验来源、写入索引，返回 (是否成功, 说明)。"""
    if not writes_enabled():
        return False, "知识写入已关闭"
    try:
        try:
            cid = ObjectId(candidate_id)
        except Exception:
            return False, "候选 ID 不合法"
        doc = evolution_repo.k_candidates.find_one({"_id": cid})
        if not doc:
            return False, "候选不存在"
        if doc.get("status") == "need_info":
            return False, "该候选没有可用事实，请先编辑补充答案和来源后再通过"
        if doc.get("status") not in {"draft", "active"}:
            return False, "候选不在可审批状态"
        if looks_like_non_answer(doc.get("faq_answer")):
            return False, "候选答案未包含可入库的事实，请先编辑补充后再通过"
        if doc.get("publication_status") in {"deprecated", "revoked"}:
            return False, "候选已失效，请复核来源后编辑"
        sources = validate_sources(doc)
        if is_retrievable(doc):
            return True, ""
        patch = {"schema_version": 1, "source_versions": sources, "reason": reason,
                 "publication_status": "indexing", "approved_at": time.time(),
                 "content_version": doc.get("content_version", 1)}
        result = evolution_repo.k_candidates.update_one(
            {"_id": cid, "faq_answer": doc.get("faq_answer"), "faq_question": doc.get("faq_question"),
             "source_refs": doc.get("source_refs"), "status": doc["status"],
             "publication_status": doc.get("publication_status")}, {"$set": patch})
        if not result.matched_count:
            return False, "候选已变化，请重试"
        key = queue_publication(candidate_id)
        run_publication_tasks(limit=1, task_key=key)
        current = evolution_repo.k_candidates.find_one({"_id": cid})
        if is_retrievable(current):
            return True, ""
        return False, "发布尚未完成，任务已保存，可重试"
    except Exception as exc:
        return False, str(exc)


def run_publication_tasks(limit: int = 20, *, task_key: str | None = None) -> dict[str, int]:
    """消费发布队列：处理待办发布/撤销任务，返回 {completed, failed} 计数。
    """
    result = {"completed": 0, "failed": 0}
    if not writes_enabled():
        return result
    tasks = evolution_repo.publication_tasks
    if not task_key:
        # 修复候选状态与出站队列插入之间的中断窗口。
        for doc in evolution_repo.k_candidates.find({"publication_status": "indexing", "approved_at": {"$exists": True}}).limit(limit):
            queue_publication(str(doc["_id"]))
        for doc in evolution_repo.k_candidates.find({"publication_status": "revoked", "evo_doc_id": {"$ne": None}}).limit(limit):
            _queue_revocation(doc)
    for _ in range(limit):
        now, token = time.time(), uuid.uuid4().hex
        query = {"state": {"$in": ["pending", "retry", "running"]},
                 "lease_until": {"$lte": now}}
        if task_key:
            query["_id"] = task_key
        else:
            query["next_retry"] = {"$lte": now}
        task = tasks.find_one_and_update(query, {"$set": {"state": "running", "lease_until": now + 120,
            "lease_token": token}, "$inc": {"attempts": 1}}, return_document=ReturnDocument.AFTER)
        if not task:
            break
        try:
            doc = evolution_repo.k_candidates.find_one({"_id": task["candidate_id"]})
            if task.get("action") == "revoke":
                if not index_deactivate(task["evo_doc_id"]):
                    raise RuntimeError("index deletion failed")
            else:
                if (not doc or doc.get('status') not in {'draft', 'active'} or
                        doc.get("publication_status") != "indexing" or content_hash(doc) != task["digest"]):
                    tasks.update_one({"_id": task["_id"], "lease_token": token}, {"$set": {"state": "cancelled"}})
                    continue
                validate_sources(doc, check_support=False)
                evo_id = "evo_" + hashlib.sha256(task["_id"].encode()).hexdigest()[:32]
                item = KnowledgeCandidate(**{k: v for k, v in doc.items() if k != "_id"})
                item.status = "active"
                item.publication_status = "active"
                item.content_sha256 = task["digest"]
                item.index_version = INDEX_VERSION
                ownership = {'_id': task['_id'], 'lease_token': token,
                             'state': 'running', 'lease_until': {'$gt': time.time()}}
                if not tasks.find_one(ownership):
                    continue
                if not index_upsert(evo_id, item):
                    raise RuntimeError("index write failed")
                ownership['lease_until'] = {'$gt': time.time()}
                if not tasks.find_one(ownership):
                    # 后继者可能持有这个相同的确定性向量 ID。
                    # 过期的 worker 既不能激活它，也不能删除它。
                    continue
                validate_sources(doc, check_support=False)
                ownership['lease_until'] = {'$gt': time.time()}
                renewed = tasks.update_one(ownership, {'$set': {'lease_until': time.time() + 120}})
                if not renewed.matched_count:
                    continue
                updated = evolution_repo.k_candidates.update_one(
                    {"_id": doc["_id"], 'status': doc['status'],
                     "publication_status": "indexing", "content_version": task["content_version"]},
                    {"$set": {"status": "active", "publication_status": "active", "evo_doc_id": evo_id,
                              "index_version": INDEX_VERSION, "content_sha256": task["digest"]}})
                if not updated.matched_count:
                    current = evolution_repo.k_candidates.find_one({'_id': doc['_id']}) or {}
                    if current.get('evo_doc_id') != evo_id or current.get('publication_status') != 'active':
                        index_deactivate(evo_id)
                    raise RuntimeError("candidate changed during publication")
                if doc.get("gap_id"):
                    try:
                        evolution_repo.k_gaps.update_one({"_id": ObjectId(doc["gap_id"])}, {"$set": {"status": "resolved"}})
                    except Exception:
                        pass  # 源头缺口属辅助信息；发布仍然有效。
            tasks.update_one({"_id": task["_id"], "lease_token": token}, {"$set": {
                "state": "completed", "lease_until": 0, "completed_at": time.time(), "last_error": ""}})
            result["completed"] += 1
        except Exception as exc:
            tasks.update_one({"_id": task["_id"], "lease_token": token}, {"$set": {
                "state": "retry", "lease_until": 0, "next_retry": time.time() + min(3600, 2 ** min(task["attempts"], 11)),
                "last_error": str(exc)}})
            result["failed"] += 1
            if task_key:
                break
    return result


def is_retrievable(doc: dict | None, *, source_resolver=None) -> bool:
    """判断候选项是否已完全发布且来源/指纹一致，可被在线检索命中。"""
    if not doc or doc.get("schema_version") != 1 or doc.get("status") != "active" or doc.get("publication_status") != "active":
        return False
    if not doc.get("source_versions") or not doc.get("evo_doc_id"):
        return False
    try:
        return validate_sources(doc, check_support=False, source_resolver=source_resolver) == doc["source_versions"] and content_hash(doc) == doc.get("content_sha256")
    except Exception:
        return False


def _queue_revocation(doc: dict) -> str:
    """把撤销入队到发布任务（幂等，返回任务键）。"""
    key = f"revoke:{doc['evo_doc_id']}"
    evolution_repo.publication_tasks.update_one({"_id": key}, {"$setOnInsert": {
        "action": "revoke", "candidate_id": doc["_id"], "evo_doc_id": doc["evo_doc_id"],
        "state": "pending", "lease_until": 0, "next_retry": 0, "attempts": 0}}, upsert=True)
    return key


def revoke_candidate(candidate_id: str, reason: str = "revoked", *, process_task: bool = True) -> bool:
    """撤销候选：标记为失效并入队下架任务（可选同步处理），返回是否成功。"""
    if not writes_enabled():
        return False
    cid = ObjectId(candidate_id)
    doc = evolution_repo.k_candidates.find_one_and_update({"_id": cid}, {"$set": {
        "status": "deprecated", "publication_status": "revoked", "reason": reason}}, return_document=ReturnDocument.AFTER)
    if not doc:
        return False
    if doc.get("evo_doc_id"):
        key = _queue_revocation(doc)
        if process_task:
            run_publication_tasks(limit=1, task_key=key)
    return True


def invalidate_document(doc_id: str, version: str | None = None) -> int:
    """来源文档失效：撤销所有引用它的候选，返回被撤销的条数。"""
    if not writes_enabled():
        return 0
    query = {"source_versions": {"$elemMatch": {"doc_id": doc_id, **({"document_version": version} if version else {})}}}
    count = 0
    for doc in evolution_repo.k_candidates.find(query):
        count += int(revoke_candidate(str(doc["_id"]), "source document invalidated"))
    return count


def approved_faq_snapshot() -> list[dict]:
    """导出所有已发布且可检索候选的元数据快照（供评估/对账）。"""
    fields = ("evo_doc_id", "content_sha256", "content_version", "index_version", "source_versions")
    return sorted([{key: doc.get(key) for key in fields} for doc in evolution_repo.k_candidates.find({"status": "active"})
                   if is_retrievable(doc)], key=lambda doc: doc["evo_doc_id"])


def migrate_candidates(*, apply: bool = False) -> dict[str, int]:
    report = {"scanned": 0, "review_required": 0, "updated": 0}
    for doc in evolution_repo.k_candidates.find({"schema_version": {"$ne": 1}}):
        report["scanned"] += 1
        report["review_required"] += 1
        if apply:
            evolution_repo.k_candidates.update_one({"_id": doc["_id"]}, {"$set": {
                "schema_version": 1, "publication_status": "review_required", "content_version": 1}})
            report["updated"] += 1
    return report


if __name__ == "__main__":
    import argparse
    import json
    parser = argparse.ArgumentParser(description="Report legacy FAQ migration. Use --apply for additive metadata updates.")
    parser.add_argument("--apply", action="store_true")
    print(json.dumps(migrate_candidates(apply=parser.parse_args().apply)))