"""客服页闲聊功能回归：意图识别分流（rag / chitchat）与闲聊作答链路。

回归背景（功能新增）：
客服页此前所有提问都强制走「主体确认 → 三路召回 → 重排 → 作答」，问候寒暄
（「你好」「谢谢」）会被当成知识库提问，先抽主体、再检索，最后回一句
「本次问题没有关联到任何主体……请补充产品名称后再提问」，体验生硬。

本次改动在图入口新增 node_intent_route（LLM 二分类），闲聊走
node_chitchat_answer 直接作答。本文件锁住四条边界：
1. 开关关闭 / rewrite_hint / 识别失败 → 一律 rag，RAG 链路逐字节等价；
2. 意图识别任何异常都降级 rag，不得把原链路打成 500；
3. 闲聊消息落库 intent=chitchat 且 item_names 为空，不污染下一轮主体消解；
4. 闲聊轮绝不写 fb_events（自进化缺口信号），含答案疑似兜底话术的极端用例。
"""
from __future__ import annotations

import inspect
from pathlib import Path

import mongomock
import pytest

from app.api.schema.query_schema import (
    HistoryItemResponseSchema,
    QueryNotStreamResponseSchema,
)
from app.evolution import repositories as repositories_module
from app.process.query.agent import main_graph
from app.process.query.agent.main_graph import after_node_intent_route
from app.process.query.agent.state import create_query_default_state
from app.rag.query import chitchat_service as chitchat
from app.rag.query import history_utils
from app.rag.query import intent_route_service as intent_svc
from app.rag.query import pipeline as pipeline_module
from app.rag.query.pipeline import invoke_query_graph
from app.shared.clients import history_repository as history_module
from app.shared.config import settings
from app.shared.models import providers as providers_module
from app.shared.utils.sse_broker import SSEEvent

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPT_DIR = PROJECT_ROOT / "app" / "resources" / "prompts"


# ==================== 测试工具 ====================

class _Resp:
    """模拟 LLM 响应 / 流式 chunk：只需带 content 属性。"""

    def __init__(self, content: str):
        self.content = content


class _FakeLLMClient:
    """按用例配置返回固定内容 / 固定流式分片 / 抛异常。"""

    def __init__(self, content=None, chunks=None, raises=None):
        self._content = content
        self._chunks = chunks
        self._raises = raises

    def invoke(self, _messages):
        if self._raises:
            raise self._raises
        return _Resp(self._content)

    def stream(self, _messages):
        if self._raises:
            raise self._raises
        for text in self._chunks or []:
            yield _Resp(text)


def _stub_llm(monkeypatch, *, content=None, chunks=None, raises=None):
    """桩掉 get_llm_client：chat()/stream() 全部走假客户端，不触达真实模型。"""
    monkeypatch.setattr(
        providers_module,
        "get_llm_client",
        lambda *_args, **_kwargs: _FakeLLMClient(content=content, chunks=chunks, raises=raises),
    )


def _boom(*_args, **_kwargs):
    pytest.fail("该路径不得调用 LLM")


def _saved_messages(monkeypatch):
    """拦截闲聊落库，返回收集到的 kwargs 列表。"""
    saved: list[dict] = []
    monkeypatch.setattr(
        chitchat.history_repository, "save_message", lambda **kwargs: saved.append(kwargs) or ""
    )
    return saved


# ==================== 意图路由：短路与兜底 ====================

def test_gate_disabled_never_calls_llm(monkeypatch):
    """开关关闭时 route_intent 不得调用 LLM，恒为 rag（默认行为零变化）。"""
    monkeypatch.setattr(intent_svc, "CHITCHAT_ENABLED", False)
    monkeypatch.setattr(intent_svc, "call_llm_classify_intent", _boom)

    state = create_query_default_state(session_id="s1", original_query="你好")
    assert intent_svc.route_intent(state)["intent"] == "rag"


