"""只读检索契约。所有服务客户端均在离线环境中替换。"""
from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

import pytest

from app.rag.agentic.contracts import RetrievalScope
from app.rag.agentic.evidence import docs_to_evidence, evidence_to_docs
from app.rag.agentic import tools
from app.rag.item_name.config import GLOBAL_ITEM_NAME


def chunk(chunk_id="c1", **changes):
    result = dict(chunk_id=chunk_id, doc_id="d1", parent_id="p1", item_name="HAK 180",
                  document_version="v1", document_kind="manual", content="E01 reset", seq=5)
    result.update(changes)
    return result


class Client:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def query(self, **kwargs):
        self.calls.append(kwargs)
        return self.rows


@pytest.fixture
def service(monkeypatch):
    client = Client()
    gateway = SimpleNamespace(milvus_client=client, chunk_collection_name="chunks",
                              evolution_collection_name="faq", item_name_collection_name="items")
    monkeypatch.setattr(tools, "milvus_gateway", gateway)
    monkeypatch.setattr(tools, "expand_scope_names", lambda names, deadline: names)
    return tools.RetrievalTools({"item_names": ["HAK 180"]}), client


def test_evidence_roundtrip_keeps_parent_context_and_hash():
    document = chunk(parent_content="Power must be off before E01 reset.", title="Reset")
    evidence = docs_to_evidence([document], scope=RetrievalScope(item_names=["HAK 180"]))
    assert len(evidence) == 1
    assert "Power must be off" in evidence[0].text
    assert "E01 reset" in evidence[0].text
    assert evidence[0].content_sha256 == hashlib.sha256(evidence[0].text.encode()).hexdigest()
    restored = evidence_to_docs(evidence)[0]
    assert restored["parent_content"] == document["parent_content"]
    assert restored["text"] == document["content"]
    assert restored["document_version"] == "v1"


def test_evidence_filters_wrong_product_expired_policy_and_unapproved_faq():
    rows = [chunk(), chunk("c2", item_name="HAK 190"), chunk("c3", item_name=GLOBAL_ITEM_NAME),
            chunk("c4", document_kind="policy", valid_until=time.time()-1),
            chunk("c5", source="evolution", approval_status="draft", source_version="v1")]
    assert [e.chunk_id for e in docs_to_evidence(rows, scope=RetrievalScope(item_names=["HAK 180"]))] == ["c1", "c3"]


def test_evidence_enforces_version_and_accepts_known_product_alias():
    scope = RetrievalScope(item_names=["HAK 180"], corpus_version="v1")
    rows = [chunk(item_name="Brother HAK 180"), chunk("c2", document_version="v2")]
    assert [e.chunk_id for e in docs_to_evidence(rows, scope=scope)] == ["c1"]


def test_keyword_search_has_scoped_bounded_query_and_literal_matching(service):
    retriever, client = service
    client.rows = [chunk(), chunk("c2", content="Other fault"), chunk("c3", item_name="HAK 190")]
    result = retriever.execute("keyword_search", {"terms": ["E01"], "limit": 2}, 1.0)
    assert result.status == "ok"
    assert [e.chunk_id for e in result.evidence] == ["c1"]
    assert "item_name" in client.calls[0]["filter"]
    assert "HAK 180" in client.calls[0]["filter"]
    assert client.calls[0]["limit"] == tools.settings.agentic.keyword_scan_limit + 1
    assert 0 < client.calls[0]["timeout"] <= 1.0


def test_keyword_scan_overflow_is_unavailable_instead_of_partial_result(service, monkeypatch):
    retriever, client = service
    monkeypatch.setattr(tools.settings.agentic, "keyword_scan_limit", 2)
    client.rows = [chunk(str(i)) for i in range(3)]
    result = retriever.execute("keyword_search", {"terms": ["E01"]}, 1)
    assert result.status == "unavailable"
    assert result.evidence == []


def test_scope_arguments_cannot_authorize_another_product(service):
    retriever, client = service
    result = retriever.execute("keyword_search", {"terms": ["E01"], "scope": {"item_names": ["HAK 190"]}}, 1)
    assert result.status == "invalid"
    assert not client.calls


@pytest.mark.parametrize("requested", ["HAK", "HAK 180 Pro", "HAK 180,HAK 190"])
def test_scope_rejects_partial_or_mixed_product_names_before_retrieval(service, requested):
    retriever, client = service
    client.rows = [chunk("foreign", item_name="HAK 190")]
    result = retriever.execute("keyword_search", {
        "terms": ["E01"], "scope": {"item_names": [requested]}}, 1)
    assert result.status == "invalid"
    assert result.evidence == []
    assert not client.calls


def test_scope_alias_resolves_to_server_canonical_name(service):
    retriever, _ = service
    scope = retriever._scope({"scope": {"item_names": ["Brother HAK 180"]}})
    assert scope.item_names == ["HAK 180"]


def test_evidence_does_not_treat_different_product_suffix_as_same_model():
    rows = [chunk(), chunk("foreign", item_name="HAK 180 Pro")]
    refs = docs_to_evidence(rows, scope=RetrievalScope(item_names=["HAK 180"]))
    assert [ref.chunk_id for ref in refs] == ["c1"]


