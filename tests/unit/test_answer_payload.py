"""HTTP 与 SSE 共用字段，但保持各自既有的空值和独有字段。"""
from copy import deepcopy
from types import SimpleNamespace

from fastapi import BackgroundTasks, FastAPI
from fastapi.testclient import TestClient
import pytest


COMMON = {
    "answer": "答", "answer_id": "", "business_tag": "", "handoff_required": False,
    "issue_id": "", "metrics": {}, "image_urls": [], "item_names": [], "citations": [],
    "groundedness": None, "item_name_options": [], "intent": "rag",
}


def test_common_fields_keep_defaults_without_adding_transport_fields():
    from app.rag.query.answer_payload import answer_fields
    assert answer_fields({"answer": "答"}) == COMMON
    explicit = {**COMMON, "answer_id": None, "groundedness": None, "intent": ""}
    assert answer_fields(explicit) == {**COMMON, "answer_id": None}


def test_common_fields_keep_actual_citations_and_image_selection():
    from app.rag.query.answer_payload import answer_fields
    citation = {"source": "kb", "chunk_id": "child-1", "page": 3, "version": "v1"}
    state = {"answer": "答", "citations": [citation], "image_urls": ["https://example.com/step.jpg"],
             "answer_contexts": ["父章节正文"], "evidence_manifest": [{"source_id": "parent-1"}]}
    original = deepcopy(state)
    result = answer_fields(state)
    assert result["citations"] == [citation]
    assert result["image_urls"] == state["image_urls"]
    assert "answer_contexts" not in result and "evidence_manifest" not in result
    assert state == original


def test_answer_prompt_exposes_original_model_and_evidence_section(monkeypatch):
    from app.rag.query import answer_service

    monkeypatch.setattr(answer_service, "build_history_context", lambda *_args, **_kwargs: "")
    state = {
        "original_query": "HL-3070CW 如何设置 IP Boot Tries？",
        "rewritten_query": "打印服务器如何设置尝试次数？",
        "reranked_docs": [{
            "chunk_id": "ip-tries", "source": "milvus", "type": "milvus", "title": "网络说明书",
            "heading_path": "无线配置（适用于 HL-3070CW） / TCP/IP",
            "text": "选择 IP Boot Tries。按 + 或 - 设置尝试次数。",
        }],
    }
    prompt = answer_service.load_answer_prompt(state)
    assert state["original_query"] in prompt and state["rewritten_query"] in prompt
    assert state["reranked_docs"][0]["heading_path"] in state["answer_contexts"][0]


def test_http_keeps_null_answer_id_normalization_and_own_fields(monkeypatch):
    from app.api.routers import query as api
    from app.api.schema.query_schema import QueryRequestSchema
    monkeypatch.setattr(api, "authorize_session", lambda *a, **k: None)
    monkeypatch.setattr(api, "reserve_query", lambda *a: "turn-a")
    monkeypatch.setattr(api, "get_done_task_list", lambda *a: ["已完成"])
    monkeypatch.setattr(api, "invoke_query_graph", lambda **k: {"answer": "答", "answer_id": None})
    response = api.query(BackgroundTasks(), QueryRequestSchema(query="问", session_id="session-a", is_stream=False))
    assert response.model_dump() == {
        **COMMON, "message": "已完成问的内容检索", "session_id": "session-a",
        "done_list": ["已完成"], "retrieval_signals": {}, "turn_id": "turn-a",
        # 未请求证据时保持空值，避免普通问答响应体膨胀
        "evidence": [], "retrieval_layers": {},
    }