def test_rewrite_hint_short_circuits_to_rag(monkeypatch):
    """点选主体重问（rewrite_hint 非空）一定是知识诉求：不调 LLM，直接 rag。"""
    monkeypatch.setattr(intent_svc, "CHITCHAT_ENABLED", True)
    monkeypatch.setattr(intent_svc, "call_llm_classify_intent", _boom)

    state = create_query_default_state(
        session_id="s1", original_query="Brother HAK 180 烫金机", rewrite_hint="怎么安装"
    )
    assert intent_svc.route_intent(state)["intent"] == "rag"


@pytest.mark.parametrize("query", ["你好", "你好！", "  您好～  ", "在吗？", "谢谢", "Hello!"])
def test_plain_greeting_does_not_depend_on_classifier_or_history(monkeypatch, query):
    """纯寒暄不应因分类模型超时而进入产品检索。"""
    monkeypatch.setattr(intent_svc, "CHITCHAT_ENABLED", True)
    monkeypatch.setattr(intent_svc, "call_llm_classify_intent", _boom)
    monkeypatch.setattr(intent_svc, "build_chat_history_context", _boom)
    state = create_query_default_state(session_id="greeting", original_query=query)
    assert intent_svc.route_intent(state)["intent"] == "chitchat"


@pytest.mark.parametrize("query", ["你好，HAK180的功率是多少？", "谢谢，请问烫金机参数？", "它呢？"])
def test_mixed_greeting_and_product_followup_still_use_rag(monkeypatch, query):
    monkeypatch.setattr(intent_svc, "CHITCHAT_ENABLED", True)
    monkeypatch.setattr(intent_svc, "build_chat_history_context", lambda *a, **kw: "HAK180")
    _stub_llm(monkeypatch, content='{"intent":"rag"}')
    state = create_query_default_state(session_id="product", original_query=query)
    assert intent_svc.route_intent(state)["intent"] == "rag"


def test_greeting_runs_real_query_graph_without_product_lookup(monkeypatch):
    """使用真实图接线，只替换外部模型和消息存储。"""
    monkeypatch.setattr(intent_svc, "CHITCHAT_ENABLED", True)
    monkeypatch.setattr(intent_svc, "call_llm_classify_intent", _boom)
    _stub_llm(monkeypatch, content="你好，我在。")
    saved = _saved_messages(monkeypatch)
    state = create_query_default_state(session_id="hello-graph", original_query="你好")
    result = main_graph.query_app.invoke(state)
    assert result["intent"] == "chitchat"
    assert result["answer"] == "你好，我在。"
    assert result["item_names"] == [] and result["citations"] == []
    assert [message["role"] for message in saved] == ["user", "assistant"]
    assert all(message["intent"] == "chitchat" for message in saved)


@pytest.mark.parametrize("payload,expected", [
    ('{"intent": "chitchat"}', "chitchat"),
    ('{"intent": "rag"}', "rag"),
    # 大小写与空白容错
    ('{"intent": " Chitchat "}', "chitchat"),
    # 多余键忽略
    ('{"intent": "chitchat", "reason": "问候"}', "chitchat"),
])
def test_classify_translates_valid_output(monkeypatch, payload, expected):
    """合法输出按枚举透传；大小写/空白/多余键不破坏分流。"""
    _stub_llm(monkeypatch, content=payload)
    assert intent_svc.call_llm_classify_intent("无对话记录!", "你好") == expected


@pytest.mark.parametrize("payload", [
    "这不是JSON",
    "[1, 2, 3]",                     # JSON 数组，非对象
    '{"intent": "greeting"}',        # 取值不在枚举内
    '{"intent": ""}',                # 空取值
    '{"other": "chitchat"}',         # 缺 intent 键
    "",                              # 空 content
])
def test_classify_falls_back_to_rag_on_dirty_output(monkeypatch, payload):
    """脏输出一律兜底 rag：意图识别不得把原链路打死。"""
    _stub_llm(monkeypatch, content=payload)
    assert intent_svc.call_llm_classify_intent("无对话记录!", "你好") == "rag"


def test_classify_falls_back_when_llm_raises(monkeypatch):
    """LLM 抛异常时降级 rag，且不向上抛。"""
    _stub_llm(monkeypatch, raises=RuntimeError("模型服务不可用"))
    assert intent_svc.call_llm_classify_intent("无对话记录!", "你好") == "rag"


