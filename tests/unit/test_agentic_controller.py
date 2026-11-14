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


@pytest.mark.parametrize(('hint', 'expected_tag'), [
    ('转印失败怎么排查？什么情况下需要联系售后？', 'troubleshooting'),
    ('转印失败怎么排查？可以申请保修吗？', 'after_sales'),
    ('转印失败怎么排查？我要投诉。', 'complaint'),
])
def test_agentic_and_legacy_apply_same_business_rule_to_rewrite_hint(monkeypatch, hint, expected_tag):
    from app.rag.query.business import apply_business_policy

    monkeypatch.setattr('app.rag.query.answer_service.build_history_context', lambda *a, **k: '')
    manual = {'chunk_id': 'a', 'text': '提高转印温度。仍有问题时，请联系经销商。', 'document_kind': 'manual'}
    state = {'original_query': 'HAK 180', 'rewrite_hint': hint, 'rewritten_query': 'HAK 180 售后问题',
             'item_names': ['HAK 180']}
    legacy = {**state, 'reranked_docs': [manual]}
    apply_business_policy(legacy)

    pre_retrieval_tags = []
    class ManualTools(Tools):
        def execute(self, *args):
            pre_retrieval_tags.append(agentic_state['business_tag'])
            return super().execute(*args)

    def judge(kind, payload, timeout):
        if kind == 'assess': return {'coverage': 'supported', 'evidence_ids': ['a']}
        if kind == 'draft': return {'answer': manual['text']}
        return {'supported': True, 'evidence_ids': ['a']}

    agentic_state = state.copy()
    result = run_agentic(agentic_state, tools=ManualTools([{**evidence(), **manual}]), model=judge, persist=False)
    assert legacy['business_tag'] == result['business_tag'] == expected_tag
    if expected_tag == 'complaint':
        assert pre_retrieval_tags == []
    else:
        assert pre_retrieval_tags == [expected_tag]
    assert bool(legacy.get('handoff_required')) == result.get('handoff_required', False) == (expected_tag != 'troubleshooting')


@pytest.mark.parametrize(('draft', 'expected_verified'), [
    ('支持 Telnet 服务器的命令行配置。\n\n现有参考内容与历史对话中未查询到该问题相关信息，无法作答', False),
    ('该型号不支持无线连接。', True),
])
def test_agentic_verifier_does_not_publish_a_draft_with_the_fixed_refusal(monkeypatch, draft, expected_verified):
    from app.shared.config import settings

    monkeypatch.setattr(settings.agentic, 'max_repairs', 0)
    monkeypatch.setattr('app.rag.query.answer_service.build_history_context', lambda *a, **k: '')
    row = {**evidence(), 'text': '支持 Telnet 命令行配置。该型号不支持无线连接。'}

    def judge(kind, payload, timeout):
        if kind == 'assess': return {'coverage': 'supported', 'evidence_ids': ['a']}
        if kind == 'draft': return {'answer': draft}
        return {'supported': True, 'evidence_ids': ['a']}

    result = run_agentic({'item_names': ['打印服务器'], 'rewritten_query': '网络功能'},
                         tools=Tools([row]), model=judge, persist=False)
    assert result['agentic_verified'] is expected_verified
    assert (result['answer'] == draft) is expected_verified


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
    assert result['groundedness'] is None, '二元草稿校验不能伪造数值质量评分'


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


def test_assessment_sees_approved_faq_question_and_publication_metadata():
    row={**evidence(),'source':'evolution','title':'温度多少？','source_version':'2','approval_status':'active'}
    seen=[]
    def model(kind,payload,timeout):
        if kind=='assess':
            seen.append(payload['evidence'][0])
            return {'coverage':'supported','evidence_ids':['a']}
        if kind=='draft':return {'answer':'工作温度为10到32度。'}
        return {'supported':True,'evidence_ids':['a']}
    result=run_agentic({'item_names':['HAK180'],'rewritten_query':'温度多少'},tools=Tools([row]),model=model,persist=False)
    assert result['agentic_stop_reason']=='answered'
    assert seen[0]['title']=='温度多少？'
    assert seen[0]['approval_status']=='active' and seen[0]['source_version']=='2'


def test_assessment_uses_current_evidence_without_previous_coverage_or_action_trace():
    def model(kind,payload,timeout):
        if kind=='assess':
            assert set(payload)=={'question','evidence'}
            return {'coverage':'supported','evidence_ids':['a']}
        if kind=='draft':return {'answer':'工作温度为10到32度。'}
        return {'supported':True,'evidence_ids':['a']}
    result=run_agentic({'item_names':['HAK180'],'rewritten_query':'温度'},tools=Tools([evidence()]),model=model,persist=False)
    assert result['agentic_stop_reason']=='answered'


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