def test_http_returns_evidence_only_when_requested(monkeypatch):
    """评测请求才回传证据正文与分层 chunk_id。"""
    from app.api.routers import query as api
    monkeypatch.setattr(api, "authorize_session", lambda *a, **k: None)
    monkeypatch.setattr(api, "reserve_query", lambda *a: "turn-b")
    monkeypatch.setattr(api, "get_done_task_list", lambda *a: [])
    monkeypatch.setattr(api, "invoke_query_graph", lambda **k: {
        "answer": "答",
        "query_mode": "legacy",
        "reranked_docs": [{"chunk_id": "c1", "text": "正文"}],
        "rrf_chunks": [{"chunk_id": "c1"}, {"chunk_id": "c2"}],
        "manual_rerank_input_chunk_ids": ["c1", "c2"],
        "local_rerank_input_chunk_ids": ["c2", "c1"],
        "reranked_chunk_ids": ["c2", "c1"],
        "selected_chunk_ids": ["c1"],
        "answer_contexts": ["章节背景：父章节", "联网证据片段"],
        "prompt": "完整提示词和历史不属于评测上下文",
    })
    app = FastAPI()
    app.include_router(api.router)
    request = {"query": "问", "session_id": "session-b"}
    with TestClient(app) as client:
        response = client.post("/api/query", json={**request, "include_evidence": True})
        assert response.status_code == 200
        result = response.json()
        ordinary = client.post("/api/query", json=request).json()
        stream = client.post("/api/query", json={**request, "include_evidence": True, "is_stream": True}).json()
    assert result["evidence"] == [{
        "chunk_id": "c1", "content": "正文", "score": None, "source": "kb", "title": "",
        "file_title": "", "item_name": "", "page": None, "heading_path": "", "document_version": "",
    }]
    assert result["retrieval_layers"] == {
        "embedding": [], "rrf": ["c1", "c2"], "manual_rerank_input": ["c1", "c2"],
        "local_rerank_input": ["c2", "c1"], "reranked": ["c2", "c1"], "selected": ["c1"], "final": ["c1"],
    }
    assert result["evaluation"] == {
        "query_mode": "legacy", "outcome": "answered", "contexts": ["章节背景：父章节", "联网证据片段"],
    }
    assert "evaluation" not in ordinary and ordinary["retrieval_layers"] == {}
    assert "evaluation" not in stream and "retrieval_layers" not in stream


@pytest.mark.parametrize(('intent', 'prepared', 'expected'), [
    ('chitchat', {}, 'chitchat'),
    ('chitchat', {'query_outcome': 'out_of_scope'}, 'out_of_scope'),
    ('rag', {'item_name_options': [{'item_name': '可能的主体'}]}, 'clarification'),
    ('rag', {'handoff_required': True}, 'handoff'),
])
def test_http_evaluation_reports_no_retrieval_without_guessing_agentic(monkeypatch, intent, prepared, expected):
    from app.api.routers import query as api
    from app.api.schema.query_schema import QueryRequestSchema
    from app.process.query.agent.state import create_query_default_state

    monkeypatch.setattr(api, "authorize_session", lambda *a, **k: None)
    monkeypatch.setattr(api, "reserve_query", lambda *a: "turn-b")
    monkeypatch.setattr(api, "get_done_task_list", lambda *a: [])
    state = create_query_default_state(intent=intent, answer="已回应", **prepared)
    monkeypatch.setattr(api, "invoke_query_graph", lambda **k: state)
    response = api.query(BackgroundTasks(), QueryRequestSchema(query="问", include_evidence=True))
    assert response.retrieval_layers == {}
    assert response.model_dump()["evaluation"] == {
        "query_mode": "none", "outcome": expected, "contexts": [],
    }