def test_route_intent_logs_and_returns_classified(monkeypatch):
    """常规流程：走 LLM 分类并把结论回写 state。"""
    monkeypatch.setattr(intent_svc, "CHITCHAT_ENABLED", True)
    _stub_llm(monkeypatch, content='{"intent": "chitchat"}')
    # 历史上下文读取也要离线：闲聊轮不过滤主体名
    monkeypatch.setattr(
        history_utils.history_repository, "list_recent",
        lambda session_id, limit=10: [{"role": "user", "text": "在吗", "item_names": []}],
    )

    state = create_query_default_state(session_id="s1", original_query="在吗")
    assert intent_svc.route_intent(state)["intent"] == "chitchat"


def test_intent_prompt_states_conservative_boundary():
    """提示词必须写明保守边界：拿不准 / 混合输入 / 含产品名 → rag。"""
    text = (PROMPT_DIR / "intent_classify.prompt").read_text(encoding="utf-8")
    assert "拿不准" in text, "缺少「拿不准判 rag」的保守规则"
    assert "混合输入" in text, "缺少「问候+知识诉求混合判 rag」规则"
    assert "一律判 rag" in text
    # 文件里 JSON 示例的花括号按 str.format 规则转义为双括号
    assert '{{"intent": "chitchat"}}' in text
    assert '{{"intent": "rag"}}' in text


def test_chitchat_prompt_forbids_no_answer_markers():
    """闲聊提示词必须禁输出「无法作答 / 未查询到」（否则误触自进化缺口信号）。"""
    text = (PROMPT_DIR / "chitchat_answer.prompt").read_text(encoding="utf-8")
    assert "无法作答" in text and "未查询到" in text
    assert "{history_text}" in text and "{query}" in text


# ==================== 历史上下文：闲聊轮不被过滤 ====================

def test_build_chat_history_context_includes_chitchat_rounds(monkeypatch):
    """闲聊消息 item_names 为空，不得被过滤；序号按旧 → 新。"""
    monkeypatch.setattr(
        history_utils.history_repository, "list_recent",
        lambda session_id, limit=10: [
            {"role": "assistant", "text": "不客气哒", "item_names": []},
            {"role": "user", "text": "谢谢", "item_names": []},
        ],
    )

    text = history_utils.build_chat_history_context("s1", 6)

    assert "谢谢" in text and "不客气哒" in text
    assert text.index("序号:1,用户:谢谢") < text.index("序号:2,助手:不客气哒")


def test_build_chat_history_context_empty(monkeypatch):
    """无历史时返回固定提示串（与 build_history_context 口径一致）。"""
    monkeypatch.setattr(history_utils.history_repository, "list_recent", lambda session_id, limit=10: [])
    assert history_utils.build_chat_history_context("s1", 6) == "无对话记录!"


# ==================== 闲聊作答：生成、回填、落库 ====================

def test_generate_chitchat_saves_two_messages(monkeypatch):
    """闲聊链自行落库 user + assistant 两条，均带 intent=chitchat。"""
    _stub_llm(monkeypatch, content="在呢，有什么可以帮你？")
    saved = _saved_messages(monkeypatch)

    state = create_query_default_state(session_id="s1", original_query="在吗")
    chitchat.generate_chitchat_answer(state)

    assert [m["role"] for m in saved] == ["user", "assistant"]
    assert all(m["intent"] == "chitchat" for m in saved)
    assert all(m["item_names"] == [] for m in saved), "闲聊消息不得带主体名（会污染下一轮消解）"
    assert saved[1]["citations"] == []
    assert saved[1]["pending_subject_ask"] == {}, "闲聊轮不得被认领为反问轮"
    assert saved[0]["text"] == "在吗"
    assert saved[1]["text"] == "在呢，有什么可以帮你？"