def test_missing_scope_rejects_database_scan(monkeypatch):
    monkeypatch.setattr(tools, "expand_scope_names", lambda names, deadline: names)
    result = tools.RetrievalTools({}).execute("keyword_search", {"terms": ["E01"]}, 1)
    assert result.status == "invalid"


def test_read_revalidates_neighbors_and_parent_identity(service, monkeypatch):
    retriever, client = service
    responses = [[chunk()], [chunk("c0", seq=4), chunk("c2", seq=6),
                             chunk("foreign", seq=6, item_name="HAK 190")]]
    client.query = lambda **kwargs: (client.calls.append(kwargs), responses.pop(0))[1]
    monkeypatch.setattr(tools, "fetch_scoped_parents", lambda docs, deadline: {"p1": {
        "parent_id": "p1", "doc_id": "d1", "document_version": "v1", "content": "Safe reset"}})
    result = retriever.execute("read_chunk", {"chunk_ids": ["c1"], "neighbor_window": 1}, 1)
    assert result.status == "ok"
    assert {e.chunk_id for e in result.evidence} == {"c0", "c1", "c2"}
    assert "Safe reset" in result.evidence[0].text
    assert "doc_id" in client.calls[1]["filter"] and "seq" in client.calls[1]["filter"]


def test_read_does_not_inject_parent_from_other_document(service, monkeypatch):
    retriever, client = service
    client.rows = [chunk()]
    monkeypatch.setattr(tools, "fetch_scoped_parents", lambda docs, deadline: {"p1": {
        "parent_id": "p1", "doc_id": "foreign", "document_version": "v1", "content": "SECRET"}})
    result = retriever.execute("read_chunk", {"chunk_ids": ["c1"], "neighbor_window": 0}, 1)
    assert result.status == "ok"
    assert "SECRET" not in result.evidence[0].text


def test_unavailable_is_distinct_from_empty_and_expired_deadline(service):
    retriever, client = service
    assert retriever.execute("keyword_search", {"terms": ["missing"]}, 1).status == "empty"
    client.query = lambda **kwargs: (_ for _ in ()).throw(ConnectionError("offline"))
    assert retriever.execute("keyword_search", {"terms": ["E01"]}, 1).status == "unavailable"
    assert retriever.execute("keyword_search", {"terms": ["E01"]}, 0).status == "timeout"


def test_policy_mode_does_not_allow_web_or_nonpolicy_documents(service):
    retriever, client = service
    retriever.state["business_tag"] = "pricing"
    client.rows = [chunk(), chunk("policy", document_kind="policy", valid_until=time.time()+100)]
    result = retriever.execute("keyword_search", {"terms": ["E01"]}, 1)
    assert [e.chunk_id for e in result.evidence] == ["policy"]
    assert retriever.execute("web_search", {"query": "price"}, 1).status == "invalid"


def test_revalidation_removes_changed_deleted_and_foreign_sources(service):
    retriever, client = service
    refs = docs_to_evidence([chunk(), chunk("changed"), chunk("deleted"), chunk("foreign")])
    client.rows = [chunk(), chunk("changed", content="Updated instructions"), chunk("foreign", item_name="HAK 190")]
    result = retriever.revalidate([ref.model_dump() for ref in refs], 1)
    assert [ref.chunk_id for ref in result] == ["c1"]
    assert client.calls[0]["timeout"] <= 1


def test_revalidation_requires_parent_context_to_remain_identical(service, monkeypatch):
    retriever, client = service
    refs = docs_to_evidence([chunk(parent_content="Original parent")])
    client.rows = [chunk()]
    monkeypatch.setattr(tools, "fetch_scoped_parents", lambda docs, deadline: {"p1": {
        "parent_id": "p1", "doc_id": "d1", "document_version": "v1", "content": "Changed parent"}})
    assert retriever.revalidate(refs, 1) == []


def test_keyword_snippets_require_read_before_final_validation(service):
    retriever, client = service
    client.rows = [chunk()]
    result = retriever.execute("keyword_search", {"terms": ["E01"]}, 1)
    assert result.evidence[0].metadata["read"] is False
    assert retriever.revalidate(result.evidence, 1) == []


def test_semantic_search_passes_remaining_timeout_to_actual_rpc(service, monkeypatch):
    retriever, client = service
    monkeypatch.setattr(tools.llm_providers, "embed_text", lambda query: {"dense": [[1.0]], "sparse": [{1: 1.0}]})
    monkeypatch.setattr(tools.milvus_gateway, "create_requests", lambda **kwargs: [kwargs], raising=False)
    calls = []
    client.hybrid_search = lambda **kwargs: (calls.append(kwargs), [[{"entity": chunk(), "distance": .9}]])[1]
    result = retriever.execute("semantic_search", {"query": "Reset E01"}, .5)
    assert result.status == "ok"
    assert result.evidence[0].metadata["read"] is True
    assert 0 < calls[0]["timeout"] <= .5
    assert "HAK 180" in calls[0]["reqs"][0]["expr"]


