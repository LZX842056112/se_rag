"""重排的来源优先级测试。

事故背景：用户审批入库的 FAQ 在重排里只喂了答案文本（“配对码123456”），被 4 条联网结果
（0.999x）挤出上下文，于是客服回答「未查询到」且没有任何引用。
"""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.rag.query import rerank_service
from app.shared.models import llm_providers
from app.shared.runtime.deadline import query_deadline


class _FakeTokenizer:
    def encode(self, text, add_special_tokens=False, truncation=False, max_length=None):
        tokens = str(text or "").split()
        return tokens[:max_length] if (truncation and max_length) else tokens

    def decode(self, tokens):
        return " ".join(tokens)


class _FakeReranker:
    """按预设分数回填，模拟「联网段落得分高、短答案得分低」的真实情况。"""

    tokenizer = _FakeTokenizer()

    def __init__(self, scores):
        self._scores = scores

    def compute_score(self, pairs, normalize=True):
        return list(self._scores)[: len(pairs)]


def _evolution_chunk(evo_id="evo_1", question="HAK 180 的配对码是多少？", answer="配对码123456"):
    return {"chunk_id": evo_id, "title": question, "content": answer, "source": "evolution", "type": "milvus"}


def _kb_chunk(cid=1, text="手册正文"):
    return {"chunk_id": cid, "title": "标题", "content": text, "source": "milvus", "type": "milvus"}


def test_evolution_text_includes_faq_question():
    """进化条目参与重排时应带 FAQ 问题，否则短答案与问句不匹配。"""
    docs = rerank_service.deal_rrf_and_web_result([_evolution_chunk()], [])
    assert "配对码123456" in docs[0]["text"]
    assert "HAK 180 的配对码是多少？" in docs[0]["text"]
    assert docs[0]["source"] == "evolution"


def test_kb_chunk_text_is_content_only():
    docs = rerank_service.deal_rrf_and_web_result([_kb_chunk()], [])
    assert docs[0]["text"] == "手册正文"
    assert docs[0]["type"] == "milvus"


def test_web_cap_only_when_local_hits():
    local = [{"chunk_id": 1, "type": "milvus", "source": "milvus"}, {"chunk_id": 2, "type": "milvus"}]
    web = [{"chunk_id": None, "type": "web", "source": "web"} for _ in range(5)]
    docs = local + web

    capped = rerank_service.cap_web_docs(docs, limit=2)
    assert len([d for d in capped if d["type"] == "web"]) == 2
    assert len([d for d in capped if d["type"] != "web"]) == 2

    # 本地无命中时不限制联网（纯联网问答仍可用）
    assert len(rerank_service.cap_web_docs(web, limit=2)) == 5


def test_web_docs_cannot_outrank_local_hits():
    """回归：联网片段分数再高，也不允许排在本地命中之前。"""
    docs = [
        {"chunk_id": 1, "type": "milvus", "source": "milvus", "score": 0.42},
        {"chunk_id": None, "type": "web", "source": "web", "score": 0.999, "url": "https://w/1"},
        {"chunk_id": None, "type": "web", "source": "web", "score": 0.998, "url": "https://w/2"},
    ]
    ranked = rerank_service.prefer_local_docs(list(docs))
    assert ranked[0]["chunk_id"] == 1, "本地知识必须排在联网补充之前"
    assert [d["score"] for d in ranked[1:]] == [0.42, 0.42], "联网分被压到本地最高分"


def test_pure_web_answering_keeps_scores():
    """本地零命中时联网结果保持原分（纯联网问答仍可用）。"""
    docs = [
        {"chunk_id": None, "type": "web", "source": "web", "score": 0.5, "url": "https://w/1"},
        {"chunk_id": None, "type": "web", "source": "web", "score": 0.9, "url": "https://w/2"},
    ]
    ranked = rerank_service.prefer_local_docs(list(docs))
    assert [d["score"] for d in ranked] == [0.9, 0.5]