def test_generate_chitchat_backfills_state(monkeypatch):
    """state 回填：answer / intent / 空引用 / groundedness=None / 空主体。"""
    _stub_llm(monkeypatch, content="你好呀，欢迎随时来聊～")
    _saved_messages(monkeypatch)

    state = create_query_default_state(session_id="s1", original_query="你好")
    chitchat.generate_chitchat_answer(state)

    assert state["answer"] == "你好呀，欢迎随时来聊～"
    assert state["intent"] == "chitchat"
    assert state["citations"] == []
    assert state["groundedness"] is None, "未评估必须是 None，前端才显示「未评估」而非 0%"
    assert state["image_urls"] == []
    assert state["item_names"] == []
    assert state["rewritten_query"] == ""


def test_chitchat_stream_publishes_delta(monkeypatch):
    """流式闲聊逐段 publish DELTA，answer 为拼接结果。"""
    _stub_llm(monkeypatch, chunks=["你好", "呀，", "欢迎来逛～"])
    events: list[tuple] = []
    monkeypatch.setattr(chitchat, "publish", lambda sid, event, data: events.append((event, data)))
    _saved_messages(monkeypatch)

    state = create_query_default_state(session_id="s1", original_query="你好", is_stream=True)
    chitchat.generate_chitchat_answer(state)

    deltas = [d["delta"] for event, d in events if event == SSEEvent.DELTA]
    assert deltas == ["你好", "呀，", "欢迎来逛～"]
    assert state["answer"] == "你好呀，欢迎来逛～"


def test_chitchat_llm_failure_uses_fallback_answer(monkeypatch):
    """闲聊 LLM 失败降级为固定兜底话术，不打 500（口径同 HyDE / 联网）。"""
    _stub_llm(monkeypatch, raises=RuntimeError("模型服务不可用"))
    saved = _saved_messages(monkeypatch)

    state = create_query_default_state(session_id="s1", original_query="你好")
    chitchat.generate_chitchat_answer(state)

    assert state["answer"] == chitchat.CHITCHAT_FALLBACK_ANSWER
    assert saved[-1]["text"] == chitchat.CHITCHAT_FALLBACK_ANSWER


# ==================== 仓储：intent 字段 ====================

@pytest.fixture
def fake_mongo(monkeypatch):
    """mongomock 替换 chat_message 集合获取，避免真实连库。"""
    client = mongomock.MongoClient()
    db = client["test_db"]
    monkeypatch.setattr(history_module, "get_collection", lambda name: db[name])
    return db


def test_repository_writes_intent_field(fake_mongo):
    """save_message(intent=...) 必须落进文档。"""
    history_module.history_repository.save_message(
        session_id="s-intent", role="user", text="在吗", intent="chitchat"
    )
    doc = fake_mongo[settings.mongo.chat_message_collection].find_one({"session_id": "s-intent"})
    assert doc["intent"] == "chitchat"


def test_repository_defaults_intent_empty(fake_mongo):
    """不传 intent 的既有调用点落空串（旧文档缺键兼容，无需迁移）。"""
    history_module.history_repository.save_message(
        session_id="s-no-intent", role="user", text="烫金机怎么安装"
    )
    doc = fake_mongo[settings.mongo.chat_message_collection].find_one({"session_id": "s-no-intent"})
    assert doc["intent"] == ""


# ==================== 自进化守卫：闲聊轮不写 fb_events ====================

def _fake_chitchat_app(answer: str):
    """桩查询图：直接返回一轮闲聊结果（不跑真实图）。"""

    class _FakeApp:
        def invoke(self, _state):
            return {
                "session_id": "s-chitchat",
                "original_query": "谢谢你",
                "answer": answer,
                "intent": "chitchat",
                "item_names": [],
                "citations": [],
                "retrieval_signals": {},
            }

    return _FakeApp()


def _run_chitchat_query(monkeypatch):
    """跑一轮闲聊 invoke_query_graph（图与自进化仓储全部打桩）。"""
    client = mongomock.MongoClient()
    db = client["test_db"]
    monkeypatch.setattr(repositories_module, "get_collection", lambda name: db[name])
    monkeypatch.setattr(settings.evolution, "enabled", True)
    monkeypatch.setattr(pipeline_module, "query_app", _fake_chitchat_app("不客气哒"))
    invoke_query_graph(session_id="s-chitchat", original_query="谢谢你", is_stream=False)
    return list(db[settings.mongo.fb_events_collection].find({}))


