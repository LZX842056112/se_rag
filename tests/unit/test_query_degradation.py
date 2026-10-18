"""检索降级边界回归：外部依赖故障必须降级，不得把整笔查询打成 500。

回归背景（真实事故窗口）：三类外部依赖有三种行为——
- Milvus 混合检索失败 → 网关返回 None → ``chunk_search`` 转 ``[]`` → 但
  ``rrf_service`` 的 ``require_state_list``（空列表即失败）抛 ValueError → **整笔查询 500**；
- HyDE 失败（LLM/检索异常）→ 节点无 try/except → **500**；
- 联网搜索失败 → 硬超时 + 双层 try → 优雅降级为 ``[]``。

即「同类外部依赖，Milvus/HyDE 故障表现为 500，web 故障表现为降级」。本文件把前者
对齐到后者。注意只放宽 RRF 不够：空 ``rrf_chunks`` 会在 ``rerank_service`` 的
``require_state_list`` 再次抛错，仍是 500——连环点必须一起修。
"""
from __future__ import annotations

import pytest

from app.process.query.agent.main_graph import query_app
from app.process.query.agent.nodes import node_search_embedding_hyde as hyde_node
from app.process.query.agent.nodes import node_web_search_mcp as web_node
from app.process.query.agent.state import create_query_default_state
from app.rag.query import answer_service, item_name_confirm_service, rerank_service, rrf_service
from app.shared.clients import history_repository as history_module
from app.shared.clients.milvus_gateway import milvus_gateway as gw_instance
from app.shared.models import llm_providers

_JSON_HINT = "请直接返回JSON格式结果"


class _FakeChain:
    """支持 invoke / stream 的假链（由 _FakeChat 经 ``|`` 得到）。"""

    def __init__(self, chat: "_FakeChat"):
        self._chat = chat

    def invoke(self, messages):
        return _AiMessage(self._chat._invoke(messages))

    def stream(self, messages):
        yield _Chunk(self._chat._invoke(messages))


class _Chunk:
    def __init__(self, content):
        self.content = content


class _AiMessage(str):
    """伪装成 langchain AIMessage：本体是文本，同时暴露 ``.content``。

    ``answer_service`` 走 ``invoke(messages).content``，而 HyDE 链经
    ``StrOutputParser`` 直接取文本，故两者都要满足。
    """

    @property
    def content(self) -> str:
        return str(self)


class _FakeChat:
    """按消息内容分叉：主体抽取走 JSON，其余走普通文本。

    同时支持 ``invoke``（返回带 ``.content`` 的消息对象）与
    ``| StrOutputParser()`` 两种用法：前者给 ``chat(json_mode=True).invoke(...)``
    与 ``answer_service`` 的生成调用，后者给 HyDE / 链路调用。
    """

    def __init__(self):
        self.calls = 0

    def _invoke(self, messages):
        self.calls += 1
        text = messages[0].content if messages else ""
        if _JSON_HINT in text:
            return '{"item_names": ["HAK 180"], "rewritten_query": "HAK 180 的电源参数"}'
        if "接地性评估器" in text:
            return '{"groundedness": 0.9, "unsupported": []}'
        return "参考内容中未提及该信息，无法作答"

    def invoke(self, messages):
        return _AiMessage(self._invoke(messages))

    def stream(self, messages):
        yield _Chunk(self._invoke(messages))

    def __or__(self, _other):
        return _FakeChain(self)


def _patch_llm(monkeypatch):
    fake_chat = _FakeChat()
    monkeypatch.setattr(llm_providers, "chat", lambda *a, **k: fake_chat)
    monkeypatch.setattr(
        llm_providers, "embed_text", lambda text: {"dense": [[0.1, 0.2]], "sparse": [{1: 0.5}]}
    )
    monkeypatch.setattr(
        llm_providers, "generate_embeddings",
        lambda texts: {"dense": [[0.1, 0.2] for _ in texts], "sparse": [{1: 0.5} for _ in texts]},
    )
    return fake_chat


def _milvus_down_except_item_names(**kwargs):
    """模拟 Milvus 故障：只有主体名集合可用，kb_chunks 全部失败（网关返回 None）。"""
    if kwargs.get("collection_name") == gw_instance.item_name_collection_name:
        return [[{"id": "1", "distance": 0.9, "entity": {"item_name": "HAK 180"}}]]
    return None