def test_local_wins_ties_against_web():
    """同分时本地排前（不依赖排序稳定性）。"""
    docs = [
        {"chunk_id": None, "type": "web", "source": "web", "score": 0.7, "url": "https://w/1"},
        {"chunk_id": 9, "type": "milvus", "source": "milvus", "score": 0.7},
    ]
    ranked = rerank_service.prefer_local_docs(list(docs))
    assert ranked[0]["chunk_id"] == 9


def test_web_supplement_does_not_consume_minimum_local_evidence_slots():
    docs = [
        {'chunk_id': 'temperature-setting', 'type': 'milvus', 'score': .43},
        {'chunk_id': None, 'type': 'web', 'score': .43},
        {'chunk_id': 'environment-facts', 'type': 'milvus', 'score': .118},
        {'chunk_id': 'supported-media', 'type': 'milvus', 'score': .05},
        {'chunk_id': 'temperature-range', 'type': 'milvus', 'score': .02},
        {'chunk_id': 'adjustment-steps', 'type': 'milvus', 'score': .01},
        {'chunk_id': 'unrelated', 'type': 'milvus', 'score': .001},
    ]
    selected = rerank_service.dyn_limit_reranker_docs(docs)
    assert [doc['chunk_id'] for doc in selected] == [
        'temperature-setting', None, 'environment-facts', 'supported-media',
        'temperature-range', 'adjustment-steps',
    ]


def test_small_total_cap_reserves_its_slots_for_minimum_local_evidence(monkeypatch):
    monkeypatch.setattr(rerank_service, 'get_rerank_topk', lambda: 2)
    docs = [
        {'chunk_id':'local-a','type':'milvus','score':.43},
        {'chunk_id':None,'type':'web','score':.43},
        {'chunk_id':None,'type':'web','score':.43},
        {'chunk_id':'local-b','type':'milvus','score':.118},
    ]
    assert [d['chunk_id'] for d in rerank_service.dyn_limit_reranker_docs(docs)] == ['local-a','local-b']


def test_evolution_authority_survives_truncation(monkeypatch):
    """权威 FAQ 即使重排分数被压低，也必须留在最终上下文里。"""
    evolution = _evolution_chunk()
    local = [_kb_chunk(1, "手册正文一"), _kb_chunk(2, "手册正文二")]
    web = [{"title": f"网页{i}", "snippet": f"联网片段{i}", "url": f"http://w/{i}"} for i in range(4)]
    # 打分顺序：联网 4 条 >> 本地 2 条 >> 进化 FAQ（模拟真实事故）
    monkeypatch.setattr(llm_providers, "reranker_model",
                        lambda: _FakeReranker([0.999, 0.998, 0.997, 0.996, 0.30, 0.20, 0.10]))
    monkeypatch.setattr(llm_providers, "chat", lambda *a, **k: None)

    state = {
        "rewritten_query": "HAK 180 的配对码是多少？",
        "rrf_chunks": [evolution] + local,
        "web_search_docs": web,
        "is_stream": False,
    }
    result = rerank_service.rerank_documents(state)
    ids = [d.get("chunk_id") for d in result["reranked_docs"]]

    assert "evo_1" in ids, f"权威 FAQ 被截断丢弃：{ids}"
    assert ids[0] == "evo_1", "权威条目应置于上下文最前，供作答优先使用"
    assert any(d.get("type") == "web" for d in result["reranked_docs"]), "联网保留少量补充"


def test_expanded_manual_candidates_use_the_models_evidence_order(monkeypatch):
    def chat(**kwargs):
        assert kwargs['json_mode'] and kwargs['max_retries'] == 0
        return SimpleNamespace(invoke=lambda messages: SimpleNamespace(content='{"ranks":[6,2,1,4,5]}'))

    monkeypatch.setattr(llm_providers, 'chat', chat)
    monkeypatch.setattr(llm_providers, 'reranker_model', lambda: pytest.fail('Listwise ranking already selected the evidence'))
    monkeypatch.setattr(rerank_service, 'get_rerank_topk', lambda: 6)
    state = {'original_query': '怎么配置设备？', 'rewritten_query': '产品怎么配置？', 'item_names': ['产品'],
             'rrf_chunks': [_kb_chunk(str(i)) for i in range(1, 8)],
             'web_search_docs': [{'title': '补充', 'snippet': '网页正文', 'url': 'http://w/1'}]}
    result = rerank_service.rerank_documents(state)
    assert result['reranked_chunk_ids'] == ['6', '2', '1', '4', '5']
    assert result['manual_rerank_input_chunk_ids'] == [str(i) for i in range(1, 8)]
    assert [doc['chunk_id'] for doc in result['reranked_docs']] == ['6', '2', '1', '4', '5', None]
    assert all(doc['score'] is None for doc in result['reranked_docs'])
    assert result['reranked_docs'][0]['text'] == '手册正文'