def test_chitchat_never_writes_fb_events(monkeypatch):
    """闲聊轮即使开了自进化，也不写未解决信号（无检索过程 ≠ 知识缺口）。"""
    assert _run_chitchat_query(monkeypatch) == []


def test_chitchat_guard_survives_no_answer_like_text(monkeypatch):
    """极端用例：闲聊答案疑似兜底话术时，守卫仍拦住 fb_events（不靠 prompt 运气）。"""
    client = mongomock.MongoClient()
    db = client["test_db"]
    monkeypatch.setattr(repositories_module, "get_collection", lambda name: db[name])
    monkeypatch.setattr(settings.evolution, "enabled", True)
    monkeypatch.setattr(
        pipeline_module, "query_app", _fake_chitchat_app("这个问题我无法作答，建议您咨询客服")
    )
    invoke_query_graph(session_id="s-chitchat2", original_query="谢谢你", is_stream=False)

    docs = list(db[settings.mongo.fb_events_collection].find({}))
    assert docs == [], "闲聊轮被 is_no_answer 误命中，守卫失效"


# ==================== 图结构：闲聊分支接线 ====================

def test_after_node_intent_route_routes_by_intent():
    """条件边纯函数：chitchat → 闲聊节点；rag / 缺键 → 主体确认。"""
    assert after_node_intent_route({"intent": "chitchat"}) == "node_chitchat_answer"
    assert after_node_intent_route({"intent": "rag"}) == "node_item_name_confirm"
    assert after_node_intent_route({}) == "node_item_name_confirm", "缺键旧 state 必须走 RAG"


def test_query_graph_wires_chitchat_branch():
    """图入口与闲聊分支接线：入口改为意图路由，闲聊作答后直接 END。"""
    source = inspect.getsource(main_graph)
    assert 'set_entry_point("node_intent_route")' in source
    assert 'add_conditional_edges("node_intent_route"' in source
    assert '"node_chitchat_answer": "node_chitchat_answer"' in source
    assert 'add_edge("node_chitchat_answer", END)' in source


def test_rag_branch_structure_unchanged():
    """回归锁：主体确认节点的原条件边与四目标映射不得被动过。"""
    source = inspect.getsource(main_graph)
    assert 'add_conditional_edges("node_item_name_confirm"' in source
    for node in ("node_search_embedding", "node_search_embedding_hyde",
                 "node_web_search_mcp", "node_answer_output"):
        assert f'"{node}": "{node}"' in source, f"RAG 分支目标 {node} 丢失"
    # 原有静态链路保持旧序
    assert source.index('add_edge("node_rrf", "node_rerank")') < source.index(
        'add_edge("node_rerank", "node_parent_expand")'
    )


def test_default_state_carries_intent():
    """默认 state 必须带 intent 键（缺键会让 .get 退化成 rag，掩盖 wiring 错误）。"""
    assert create_query_default_state()["intent"] == "rag"


# ==================== Schema 兼容 ====================

def test_schemas_default_intent():
    """不带 intent 时：非流式响应默认 rag，历史项默认空串（旧客户端不破）。"""
    resp = QueryNotStreamResponseSchema(
        message="m", session_id="s", answer="a", done_list=[], image_urls=[]
    )
    assert resp.intent == "rag"
    item = HistoryItemResponseSchema(id="1", session_id="s", role="assistant", text="t", ts=1)
    assert item.intent == ""


def test_state_typo_guard(monkeypatch):
    """闲聊分支节点装饰器可独立运行：直接调用 node 函数应完成作答并落库。"""
    from app.process.query.agent.nodes.node_chitchat_answer import node_chitchat_answer

    _stub_llm(monkeypatch, content="在呢～")
    _saved_messages(monkeypatch)
    state = create_query_default_state(session_id="s1", original_query="在吗", is_stream=False)
    result = node_chitchat_answer(state)
    assert result["intent"] == "chitchat" and result["answer"] == "在呢～"