def test_milvus_down_query_no_longer_500(monkeypatch):
    """**本次最重要的回归**：主体确认可用但 kb/HyDE 混合检索失败时不再 500。

    精确复现真实事故窗口：主体名集合正常（确认通过）、kb_chunks 集合检索失败
    （网关返回 None）。历史实现会在 rrf_service 的 require_state_list 抛
    ValueError，把整笔查询打成 500；现在应按零命中降级作答。

    注意：业务层经单例 ``milvus_gateway.hybrid_search`` 调用（实例方法，内部再取
    客户端），故必须替换**实例方法**；替换模块级同名函数不会命中调用点。
    """
    _patch_llm(monkeypatch)
    monkeypatch.setattr(gw_instance, "hybrid_search", _milvus_down_except_item_names)
    monkeypatch.setattr(web_node, "search_by_web", lambda state: [])

    state = query_app.invoke(create_query_default_state(
        session_id="s_deg", original_query="HAK 180 的电源参数", is_stream=False
    ))

    assert state.get("answer"), "应产出兜底答案而非抛异常"
    assert state["retrieval_signals"]["zero_hit"] is True
    assert state["reranked_docs"] == []


def test_hyde_node_failure_degrades_to_empty(monkeypatch):
    """HyDE 节点故障降级为该路空结果，不抛异常。"""
    def _boom(_state):
        raise RuntimeError("假设答案生成失败")

    monkeypatch.setattr(hyde_node, "search_by_hyde", _boom)

    result = hyde_node.node_search_embedding_hyde({"rewritten_query": "q", "item_names": ["A"]})

    assert result == {"hyde_embedding_chunks": []}


def test_rrf_fuse_with_empty_inputs_no_raise():
    """两路召回均空时按零命中融合，不得抛 ValueError。"""
    state = {
        "embedding_chunks": [],
        "hyde_embedding_chunks": [],
        "web_search_docs": [],
        "evolution_chunks": [],
    }

    rrf_service.fuse_by_rrf(state)

    assert state["rrf_chunks"] == []


def test_rerank_skips_model_when_all_empty(monkeypatch):
    """全链路零命中时不加载重排模型，直接按空结果进入作答。"""
    def _must_not_load():
        raise AssertionError("零命中时不应加载重排模型")

    monkeypatch.setattr(rerank_service.llm_providers, "reranker_model", _must_not_load)
    state = {"rewritten_query": "q", "rrf_chunks": [], "web_search_docs": []}

    rerank_service.rerank_documents(state)

    assert state["reranked_docs"] == []


def test_rerank_still_enforces_rewritten_query():
    """降级不放宽 rewritten_query：该字段缺失仍是硬失败（前置契约）。"""
    state = {"rewritten_query": "", "rrf_chunks": [], "web_search_docs": []}

    with pytest.raises(ValueError):
        rerank_service.rerank_documents(state)


# ---------------- P0-4 落库降级 ----------------


def _raiser_collection(*_args, **_kwargs):
    raise RuntimeError("Mongo 不可用")


def test_save_message_degrades_on_mongo_error(monkeypatch):
    """仓储层落库失败降级为空串，不抛给主链路。"""
    monkeypatch.setattr(
        history_module.HistoryRepository, "_collection", property(lambda self: _raiser_collection)
    )

    result = history_module.HistoryRepository().save_message(
        session_id="s1", role="user", text="t"
    )

    assert result == ""


def test_generate_answer_survives_history_write_failure(monkeypatch):
    """答案已生成后落库失败，不得把整笔查询打成 500。

    在**取集合**这一步注入故障，让真实的 ``save_message`` 走内部降级分支；
    直接替换 ``save_message`` 会绕过被验证的 try/except，测不到真实行为。
    """
    _patch_llm(monkeypatch)
    monkeypatch.setattr(history_module, "get_collection", _raiser_collection)
    state = {
        "session_id": "s",
        "rewritten_query": "q",
        "item_names": ["A"],
        "is_stream": False,
        "reranked_docs": [
            {"chunk_id": "c1", "title": "t", "content": "内容", "source": "milvus",
             "type": "milvus", "parent_id": "p1", "page": 1}
        ],
    }

    answer_service.generate_answer(state)

    assert state.get("answer")


def test_save_history_message_user_msg_degrades(monkeypatch):
    """用户消息落库失败同样降级（另一调用方）。"""
    monkeypatch.setattr(history_module, "get_collection", _raiser_collection)

    state = {"session_id": "s", "original_query": "q", "rewritten_query": "q", "item_names": ["A"]}

    item_name_confirm_service.save_history_message(state)
