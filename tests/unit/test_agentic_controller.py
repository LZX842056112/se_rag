import pytest

from app.rag.agentic.budget import Budget, StopRun, selected_mode
from app.rag.agentic.controller import run_agentic


def test_budget_counts_first_call_and_uses_shared_deadline():
    clock = [0.0]
    budget = Budget(max_calls=1, seconds=2, clock=lambda: clock[0])
    budget.take('semantic_search', {'query': 'q'})
    with pytest.raises(StopRun, match='tool_budget'):
        budget.take('read_chunk', {'chunk_ids': ['a']})
    clock[0] = 3
    with pytest.raises(StopRun, match='deadline'):
        budget.remaining()


def test_repeated_action_stops_and_rollout_is_stable():
    budget = Budget()
    budget.take('semantic_search', {'query': 'q'})
    with pytest.raises(StopRun, match='repeated_action'):
        budget.take('semantic_search', {'query': 'q'})
    assert selected_mode('abc', mode='legacy', percent=100) == 'legacy'
    assert selected_mode('abc', mode='agentic', percent=0) == 'legacy'
    assert selected_mode('abc', mode='agentic', percent=100) == 'agentic'


def test_token_budget_uses_conservative_fallback():
    budget = Budget(max_tokens=3)
    with pytest.raises(StopRun, match='context_budget'):
        budget.consume('中文')
    assert budget.counting == 'utf8_bytes_upper_bound'


class Tools:
    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = []

    def execute(self, name, arguments, timeout_seconds):
        self.calls.append((name, arguments, timeout_seconds))
        return {'status': 'ok', 'evidence': self.evidence}


def evidence():
    return {'evidence_id': 'a', 'chunk_id': 'a', 'doc_id': 'd', 'source': 'kb',
            'item_name': 'HAK180', 'text': '工作温度为10到32度。', 'title': '温度',
            'content_sha256': 'hash', 'document_version': 'v1'}


def test_supported_draft_only_published_after_verification():
    tools = Tools([evidence()])
    seen = []
    def judge(kind, payload, timeout):
        seen.append(kind)
        if kind == 'assess':
            return {'coverage': 'supported', 'evidence_ids': ['a']}
        if kind == 'draft':
            return {'answer': '工作温度为10到32度。'}
        if kind == 'verify':
            return {'supported': True, 'evidence_ids': ['a']}
        raise AssertionError(kind)
    state = {'item_names': ['HAK180'], 'rewritten_query': '温度是多少', 'original_query': '温度是多少'}
    result = run_agentic(state, tools=tools, model=judge, persist=False)
    assert result['answer'] == '工作温度为10到32度。'
    assert result['agentic_stop_reason'] == 'answered'
    assert seen == ['assess', 'draft', 'verify']
    assert len(tools.calls) == 1
    assert result['answer_contexts'] and result['evidence_manifest']


def test_successful_agentic_query_is_not_a_no_retrieval_signal():
    from app.rag.query import answer_service
    state = {'query_mode': 'agentic', 'agentic_verified': True,
             'reranked_docs': [{'chunk_id': 'a', 'text': 'evidence'}],
             'agentic_trace': [{'tool': 'semantic_search', 'status': 'ok', 'evidence_ids': ['a']}]}
    answer_service.backfill_evolution_outputs(state)
    assert state['retrieval_signals']['no_retrieval'] is False
    assert state['retrieval_signals']['zero_hit'] is False


def test_model_payload_omits_unbudgeted_backend_metadata():
    row = evidence()
    row['metadata'] = {'debug_blob': 'not-model-context' * 1000, 'read': True}
    seen = []
    def model(kind, payload, timeout):
        if kind == 'assess':
            seen.append(payload['evidence'][0])
            return {'coverage': 'missing', 'evidence_ids': []}
        return {'action': 'handoff'}
    result = run_agentic({'item_names': ['HAK180'], 'rewritten_query': 'q'},
                         tools=Tools([row]), model=model, persist=False)
    assert result['agentic_metrics']['tool_output_tokens'] < 1000
    assert result['agentic_stop_reason'] == 'handoff'
    assert 'metadata' not in seen[0]
    assert 'debug_blob' not in seen[0]