def test_invalid_manual_ranks_use_bounded_bge_fallback(monkeypatch):
    fake = _FakeReranker([.9] * 7)
    scored = []

    def score(pairs, normalize=True):
        scored.extend(pairs)
        return [.9] * len(pairs)

    fake.compute_score = score
    monkeypatch.setattr(llm_providers, 'chat', lambda **kwargs: SimpleNamespace(
        invoke=lambda messages: SimpleNamespace(content='{"ranks":[1,1,1,1,1]}')))
    monkeypatch.setattr(llm_providers, 'reranker_model', lambda: fake)
    monkeypatch.setattr(rerank_service, 'get_rrf_top', lambda: 5)
    state = {'rewritten_query': '产品配置', 'rrf_chunks': [_kb_chunk(str(i)) for i in range(14)], 'web_search_docs': []}
    result = rerank_service.rerank_documents(state)
    assert len(scored) == 7
    assert result['reranked_chunk_ids'] == ['0', '1', '2', '3', '4', '5', '6']


@pytest.mark.parametrize(("named_section", "last_index"), [(False, 6), (True, 4)])
def test_manual_fallback_uses_fusion_and_keeps_requested_section(monkeypatch, named_section, last_index):
    candidates = [_kb_chunk(str(i)) for i in range(8)]
    if named_section:
        candidates[4]['heading_path'] = '设备设置 / 电源上电策略'
    monkeypatch.setattr(llm_providers, 'chat', lambda **kwargs: SimpleNamespace(
        invoke=lambda messages: SimpleNamespace(content='{"ranks":[1,1,1,1,1]}')))
    monkeypatch.setattr(llm_providers, 'reranker_model', lambda: pytest.fail('Fusion can provide local evidence'))
    state = {'rewritten_query': '使用电源上电策略后为何没有开机？', 'rrf_chunks': candidates, 'web_search_docs': [],
             'embedding_chunks': candidates[:5], 'keyword_chunks': [candidates[6]]}
    result = rerank_service.rerank_documents(state)
    ids = [doc['chunk_id'] for doc in result['reranked_docs']]
    assert ids == ['0', '1', '2', '3', str(last_index)]
    assert not result.get('local_rerank_input_chunk_ids')


def test_manual_rerank_reserves_answer_time(monkeypatch):
    timeouts = []

    def chat(**kwargs):
        timeouts.append(kwargs['timeout_seconds'])
        return SimpleNamespace(invoke=lambda messages: SimpleNamespace(content='{"ranks":[1,2,3,4,5]}'))

    monkeypatch.setattr(llm_providers, 'chat', chat)
    state = {'rewritten_query': '产品配置', 'rrf_chunks': [_kb_chunk(str(i)) for i in range(8)], 'web_search_docs': []}
    token = query_deadline.set(time.monotonic() + 15)
    try:
        rerank_service.rerank_documents(state)
    finally:
        query_deadline.reset(token)
    assert len(timeouts) == 1 and 0 < timeouts[0] <= 3


def test_low_remaining_budget_uses_fusion_evidence_without_reranking(monkeypatch):
    monkeypatch.setattr(llm_providers, 'chat', lambda **kwargs: pytest.fail('Manual rerank consumed answer time'))
    monkeypatch.setattr(llm_providers, 'reranker_model', lambda: pytest.fail('Local rerank consumed answer time'))
    state = {'rewritten_query': '产品配置', 'rrf_chunks': [_kb_chunk(str(i)) for i in range(8)], 'web_search_docs': []}
    token = query_deadline.set(time.monotonic() + 6)
    try:
        result = rerank_service.rerank_documents(state)
    finally:
        query_deadline.reset(token)
    assert [doc['chunk_id'] for doc in result['reranked_docs']] == ['0', '1', '2', '3', '4']
    assert not result.get('manual_rerank_input_chunk_ids')
    assert not result.get('local_rerank_input_chunk_ids')


