from app.rag.query import manual_keyword_search as keyword_search
from app.rag.query.rrf_service import fuse_by_rrf


def test_manual_keyword_search_uses_confirmed_product_and_finds_error_label(monkeypatch):
    filters = []

    class Client:
        def query(self, **kwargs):
            filters.append(kwargs['filter'])
            return [
                {'chunk_id': 'paper', 'heading_path': '手册 / 装入介质', 'content': '将不同尺寸的纸张装入托盘。'},
                {'chunk_id': 'error', 'heading_path': '手册 / 错误消息', 'content': '纸张尺寸不正确。请确认模式设置。'},
            ]

    monkeypatch.setattr(keyword_search, 'expand_item_names', lambda names: names)
    monkeypatch.setattr(keyword_search, 'milvus_gateway', type('Gateway', (), {
        'milvus_client': Client(), 'chunk_collection_name': 'chunks',
    })())
    rows = keyword_search.search_manual_keywords(['产品A'], '显示纸张尺寸不正确是什么原因？')
    assert [row['chunk_id'] for row in rows] == ['error', 'paper']
    assert filters == ["(item_name in ['产品A'] or item_name == 'default_item_name')"]


def test_named_mode_is_recalled_from_parenthetical_heading():
    docs = [
        {'chunk_id': 'generic', 'content': '节省烫金膜模式可以设置节省烫金膜。',
         'heading_path': '手册 / 节省烫金膜模式'},
        {'chunk_id': 'skip', 'content': '仅在空白区域以外转印。',
         'heading_path': '手册 / 转印到纸张下半部分（跳过）'},
    ]
    assert keyword_search.rank_manual_keywords(docs, '节省烫金膜跳过模式是什么效果？')[0]['chunk_id'] == 'skip'


def test_keyword_terms_preserve_menu_phrases_and_normalize_manual_aliases():
    actual = set(keyword_search._terms("Web BRAAdmin、TCP/IP、IP Boot Tries"))
    assert {"web bradmin", "tcp/ip", "ip boot tries", "bradmin"} <= actual


def test_keyword_search_prefers_a_matching_explicit_model():
    product = "Brother HL-3040CN / HL-3070CW 网络打印服务器"
    docs = [
        {"chunk_id": "other", "item_name": product, "heading_path": "网络菜单（适用于 HL-3070CW）",
         "content": "按键进入网络菜单，打开网络菜单进行设置。"},
        {"chunk_id": "common", "item_name": product, "heading_path": "操作面板",
         "content": "通过网络菜单设置网络。"},
    ]
    ranked = keyword_search.rank_manual_keywords(docs, "HL-3040CN 怎么进入网络菜单？")
    assert [doc["chunk_id"] for doc in ranked][:2] == ["common", "other"]


def test_nested_section_respects_parent_model_scope():
    product = "Brother HL-3040CN / HL-3070CW 网络打印服务器"
    question = "HL-3040CN 的 TCP/IP 启动方式有哪些？"
    other_model = {
        "chunk_id": "other", "item_name": product,
        "heading_path": "无线网络（适用于 HL-3070CW） / TCP/IP",
        "content": "TCP/IP 启动方式。",
    }
    common = {
        "chunk_id": "common", "item_name": product,
        "heading_path": "功能表和出厂默认设置", "content": "启动方式包括 Auto 和 Static。",
    }
    ranked = keyword_search.rank_manual_keywords([other_model, common], question)
    assert [doc["chunk_id"] for doc in ranked] == ["common", "other"]
    assert not keyword_search.matches_requested_heading(other_model, question)
    assert keyword_search.matches_requested_heading(other_model, "HL-3070CW 的 TCP/IP 启动方式有哪些？")


def test_named_menu_prefers_its_section_to_cross_references():
    docs = [
        {"chunk_id": "mention", "heading_path": "重要事项", "content": "在 TCP/IP 菜单中设置 TCP/IP 的配置项。"},
        {"chunk_id": "menu", "heading_path": "网络 / TCP/IP", "content": "共有七项，含 Boot Method 和 IP Address。"},
    ]
    ranked = keyword_search.rank_manual_keywords(docs, "TCP/IP 菜单包含哪些配置项？")
    assert ranked[0]["chunk_id"] == "menu"


def test_named_protocol_with_digits_is_matched_as_a_complete_token():
    docs = [
        {"chunk_id": "mention", "heading_path": "网络规格", "content": "打印服务器支持 IPv6 协议。"},
        {"chunk_id": "protocol", "heading_path": "网络 / IPv6", "content": "此项默认设置为 Off。"},
    ]
    question = "HL-3070CW 是否支持IPv6协议？默认状态是什么？"
    assert keyword_search.matches_requested_heading(docs[1], question)
    assert not keyword_search.matches_requested_heading(docs[1], "IPv60 的配置方式是什么？")
    assert keyword_search.rank_manual_keywords(docs, question)[0]["chunk_id"] == "protocol"


def test_keyword_candidates_extend_semantic_fusion_without_duplicates(monkeypatch):
    monkeypatch.setattr('app.rag.query.rrf_service.get_rrf_top', lambda: 2)
    a = {'chunk_id': 'a'}
    b = {'chunk_id': 'b'}
    hyde_only = {'chunk_id': 'hyde-only'}
    extra = {'chunk_id': 'error'}
    state = {'embedding_chunks': [a, b], 'hyde_embedding_chunks': [b, hyde_only], 'keyword_chunks': [a.copy(), extra]}
    assert [doc['chunk_id'] for doc in fuse_by_rrf(state)['rrf_chunks']] == ['b', 'a', 'hyde-only', 'error']