def test_invalid_action_retry_receives_validation_feedback():
    calls = {'assess':0, 'select':0}
    def model(kind, payload, timeout):
        if kind == 'assess':
            calls['assess'] += 1
            return {'coverage':'missing' if calls['assess']==1 else 'supported', 'evidence_ids':['a']}
        if kind == 'select':
            calls['select'] += 1
            if calls['select']==1:
                return {'action':'search', 'tool':'search_approved_faq', 'arguments':{'query':'温度'}, 'reason':'extra'}
            assert payload.get('validation_feedback'), '重试必须知道首次动作的校验错误'
            assert any(error['loc']==['reason'] for error in payload['validation_feedback'])
            return {'action':'search','tool':'search_approved_faq','arguments':{'query':'温度'}}
        if kind == 'draft': return {'answer':'工作温度为10到32度。'}
        if kind == 'verify': return {'supported':True,'evidence_ids':['a']}
        raise AssertionError(kind)
    result=run_agentic({'item_names':['HAK180'],'rewritten_query':'温度'},tools=Tools([evidence()]),model=model,persist=False)
    assert result['agentic_stop_reason']=='answered'
    assert calls['select']==2


def test_invalid_extra_field_cannot_expand_retry_context_without_bound():
    import json
    calls=[]
    def model(kind,payload,timeout):
        if kind=='assess':return {'coverage':'missing','evidence_ids':[]}
        calls.append(payload)
        if len(calls)==1:return {'action':'search','unexpected-'+'x'*100000:1}
        assert len(json.dumps(payload)) < 10000
        return {'action':'handoff'}
    result=run_agentic({'item_names':['HAK180'],'rewritten_query':'q'},tools=Tools([]),model=model,
                       budget=Budget(max_calls=1),persist=False)
    assert result['agentic_stop_reason']=='handoff'
    assert max(len(str(part)) for error in calls[1]['validation_feedback'] for part in error['loc']) <= 80


def test_missing_document_evidence_checks_approved_faq_before_early_clarification():
    class FaqTools(Tools):
        def execute(self, name, arguments, timeout_seconds):
            self.calls.append((name, arguments, timeout_seconds))
            return {'status':'ok' if name=='search_approved_faq' else 'empty',
                    'evidence':[evidence()] if name=='search_approved_faq' else []}
    tools=FaqTools([])
    def model(kind,payload,timeout):
        if kind=='assess':return {'coverage':'supported' if payload['evidence'] else 'missing','evidence_ids':['a'] if payload['evidence'] else []}
        if kind=='select':return {'action':'clarify'}
        if kind=='draft':return {'answer':'工作温度为10到32度。'}
        if kind=='verify':return {'supported':True,'evidence_ids':['a']}
        raise AssertionError(kind)
    result=run_agentic({'item_names':['HAK180'],'original_query':'温度多少'},tools=tools,model=model,persist=False)
    assert result['agentic_stop_reason']=='answered'
    assert [call[0] for call in tools.calls]==['semantic_search','search_approved_faq']
    assert result['agentic_metrics']['tool_calls']==2


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
    assert result['issue_type'] == 'complaint'
    assert result['query_mode'] == 'none'
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
    from app.rag.query.eval_evidence import build_retrieval_layers

    def model(kind, payload, timeout):
        if kind == 'assess': return {'coverage': 'missing', 'evidence_ids': []}
        return {'action': 'search', 'tool': 'keyword_search', 'arguments': {'terms': ['q']}}
    tools = Tools([])
    result = run_agentic({'item_names': ['HAK180'], 'rewritten_query': 'q'},
                         tools=tools, model=model, persist=False)
    assert len(tools.calls) == 2
    assert result['agentic_stop_reason'] == 'no_new_evidence'
    assert build_retrieval_layers(result) == {'agentic_retrieval': [], 'final': []}