@pytest.mark.parametrize('verdict', [False, None, 'yes'])
def test_invalid_or_unsupported_answer_is_not_released(verdict):
    def judge(kind, payload, timeout):
        if kind == 'assess': return {'coverage': 'supported', 'evidence_ids': ['a']}
        if kind == 'draft': return {'answer': '错误事实'}
        if kind == 'verify': return {'supported': verdict, 'evidence_ids': ['a']}
        return {'action': 'handoff'}
    result = run_agentic({'item_names': ['HAK180'], 'rewritten_query': '温度'},
                         tools=Tools([evidence()]), model=judge, persist=False)
    assert result['answer'] != '错误事实'
    assert result['handoff_required'] is True
    assert result['answer_contexts'] == []


def test_empty_evidence_and_invalid_actions_terminate():
    calls = []
    def model(kind, payload, timeout):
        calls.append(kind)
        if kind == 'assess': return {'coverage': 'missing', 'evidence_ids': []}
        return {'action': 'delete_database'}
    result = run_agentic({'item_names': ['HAK180'], 'rewritten_query': 'q'},
                         tools=Tools([]), model=model, persist=False)
    assert calls.count('select') == 2
    assert result['agentic_stop_reason'] == 'invalid_action'
    assert result['handoff_required'] is True


def test_grounding_consumes_same_parent_context(monkeypatch):
    from app.rag.query import answer_service as svc
    monkeypatch.setattr(svc.settings.evolution, 'enabled', True)
    captured = []
    monkeypatch.setattr(svc, 'compute_groundedness', lambda answer, evidence: captured.extend(evidence) or 1.0)
    state = {'reranked_docs': [{'chunk_id': 'a', 'text': '子块'}],
             'answer': '父章节事实', 'answer_contexts': ['实际父章节上下文']}
    svc.backfill_evolution_outputs(state)
    assert captured == ['实际父章节上下文']


def test_complaint_does_not_call_tools_or_models():
    def forbidden(*args): raise AssertionError('external call')
    tools = Tools([])
    result = run_agentic({'original_query': '我要投诉，一直没解决'}, tools=tools,
                         model=forbidden, persist=False)
    assert tools.calls == []
    assert result['handoff_required']
    assert result['agentic_stop_reason'] == 'business_policy'


def test_invalid_evidence_ids_do_not_allow_answer():
    def model(kind, payload, timeout):
        if kind == 'assess': return {'coverage': 'supported', 'evidence_ids': ['forged']}
        return {'action': 'answer'}
    result = run_agentic({'item_names': ['HAK180'], 'rewritten_query': 'q'},
                         tools=Tools([evidence()]), model=model, persist=False)
    assert not result['agentic_verified']
    assert result['handoff_required']


def test_empty_tools_stop_after_two_attempts():
    def model(kind, payload, timeout):
        if kind == 'assess': return {'coverage': 'missing', 'evidence_ids': []}
        return {'action': 'search', 'tool': 'keyword_search', 'arguments': {'terms': ['q']}}
    tools = Tools([])
    result = run_agentic({'item_names': ['HAK180'], 'rewritten_query': 'q'},
                         tools=tools, model=model, persist=False)
    assert len(tools.calls) == 2
    assert result['agentic_stop_reason'] == 'no_new_evidence'


def test_expired_request_cannot_emit_a_draft():
    clock = [0]
    budget = Budget(clock=lambda: clock[0], seconds=1)
    class SlowTools(Tools):
        def execute(self, *args):
            clock[0] = 2
            return super().execute(*args)
    result = run_agentic({'item_names': ['HAK180'], 'rewritten_query': 'q'},
                         tools=SlowTools([evidence()]), model=lambda *args: pytest.fail(),
                         budget=budget, persist=False)
    assert result['agentic_stop_reason'] == 'deadline'
    assert result['answer_contexts'] == []