@pytest.mark.parametrize('policy_only', [False, True])
def test_legacy_graph_reports_the_rerank_snapshot_and_model_contexts(monkeypatch, policy_only):
    from app.process.query.agent import main_graph
    from app.process.query.agent.nodes import node_parent_expand, node_rerank, node_search_embedding, node_web_search_mcp
    from app.process.query.agent.state import create_query_default_state
    from app.rag.query import answer_service, hyde_search_service, intent_route_service
    from app.rag.query.eval_evidence import build_evaluation, build_retrieval_layers

    question = 'HAK 180 保修期多久？' if policy_only else 'HAK 180 功率多少？'
    manual = {'chunk_id': 'manual', 'text': '产品正文', 'document_kind': 'manual'}
    policy = {'chunk_id': 'policy', 'text': '有效政策正文', 'document_kind': 'policy',
              'document_version': 'v1', 'valid_until': 9999999999}
    web = {'type': 'web', 'text': '联网证据正文'}
    sent_prompts = []

    monkeypatch.setattr(main_graph.settings.agentic, 'mode', 'legacy')
    monkeypatch.setattr(main_graph.settings.evolution, 'enabled', False)
    monkeypatch.setattr(intent_route_service, 'CHITCHAT_ENABLED', False)
    monkeypatch.setattr('app.process.query.agent.nodes.node_item_name_confirm.confirm_item_name',
                        lambda state: {**state, 'item_names': ['HAK 180'], 'rewritten_query': question})
    monkeypatch.setattr(node_search_embedding.llm_providers, 'embed_text', lambda query: {})
    monkeypatch.setattr(node_search_embedding, 'search_by_embedding', lambda *a, **k: [manual.copy(), policy.copy()])
    monkeypatch.setattr(hyde_search_service, 'HYDE_GATE_ENABLED', not policy_only)
    monkeypatch.setattr(hyde_search_service, 'HYDE_GATE_SHORT_CHARS', 100)
    monkeypatch.setattr(hyde_search_service, 'call_llm_answer', lambda question: '假设性答案')
    monkeypatch.setattr(hyde_search_service, 'search_chunks', lambda *a, **kw: [])
    monkeypatch.setattr(node_web_search_mcp, 'search_by_web', lambda state: [])
    monkeypatch.setattr(node_rerank, 'rerank_documents',
                        lambda state: {**state, 'reranked_chunk_ids': ['manual', 'policy', 'trimmed'],
                                       'reranked_docs': [manual, policy, web]})
    monkeypatch.setattr(node_parent_expand, 'expand_docs_with_parents',
                        lambda docs: [{**doc, 'parent_content': '父章节正文'} if doc is manual else doc for doc in docs])
    monkeypatch.setattr(answer_service, 'build_history_context', lambda *a, **k: '私有历史')
    monkeypatch.setattr(answer_service, 'save_answer_message_history', lambda state: None)

    def answer(messages):
        sent_prompts.append(messages[0].content)
        return SimpleNamespace(content='回答。')

    monkeypatch.setattr(answer_service.llm_providers, 'chat', lambda: SimpleNamespace(invoke=answer))
    result = main_graph.query_app.invoke(create_query_default_state(session_id='test-legacy-layers', original_query=question))
    expected_layers = {'embedding': ['manual', 'policy'], 'rrf': ['manual', 'policy'],
                       'reranked': ['manual', 'policy', 'trimmed'], 'selected': ['manual', 'policy'],
                       'final': ['policy'] if policy_only else ['manual', 'policy']}
    if policy_only:
        expected_layers['hyde'] = []
    assert build_retrieval_layers(result) == expected_layers
    evaluation = build_evaluation(result)
    assert evaluation['query_mode'] == 'legacy' and evaluation['outcome'] == 'answered'
    assert all(fragment in sent_prompts[0] and '私有历史' not in fragment for fragment in evaluation['contexts'])
    if policy_only:
        assert len(evaluation['contexts']) == 1 and '有效政策正文' in evaluation['contexts'][0]
    else:
        assert '父章节正文' in evaluation['contexts'][0] and '联网证据正文' in evaluation['contexts'][-1]


def test_sse_keeps_null_answer_id_and_finishes_once(monkeypatch):
    from app.rag.query import pipeline
    from app.shared.config import settings
    from app.shared.utils.sse_broker import SSEEvent
    monkeypatch.setattr(settings.agentic, "mode", "legacy")
    monkeypatch.setattr(settings.evolution, "write_enabled", False)
    monkeypatch.setattr(pipeline, "query_app", SimpleNamespace(invoke=lambda state: {"answer": "答", "answer_id": None}))
    monkeypatch.setattr(pipeline, "register_unresolved", lambda state: "")
    monkeypatch.setattr(pipeline, "record_query", lambda *a, **kw: None)
    monkeypatch.setattr(pipeline.QueryMetrics, "document", lambda *a: {})
    monkeypatch.setattr(pipeline, "update_task_status", lambda *a, **k: None)
    events, released = [], []
    monkeypatch.setattr(pipeline, "publish", lambda session, kind, payload: events.append((kind, payload)))
    monkeypatch.setattr(pipeline, "release_query", lambda *args: released.append(args))
    assert pipeline.invoke_query_graph("session-a", "问", False, turn_id="turn-a") is not None
    assert events == [
        (SSEEvent.FINAL, {**COMMON, "answer_id": None, "turn_id": "turn-a", "status": "completed"}),
        (SSEEvent.CLOSE, {}),
    ]
    assert released == [("session-a", "turn-a")]