def test_manual_fallback_keeps_the_candidate_that_all_retrieval_branches_rank_first(monkeypatch):
    candidates = [_kb_chunk(str(i)) for i in range(8)]
    monkeypatch.setattr(llm_providers, 'chat', lambda **kwargs: SimpleNamespace(
        invoke=lambda messages: SimpleNamespace(content='{"ranks":[1,1,1,1,1]}')))
    monkeypatch.setattr(llm_providers, 'reranker_model',
                        lambda: pytest.fail('Fusion can provide local evidence'))
    monkeypatch.setattr(rerank_service, 'get_rrf_top', lambda: 5)
    state = {'rewritten_query': '怎么配置设备？', 'rrf_chunks': candidates, 'web_search_docs': [],
             'embedding_chunks': candidates[:1], 'keyword_chunks': candidates[:1],
             'hyde_embedding_chunks': candidates[:1]}
    result = rerank_service.rerank_documents(state)
    assert not result.get('reranked_chunk_ids')
    assert '0' in [doc['chunk_id'] for doc in result['reranked_docs'][:5]]


def test_manual_selection_keeps_the_fusion_leader_supported_by_three_branches():
    from app.rag.query.manual_evidence_rerank import keep_retrieval_agreement

    rows = [{"chunk_id": str(i), "type": "milvus"} for i in range(7)]
    state = {
        "rrf_chunks": rows,
        "embedding_chunks": rows[:3],
        "keyword_chunks": [rows[2], rows[1], rows[0]],
        "hyde_embedding_chunks": [rows[1], rows[0], rows[2]],
    }
    ranked = rows[1:6]
    kept = keep_retrieval_agreement(state, ranked, rows, 5)
    assert [row["chunk_id"] for row in kept] == ["1", "2", "3", "4", "0"]


@pytest.mark.parametrize("has_hyde", [True, False])
def test_manual_selection_keeps_the_semantic_leader_with_keyword_support(has_hyde):
    from app.rag.query.manual_evidence_rerank import keep_retrieval_agreement

    rows = [{"chunk_id": str(i), "type": "milvus"} for i in range(7)]
    state = {
        "rrf_chunks": rows,
        "embedding_chunks": rows[:5],
        "keyword_chunks": rows[1:4] + rows[:1] + rows[4:],
        "hyde_embedding_chunks": rows[:5] if has_hyde else [],
    }
    kept = keep_retrieval_agreement(state, rows[1:6], rows, 5)
    assert [row["chunk_id"] for row in kept] == ["1", "2", "3", "4", "0"]


def test_explicit_protocol_section_survives_a_rerank_for_another_installation_path():
    from app.rag.query.manual_evidence_rerank import keep_retrieval_agreement

    generic = [{"chunk_id": str(i), "type": "milvus"} for i in range(5)]
    protocol = {"chunk_id": "protocol", "type": "milvus", "heading_path": "打印手册 / 网络服务"}
    state = {
        "original_query": "如何通过网络服务为打印机安装驱动？",
        "embedding_chunks": generic[:3] + [protocol],
        "keyword_chunks": [protocol],
        "hyde_embedding_chunks": generic,
        "rrf_chunks": generic + [protocol],
    }
    selected = keep_retrieval_agreement(state, generic, state["rrf_chunks"], 5)
    assert [row["chunk_id"] for row in selected] == ["protocol", "0", "1", "2", "3"]


