"""kb 检索放行全局条目的回归。

回归背景：导入端 ``load_subject`` 对未识别主体回退 ``default_item_name``（inputs.py），
但 kb 检索带主体过滤时只用 ``item_name in [...]``，**不放行**该占位主体，导致未打商品
标签的全局知识永远召回不到。而 evolution 侧（app/evolution/retrieval.py）显式放行——
两侧口径不对称。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

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


def test_manual_heading_scope_keeps_product_filter_and_falls_back_if_section_is_absent(monkeypatch):
    gateway = _patch(monkeypatch, ['Product A'])
    filters = []
    documents = [[{'entity': {'chunk_id': 'fallback'}}]]

    def search(**_kwargs):
        filters.append(gateway.expr)
        return [[]] if len(filters) == 1 else documents

    monkeypatch.setattr(gateway, 'hybrid_search', search)
    result = search_chunks(['Product A'], '手册的连接设置一节如何配置？', heading_path='设备规格 / 连接设置')

    assert result == documents[0]
    assert len(filters) == 2
    assert filters[0].startswith("item_name in ['Product A'] and ")
    assert "heading_path == '设备规格 / 连接设置'" in filters[0]
    assert "heading_path like '设备规格 / 连接设置 / %'" in filters[0]
    assert 'default_item_name' not in filters[0]
    assert 'default_item_name' in filters[1]


def test_named_manual_section_results_do_not_mix_in_unscoped_chunks(monkeypatch):
    gateway = _patch(monkeypatch, ['Product A'])
    filters = []

    def search(**_kwargs):
        filters.append(gateway.expr)
        return [[{'entity': {'chunk_id': 'named-section'}}]]

    monkeypatch.setattr(gateway, 'hybrid_search', search)
    assert search_chunks(['Product A'], 'q', heading_path='设备规格 / 连接设置') == [
        {'entity': {'chunk_id': 'named-section'}},
    ]
    assert len(filters) == 1
    assert "heading_path == '设备规格 / 连接设置'" in filters[0]


@pytest.mark.parametrize(('question', 'expected'), [
    ('手册中，设备规格的连接设置表列出哪些模式？', '设备规格 / 3.2 连接设置'),
    ('请查手册的设备规格章节。该章节的连接设置表列出哪些模式？', '设备规格 / 3.2 连接设置'),
    ('请查手册的连接设置一节。', ''),
    ('请查手册的《不存在》章节。', ''),
    ('设备支持哪些连接设置？', ''),
])
def test_resolve_manual_heading_uses_literal_titles_in_confirmed_product(monkeypatch, question, expected):
    paths = ['设备规格 / 3.2 连接设置', '设备规格 / 3.2 连接设置 / 注意事项', '安装步骤 / 连接设置']
    queries = []

    def query(**kwargs):
        queries.append(kwargs)
        return [{'heading_path': path} for path in paths]

    _patch(monkeypatch, ['Product A'])
    monkeypatch.setattr(chunk_search.milvus_gateway, 'milvus_client', SimpleNamespace(query=query), raising=False)

    assert chunk_search.resolve_manual_heading(['Product A'], question) == expected
    if '手册' in question:
        assert len(queries) == 1
        assert queries[0]['filter'] == "item_name in ['Product A']"
        assert queries[0]['output_fields'] == ['heading_path']
    else:
        assert queries == []