def test_agentic_evaluation_keeps_candidate_order_and_actual_answer_contexts(monkeypatch):
    from app.rag.query.eval_evidence import build_evaluation, build_retrieval_layers

    monkeypatch.setattr('app.rag.query.answer_service.build_history_context', lambda *a, **k: '本轮历史')
    first = evidence()
    duplicate = {**first, 'evidence_id': 'a-next-version'}
    web = {**first, 'evidence_id': 'web', 'source': 'web', 'chunk_id': None, 'text': '网页内容'}
    final = {**first, 'evidence_id': 'b', 'chunk_id': 'b', 'text': '父章节内容\n\n命中子块',
             'metadata': {'child_text': '命中子块', 'parent_content': '父章节内容'}}

    class SearchTools:
        def execute(self, tool, arguments, timeout):
            return {'status': 'ok', 'evidence': [first, duplicate, web] if tool == 'semantic_search' else [final, first]}

    sent_contexts = []
    def judge(kind, payload, timeout):
        if kind == 'assess':
            if any(row['evidence_id'] == 'b' for row in payload['evidence']):
                return {'coverage': 'supported', 'evidence_ids': ['b', 'web']}
            return {'coverage': 'missing', 'evidence_ids': []}
        if kind == 'select': return {'action': 'search', 'tool': 'keyword_search', 'arguments': {'terms': ['温度']}}
        if kind == 'draft':
            sent_contexts.extend(payload['answer_contexts'])
            return {'answer': '工作温度为10到32度。'}
        return {'supported': True, 'evidence_ids': ['b', 'web']}

    result = run_agentic({'item_names': ['HAK180'], 'rewritten_query': '温度'},
                         tools=SearchTools(), model=judge, persist=False)
    assert build_retrieval_layers(result) == {'agentic_retrieval': ['a', 'b'], 'final': ['b']}
    evaluation = build_evaluation(result)
    assert evaluation == {'query_mode': 'agentic', 'outcome': 'answered', 'contexts': sent_contexts}
    assert len(sent_contexts) == 2
    assert '父章节内容' in sent_contexts[0] and '命中子块' in sent_contexts[0]
    assert '网页内容' in sent_contexts[1]
    assert all('本轮历史' not in context for context in sent_contexts)


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
    assert main_graph.after_node_item_name_confirm({}) == ("node_search_embedding", "node_search_embedding_hyde")


def test_catalog_rpcs_share_the_request_deadline(monkeypatch):
    from types import SimpleNamespace
    from app.shared.runtime import deadline
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
    # 离线用例不加载真实模型：预热逻辑由 test_query_server_warmup 单独覆盖
    warmed = []
    monkeypatch.setattr(query_server, 'warmup_models', lambda: warmed.append(True))
    monkeypatch.setattr(query_server, 'evolution_scheduler_loop', worker)
    monkeypatch.setattr(settings.evolution, 'enabled', False)
    monkeypatch.setattr(settings.evolution, 'write_enabled', True)
    monkeypatch.setattr(settings.evolution, 'scheduler_enabled', True)
    async def check():
        async with query_server.lifespan(query_server.app):
            await asyncio.sleep(0)
            assert warmed, "启动时必须先预热检索模型，否则首个查询会耗尽 Agentic 预算"
            assert started
    asyncio.run(check())


def test_terminal_action_drops_empty_tool():
    """clarify/handoff 不需要 tool；模型补空字符串时不得判成非法动作。"""
    from app.rag.agentic.controller import Action, _normalize_action

    action = Action.model_validate(_normalize_action({'action': 'Clarify', 'tool': '', 'arguments': {}}))

    assert action.action == 'clarify'


def test_clarify_with_empty_tool_still_reaches_approved_faq():
    """空 tool 不再消耗修复次数，控制器能补查已审批 FAQ 并正常作答。"""
    tools = Tools([evidence()])
    assess_calls = {'n': 0}

    def model(kind, payload, timeout):
        if kind == 'assess':
            assess_calls['n'] += 1
            if assess_calls['n'] == 1:
                return {'coverage': 'missing', 'evidence_ids': []}
            return {'coverage': 'supported', 'evidence_ids': ['a']}
        if kind == 'select':
            return {'action': 'clarify', 'tool': '', 'arguments': {}}
        if kind == 'draft':
            return {'answer': 'HAK180 不支持蓝牙连接。'}
        if kind == 'verify':
            return {'supported': True, 'evidence_ids': ['a']}
        raise AssertionError(kind)

    result = run_agentic({'item_names': ['HAK180'], 'rewritten_query': '怎么连蓝牙'},
                         tools=tools, model=model, persist=False)

    assert result['agentic_stop_reason'] == 'answered'
    assert [name for name, _, _ in tools.calls] == ['semantic_search', 'search_approved_faq']


def test_assess_prompt_accepts_negative_answer(monkeypatch):
    """“不支持某功能”也是答案：评估提示词必须明确允许否定结论判 supported。"""
    from types import SimpleNamespace

    from app.shared.models import llm_providers
    from app.rag.agentic import controller

    captured = {}

    class _Client:
        def invoke(self, messages):
            captured['prompt'] = messages[0].content
            return SimpleNamespace(content='{"coverage":"missing","evidence_ids":[]}')

    monkeypatch.setattr(llm_providers, 'chat', lambda *args, **kwargs: _Client())

    controller._model('assess', {'question': 'hak180烫金机怎么连蓝牙', 'evidence': []}, 10)

    assert '否定结论' in captured['prompt']
    assert 'supported' in captured['prompt']