def test_selected_steps_keep_the_adjacent_note_from_the_same_manual_version(monkeypatch):
    import json

    def chunk(cid, page, seq, heading, version='v1'):
        return dict(_kb_chunk(cid, cid), doc_id='manual', page=page, seq=seq,
                    heading_path=heading, document_version=version)

    docs = [
        chunk('step', 8, 20, '设备设置 / 打开页面'),
        chunk('other', 11, 31, '附录 / 其它设置'),
        chunk('wrong-version', 8, 21, '设备设置 / 注释', 'v2'),
        chunk('start', 8, 19, '设备设置 / 前置条件'),
        chunk('note', 8, 21, '设备设置 / 注释'),
        chunk('extra', 12, 40, '附录 / 术语'),
        chunk('last', 13, 44, '附录 / 操作系统'),
    ]

    def rank(messages):
        passages = json.loads(messages[0].content.rsplit('\n\n', 1)[-1])['passages']
        assert [doc['text'] for doc in passages[:3]] == ['start', 'step', 'note']
        numbers = {doc['text']: doc['number'] for doc in passages}
        order = [numbers[name] for name in ('step', 'start', 'other', 'extra', 'last')]
        return SimpleNamespace(content=json.dumps({'ranks': order}))

    monkeypatch.setattr(llm_providers, 'chat', lambda **kwargs: SimpleNamespace(invoke=rank))
    state = {'original_query': '怎么打开设备设置页面？', 'rewritten_query': '怎么打开设备设置页面？',
             'rrf_chunks': docs, 'web_search_docs': []}
    result = rerank_service.rerank_documents(state)
    assert result['reranked_chunk_ids'] == ['step', 'start', 'other', 'extra', 'last']
    assert [doc['chunk_id'] for doc in result['reranked_docs']] == ['step', 'start', 'other', 'extra', 'note']


def test_selected_procedure_fills_the_gap_between_steps_on_one_page():
    from app.rag.query.manual_evidence_rerank import keep_procedure_context

    steps = [dict(chunk_id=str(i), doc_id='manual', document_version='v1', page=8,
                  heading_path='使用说明书 / 安装配件', seq=i) for i in range(1, 6)]
    other = dict(chunk_id='other-mode', doc_id='manual', document_version='v1', page=6,
                 heading_path='使用说明书 / 安装其它配件', seq=11)
    ranked = [steps[3], steps[0], other, steps[1], steps[4]]
    result = keep_procedure_context('怎样安装配件？', ranked, steps + [other])
    assert [doc['chunk_id'] for doc in result] == ['1', '2', '3', '4', '5']


def test_cpu_reranker_scores_each_batch_once_and_keeps_pair_order(monkeypatch):
    import torch

    class Tokenizer:
        def __call__(self, texts, *, add_special_tokens, max_length, truncation):
            assert not add_special_tokens and truncation
            return {"input_ids": [[int(token) for token in text.split()][:max_length] for text in texts]}

        def prepare_for_model(self, query, passage, *, truncation, max_length, padding):
            assert truncation == "only_second" and not padding
            return {"input_ids": (query + passage)[:max_length]}

        def pad(self, rows, *, padding, return_tensors):
            assert padding and return_tensors == "pt"
            width = max(len(row["input_ids"]) for row in rows)
            return {"input_ids": torch.tensor([row["input_ids"] + [0] * (width - len(row["input_ids"])) for row in rows])}

    batches = []

    def model(input_ids, *, return_dict):
        assert return_dict and torch.is_inference_mode_enabled()
        batches.append(tuple(input_ids.shape))
        return SimpleNamespace(logits=input_ids[:, 1:2].float())

    def upstream_score(*args, **kwargs):
        pytest.fail("CPU ranking called the upstream probe and score loop")

    reranker = SimpleNamespace(target_devices=["cpu"], tokenizer=Tokenizer(), model=model,
                               batch_size=2, query_max_length=1, max_length=4, compute_score=upstream_score)
    monkeypatch.setattr(llm_providers, "reranker_model", lambda: reranker)
    docs = [{"chunk_id": value} for value in ("a", "b", "c")]
    rerank_service.use_reranker_deal_score([["9 8", "1"], ["9", "3 7 7 7"], ["9", "2 7"]], docs)
    assert batches == [(2, 4), (1, 2)]
    assert [doc["chunk_id"] for doc in docs] == ["b", "c", "a"]
    assert [doc["score"] for doc in docs] == pytest.approx(torch.tensor([3., 2., 1.]).sigmoid().tolist())
