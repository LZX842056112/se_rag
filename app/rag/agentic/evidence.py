"""无损的来源适配器与失败即关闭的证据校验。"""
from __future__ import annotations

import hashlib
import math
import time
from typing import Iterable

from app.rag.agentic.contracts import EvidenceRef, RetrievalScope
from app.rag.item_name.config import GLOBAL_ITEM_NAME
from app.rag.query.business import is_current_policy
from app.shared.utils.text import is_same_entity, split_subjects


def product_in_scope(item_name, item_names: list[str]) -> bool:
    """接受既定的别名规则，包括可选的品牌前缀。"""
    return str(item_name or "").strip() == GLOBAL_ITEM_NAME or any(
        is_same_entity(subject, name) for subject in split_subjects(item_name) for name in item_names)


def _optional_string(value) -> str | None:
    return str(value) if value is not None and str(value) else None


def doc_to_evidence(doc: dict) -> EvidenceRef:
    """对实际上下文（包括任何注入的父文本）做哈希。"""
    source = "web" if doc.get("type") == "web" or doc.get("source") == "web" else (
        "evolution" if doc.get("source") == "evolution" else "kb")
    child_text = str(doc.get("text") or doc.get("content") or doc.get("snippet") or doc.get("faq_answer") or "")
    parent_text = str(doc.get("parent_content") or "")
    text = f"{parent_text}\n\n{child_text}" if parent_text else child_text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    chunk_id = _optional_string(doc.get("chunk_id") or doc.get("evo_doc_id"))
    identity = chunk_id or str(doc.get("url") or doc.get("doc_id") or digest)
    metadata = {k: v for k, v in doc.items() if k not in {"metadata", "_id"}}
    metadata.update(doc.get("metadata") or {})
    metadata["child_text"] = child_text
    if parent_text:
        metadata["parent_content"] = parent_text
    else:
        metadata.pop("parent_content", None)
    metadata.setdefault("read", True)
    version = str(doc.get("document_version") or doc.get("source_version") or "")
    version_hash = hashlib.sha256(version.encode("utf-8")).hexdigest()[:8]
    return EvidenceRef(
        evidence_id=f"{source}:{identity}:{version_hash}:{digest[:16]}", source=source,
        doc_id=_optional_string(doc.get("doc_id")), chunk_id=chunk_id,
        parent_id=_optional_string(doc.get("parent_id")), item_name=_optional_string(doc.get("item_name")),
        text=text, content_sha256=digest, page=doc.get("page"),
        document_version=_optional_string(doc.get("document_version")),
        document_kind=_optional_string(doc.get("document_kind")), valid_until=doc.get("valid_until"),
        approval_status=_optional_string(doc.get("approval_status") or doc.get("status")),
        source_version=_optional_string(doc.get("source_version")), fetched_at=doc.get("fetched_at"),
        title=_optional_string(doc.get("title") or doc.get("faq_question")),
        url=_optional_string(doc.get("url")), metadata=metadata,
    )


def evidence_is_valid(ref: EvidenceRef, scope: RetrievalScope | None = None, *, now: float | None = None) -> bool:
    """在每个边界上应用产品、版本、发布与策略校验。"""
    now = time.time() if now is None else now
    if not ref.text.strip() or hashlib.sha256(ref.text.encode("utf-8")).hexdigest() != ref.content_sha256:
        return False
    if "child_text" in ref.metadata:
        child = str(ref.metadata["child_text"])
        parent = str(ref.metadata.get("parent_content") or "")
        if ref.text != (f"{parent}\n\n{child}" if parent else child):
            return False
    if scope is not None:
        if ref.source != "web" and not product_in_scope(ref.item_name, scope.item_names):
            return False
        if scope.corpus_version and ref.document_version != scope.corpus_version:
            return False
        if scope.allowed_document_kinds and ref.document_kind not in scope.allowed_document_kinds:
            return False
    if ref.document_kind == "policy" and not is_current_policy(ref.model_dump(), now):
        return False
    if ref.source == "evolution" and (ref.approval_status not in {"approved", "active"} or not ref.source_version):
        return False
    if ref.source == "web" and (not ref.url or ref.fetched_at is None or not math.isfinite(ref.fetched_at)):
        return False
    return True


def docs_to_evidence(docs: Iterable[dict], *, scope: RetrievalScope | None = None,
                     now: float | None = None) -> list[EvidenceRef]:
    result: list[EvidenceRef] = []
    seen: set[str] = set()
    for doc in docs or []:
        try:
            ref = doc_to_evidence(doc)
        except (ValueError, TypeError):
            continue
        if ref.evidence_id not in seen and evidence_is_valid(ref, scope, now=now):
            seen.add(ref.evidence_id)
            result.append(ref)
    return result


def evidence_to_doc(evidence: EvidenceRef | dict) -> dict:
    """为现有的答案提示词构建器一次性恢复子文本与父文本。"""
    ref = evidence if isinstance(evidence, EvidenceRef) else EvidenceRef.model_validate(evidence)
    doc = dict(ref.metadata)
    doc.update({k: v for k, v in ref.model_dump(exclude={"metadata", "text"}).items() if v is not None})
    doc["text"] = str(ref.metadata.get("child_text", ref.text))
    doc["content"] = doc["text"]
    doc["type"] = "web" if ref.source == "web" else "milvus"
    doc["source"] = "milvus" if ref.source == "kb" else ref.source
    return doc


def evidence_to_docs(evidence: Iterable[EvidenceRef | dict]) -> list[dict]:
    return [evidence_to_doc(ref) for ref in evidence]