def test_local_embedding_deadline_exhaustion_prevents_network_request(service, monkeypatch):
    retriever, client = service
    def slow_local_model(query):
        time.sleep(.02)
        return {"dense": [[1.0]], "sparse": [{1: 1.0}]}
    monkeypatch.setattr(tools.llm_providers, "embed_text", slow_local_model)
    result = retriever.execute("semantic_search", {"query": "Reset E01"}, .001)
    assert result.status == "timeout"
    assert client.calls == []


def test_faq_search_rejects_unapproved_missing_version_and_other_product(service, monkeypatch):
    from app.evolution import flags
    from app.evolution.governance import content_hash
    retriever, client = service
    monkeypatch.setattr(flags, "recall_enabled", lambda: True)
    monkeypatch.setattr(tools.llm_providers, "embed_text", lambda query: {"dense": [[1.0]], "sparse": [{1: 1.0}]})
    monkeypatch.setattr(tools.milvus_gateway, "create_requests", lambda **kwargs: [kwargs], raising=False)
    def faq(identity, **changes):
        row = {"evo_doc_id": identity, "item_name": "HAK 180", "status": "active",
               "source_version": "v1", "faq_answer": "Reset E01 using the switch.", "faq_question": "How to reset E01?"}
        row.update(changes)
        return {"entity": row, "distance": .8}
    calls = []
    client.hybrid_search = lambda **kwargs: (calls.append(kwargs), [[faq("good"), faq("draft", status="draft"),
        faq("unversioned", source_version=""), faq("foreign", item_name="HAK 190")]])[1]
    candidate = {"schema_version": 1, "evo_doc_id": "good", "status": "active", "publication_status": "active",
        "content_version": 1, "item_names": ["HAK 180"], "faq_question": "How to reset E01?",
        "faq_answer": "Reset E01 using the switch.", "source_refs": ["c1"], "source_versions": [{
            "chunk_id": "c1", "doc_id": "d1", "document_version": "v1",
            "content_sha256": hashlib.sha256(b"E01 reset").hexdigest()}]}
    candidate["content_sha256"] = content_hash(candidate)
    client.rows = [chunk()]
    monkeypatch.setattr(tools, "get_collection", lambda name: SimpleNamespace(
        find_one=lambda query: candidate if query.get("evo_doc_id") == "good" else None))
    result = retriever.execute("search_approved_faq", {"query": "Reset E01"}, 1)
    assert result.status == "ok"
    assert [ref.chunk_id for ref in result.evidence] == ["good"]
    assert result.evidence[0].source_version == candidate["content_sha256"]
    assert "active" in calls[0]["reqs"][0]["expr"]


def test_stale_active_vector_cannot_bypass_revoked_mongo_candidate(service, monkeypatch):
    from app.evolution import flags
    retriever, client = service
    monkeypatch.setattr(flags, "recall_enabled", lambda: True)
    monkeypatch.setattr(tools.llm_providers, "embed_text", lambda query: {"dense": [[1.0]], "sparse": [{1: 1.0}]})
    monkeypatch.setattr(tools.milvus_gateway, "create_requests", lambda **kwargs: [kwargs], raising=False)
    client.hybrid_search = lambda **kwargs: [[{"entity": {"evo_doc_id": "revoked", "status": "active",
        "source_version": "old", "faq_answer": "Reset switch", "item_name": "HAK 180"}}]]
    monkeypatch.setattr(tools, "get_collection", lambda name: SimpleNamespace(
        find_one=lambda query: {"evo_doc_id": "revoked", "status": "deprecated", "publication_status": "revoked"}))
    result = retriever.execute("search_approved_faq", {"query": "Reset E01"}, 1)
    assert result.status == "empty"
    assert result.evidence == []


def test_web_result_has_fetch_time_and_source_but_respects_request_budget(service, monkeypatch):
    retriever, client = service
    monkeypatch.setattr(tools.settings.mcp, "base_url", "https://example.test/search")
    monkeypatch.setattr(tools.settings.mcp, "api_key", "offline-test-key")
    calls = []
    async def request(query, limit, deadline):
        calls.append((query, limit, tools.remaining_seconds(deadline)))
        return SimpleNamespace(content=[SimpleNamespace(text='{"pages":[{"title":"Reset","snippet":"E01 reset","url":"https://example.test/reset"}]}')])
    monkeypatch.setattr(retriever, "_request_web", request)
    result = retriever.execute("web_search", {"query": "Reset E01"}, 1)
    assert result.status == "ok"
    assert result.evidence[0].source == "web" and result.evidence[0].fetched_at > 0
    assert 0 < calls[0][2] <= 1


def test_inconsistent_context_metadata_does_not_pass_evidence_validation():
    from app.rag.agentic.evidence import evidence_is_valid
    ref = docs_to_evidence([chunk(parent_content="Actual parent")])[0]
    ref.metadata["parent_content"] = "Other parent"
    assert not evidence_is_valid(ref)