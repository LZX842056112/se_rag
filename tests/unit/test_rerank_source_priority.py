"""重排的来源优先级测试。

事故背景：用户审批入库的 FAQ 在重排里只喂了答案文本（“配对码123456”），被 4 条联网结果
（0.999x）挤出上下文，于是客服回答「未查询到」且没有任何引用。
"""
from __future__ import annotations

from app.rag.query import rerank_service
from app.shared.models import llm_providers


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