def test_cancelled_model_releases_no_unverified_draft():
    import asyncio
    def cancelled(*args): raise asyncio.CancelledError()
    result = run_agentic({'item_names': ['HAK180'], 'rewritten_query': 'q'},
                         tools=Tools([evidence()]), model=cancelled, persist=False)
    assert result['agentic_stop_reason'] == 'cancelled'
    assert result['handoff_required']


def test_stale_evidence_before_draft_is_rejected():
    class StaleTools(Tools):
        def revalidate(self, evidence, timeout): return []
    result = run_agentic({'item_names': ['HAK180'], 'rewritten_query': 'q'},
        tools=StaleTools([evidence()]),
        model=lambda *args: {'coverage': 'supported', 'evidence_ids': ['a']}, persist=False)
    assert result['agentic_stop_reason'] == 'stale_evidence'


def test_mode_switch_preserves_legacy_route(monkeypatch):
    from app.process.query.agent import main_graph
    monkeypatch.setattr(main_graph.settings.agentic, 'mode', 'agentic')
    monkeypatch.setattr(main_graph.settings.agentic, 'gray_percent', 100)
    assert main_graph.after_node_item_name_confirm({'session_id': 's'}) == 'node_agentic'
    assert main_graph.after_node_item_name_confirm({'answer': '请选择产品'}) == 'node_answer_output'
    monkeypatch.setattr(main_graph.settings.agentic, 'mode', 'legacy')
    assert len(main_graph.after_node_item_name_confirm({})) == 3


def test_catalog_rpcs_share_the_request_deadline(monkeypatch):
    from types import SimpleNamespace
    from app.rag.agentic import deadline
    from app.rag.item_name import catalog
    monkeypatch.setattr(catalog, '_CACHE', {'names': [], 'raws': [], 'ts': 0.0})
    clock, timeouts = [0.0], []
    def rpc(**kwargs):
        timeouts.append(kwargs['timeout'])
        clock[0] += 1
        return True
    client = SimpleNamespace(has_collection=rpc, load_collection=rpc,
        query=lambda **kwargs: rpc(**kwargs) and [{'item_name': 'A'}])
    monkeypatch.setattr(catalog, 'milvus_gateway', SimpleNamespace(milvus_client=client, item_name_collection_name='items'))
    monkeypatch.setattr(catalog.settings.milvus, 'timeout_seconds', 30)
    monkeypatch.setattr(deadline.time, 'monotonic', lambda: clock[0])
    token = deadline.query_deadline.set(3.0)
    try:
        catalog.load_item_names(force=True)
    finally:
        deadline.query_deadline.reset(token)
    assert timeouts == [3.0, 2.0, 1.0]


def test_independent_write_flag_starts_publication_worker(monkeypatch):
    import asyncio
    from app.api.http import query_server
    from app.shared.config import settings
    started = []
    class Lease:
        def acquire(self): pass
        def release(self): pass
    async def worker(interval):
        started.append(interval)
        await asyncio.Event().wait()
    monkeypatch.setattr(query_server, 'SingleWorkerLease', Lease)
    monkeypatch.setattr(query_server, 'ensure_indexes', lambda: None)
    monkeypatch.setattr(query_server, 'evolution_scheduler_loop', worker)
    monkeypatch.setattr(settings.evolution, 'enabled', False)
    monkeypatch.setattr(settings.evolution, 'write_enabled', True)
    monkeypatch.setattr(settings.evolution, 'scheduler_enabled', True)
    async def check():
        async with query_server.lifespan(None):
            await asyncio.sleep(0)
            assert started
    asyncio.run(check())
