"""kb 检索放行全局条目的回归。

回归背景：导入端 ``load_subject`` 对未识别主体回退 ``default_item_name``（inputs.py），
但 kb 检索带主体过滤时只用 ``item_name in [...]``，**不放行**该占位主体，导致未打商品
标签的全局知识永远召回不到。而 evolution 侧（app/evolution/retrieval.py）显式放行——
两侧口径不对称。
"""
from __future__ import annotations

from app.rag.query import chunk_search
from app.rag.query.chunk_search import search_chunks


class _StubGateway:
    chunk_collection_name = "kb_chunks"

    def __init__(self):
        self.expr: str | None = None

    def create_requests(self, dense_vector, sparse_vector, expr=None, limit=5, dense_params=None):
        self.expr = expr
        return []

    def hybrid_search(self, *, collection_name, reqs, ranker_weights, norm_score, limit, output_fields):
        return None


def _patch(monkeypatch, names: list[str]) -> _StubGateway:
    gateway = _StubGateway()
    monkeypatch.setattr(chunk_search, "milvus_gateway", gateway)
    monkeypatch.setattr(chunk_search, "expand_item_names", lambda item_names: list(names))
    monkeypatch.setattr(
        chunk_search.llm_providers,
        "embed_text",
        lambda text: {"dense": [[0.1, 0.2]], "sparse": [{1: 0.5}]},
    )
    return gateway


def test_expr_releases_global_item_name(monkeypatch):
    """表达式必须显式放行 default_item_name，且带括号避免优先级歧义。"""
    gateway = _patch(monkeypatch, ["Brother HAK 180 烫金机"])

    result = search_chunks(["Brother HAK 180 烫金机"], "q")

    assert result == []
    assert gateway.expr == (
        "(item_name in ['Brother HAK 180 烫金机'] "
        "or item_name == 'default_item_name')"
    )


def test_expr_escapes_quote_in_item_name(monkeypatch):
    """主体名含单引号时必须转义（in_expr 自带，但需确认组合后仍合法）。"""
    gateway = _patch(monkeypatch, ["Brother HAK 180's 烫金机"])

    search_chunks(["Brother HAK 180's 烫金机"], "q")

    assert "Brother HAK 180\\'s 烫金机" in gateway.expr
    assert gateway.expr.endswith("or item_name == 'default_item_name')")


def test_no_item_names_means_no_filter(monkeypatch):
    """无主体过滤名单时不加表达式（全库检索语义不变）。"""
    gateway = _patch(monkeypatch, [])

    search_chunks([], "q")

    assert gateway.expr is None


def test_search_chunks_returns_empty_when_gateway_none(monkeypatch):
    """混合检索返回 None 时降级为空列表，不抛异常。"""
    _patch(monkeypatch, ["A"])

    assert search_chunks(["A"], "q") == []
