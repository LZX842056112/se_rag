"""手输纯主体名的服务端兜底回归。

回归背景（联调实测）：
用户问「hak180是什么」→ 系统反问「没有识别到明确的主体，以下可能是您要找的：
Brother HAK 180 烫金机。请点击下方主体直接提问。」→ 用户**没点按钮**，手输
「Brother HAK 180 烫金机」→ 系统答的是更早那轮的「怎么安装」。

根因：反问轮的用户消息与助手消息 ``item_names`` 都是空列表，被
``history_utils.list_valid_history`` 过滤掉，模型只看得见更早的话题，于是把意图
借成了「怎么安装」。

修复一（前端，见 ``test_frontend_pages.py``）让手输命中候选主体时也补上
``rewrite_hint``；但挂起态存在内存里，页面刷新即丢。本文件锁住修复二：
**反问轮把「触发反问的原话 + 候选主体名」落库**，下一轮服务端据此确定性复原意图，
不再经过模型。
"""
from __future__ import annotations

import pytest

from app.process.query.agent.state import create_query_default_state
from app.rag.query import item_name_confirm_service as svc
from app.rag.query.answer_service import save_answer_message_history
from app.shared.clients import history_repository as history_module

SUBJECT = "Brother HAK 180 烫金机"
OTHER = "Brother HAK 200 烫金机"
ASK = "hak180是什么"


# ==================== 反问轮落库 pending ====================

@pytest.fixture
def _saved(monkeypatch):
    """拦截历史落库，返回收集到的 kwargs 列表。"""
    saved: list[dict] = []
    monkeypatch.setattr(
        svc.history_repository, "save_message", lambda **kwargs: saved.append(kwargs) or ""
    )
    return saved


def test_clarify_round_records_pending_intent_and_options(_saved):
    """反问轮必须落库「触发反问的原话 + 候选主体名」，这是兜底唯一的依据。"""
    state = create_query_default_state(session_id="s1", original_query=ASK)
    state["answer"] = "没有识别到明确的主体，以下可能是您要找的：…"
    state["item_name_options"] = [{"item_name": SUBJECT}]

    save_answer_message_history(state)

    assert _saved[-1]["pending_subject_ask"] == {"intent": ASK, "options": [SUBJECT]}


def test_normal_round_records_empty_pending(_saved):
    """正常作答不得留下 pending，否则下一轮会被误判成「在回答反问」。"""
    state = create_query_default_state(session_id="s1", original_query="烫金机怎么安装")
    state["answer"] = "安装步骤如下…"
    state["item_names"] = [SUBJECT]

    save_answer_message_history(state)

    assert _saved[-1]["pending_subject_ask"] == {}


def test_missing_item_name_options_key_does_not_raise(_saved):
    """缺键的旧 state 不得 KeyError（落库降级用例会传不含该键的 state）。"""
    save_answer_message_history({"session_id": "s", "answer": "a", "rewritten_query": "q"})

    assert _saved[-1]["pending_subject_ask"] == {}


def test_repository_writes_pending_field(monkeypatch):
    """仓储层必须把该字段写进 document，否则下一轮读不到。"""
    captured: dict = {}

    class _Collection:
        def insert_one(self, document):
            captured.update(document)
            return type("_Result", (), {"inserted_id": "id-1"})()

    monkeypatch.setattr(
        # 属性桩要返回「可调用对象」：save_message 里是 self._collection() 调用它
        history_module.HistoryRepository, "_collection", property(lambda self: _Collection)
    )

    history_module.HistoryRepository().save_message(
        session_id="s1", role="assistant", text="t",
        pending_subject_ask={"intent": ASK, "options": [SUBJECT]},
    )

    assert captured["pending_subject_ask"] == {"intent": ASK, "options": [SUBJECT]}


# ==================== find_pending_clarify ====================

def test_find_pending_clarify_returns_record(monkeypatch):
    """最近一条是带候选的助手反问 → 返回其 pending 记录。"""
    record = {"intent": ASK, "options": [SUBJECT]}
    monkeypatch.setattr(
        svc.history_repository, "list_recent",
        lambda session_id, limit=10: [{"role": "assistant", "pending_subject_ask": record}],
    )

    assert svc.find_pending_clarify("s1") == record


@pytest.mark.parametrize(
    "messages",
    [
        [],                                                                    # 首轮无历史
        [{"role": "user", "pending_subject_ask": {"intent": ASK, "options": [SUBJECT]}}],
        [{"role": "assistant", "pending_subject_ask": {}}],                     # 正常作答
        [{"role": "assistant", "pending_subject_ask": {"intent": ASK}}],         # 缺 options
        [{"role": "assistant", "pending_subject_ask": {"options": [SUBJECT]}}],  # 缺 intent
        [{"role": "assistant"}],                                               # 旧消息无该字段
    ],
)
def test_find_pending_clarify_returns_empty(monkeypatch, messages):
    """上一条不是「带候选的助手反问」时一律返回空字典，让路给常规流程。"""
    monkeypatch.setattr(
        svc.history_repository, "list_recent", lambda session_id, limit=10: messages
    )

    assert svc.find_pending_clarify("s1") == {}


def test_find_pending_clarify_only_reads_latest(monkeypatch):
    """只读最近一条：更早的反问不得残留成「上一轮意图」。"""
    seen: list[int] = []

    def _list_recent(session_id, limit=10):
        seen.append(limit)
        return [{"role": "assistant", "pending_subject_ask": {}}]

    monkeypatch.setattr(svc.history_repository, "list_recent", _list_recent)

    svc.find_pending_clarify("s1")

    assert seen == [1], "待答反问只看最近一条，不得回看更早的历史"


# ==================== resolve_by_pending_clarify ====================

def _patch_catalog(monkeypatch, name: str) -> None:
    """把目录对齐桩成「总能确认到 name」，聚焦消解逻辑本身。"""
    monkeypatch.setattr(svc, "search_by_item_names", lambda names: {"raw": names})
    monkeypatch.setattr(
        svc, "select_item_names",
        lambda result: {
            "confirmed_list": [{"item_name": name}], "option_list": [], "similar_list": []
        },
    )


@pytest.fixture
def _catalog(monkeypatch):
    _patch_catalog(monkeypatch, SUBJECT)


@pytest.fixture
def _pending(monkeypatch):
    monkeypatch.setattr(
        svc, "find_pending_clarify", lambda session_id: {"intent": ASK, "options": [SUBJECT]}
    )


def test_takes_over_for_bare_candidate_subject(_pending, _catalog):
    """刷新后手输候选主体名 → 用反问原话确定性复原意图。"""
    state = create_query_default_state(session_id="s1", original_query=SUBJECT)

    assert svc.resolve_by_pending_clarify(state) is True
    assert state["rewritten_query"] == f"{SUBJECT}是什么"
    assert state["item_names"] == [SUBJECT]
    assert state["item_name_options"] == []


def test_takes_over_when_only_model_code_typed(_pending, _catalog):
    """只输型号前缀（hak180）同样算在回答反问。"""
    state = create_query_default_state(session_id="s1", original_query="hak180")

    assert svc.resolve_by_pending_clarify(state) is True
    assert state["rewritten_query"] == f"{SUBJECT}是什么"


def test_takeover_clears_preset_answer(_pending, _catalog):
    """接管后必须清掉 answer，否则路由会短路去答反问话术。"""
    state = create_query_default_state(session_id="s1", original_query=SUBJECT)
    state["answer"] = "没有识别到明确的主体……"

    assert svc.resolve_by_pending_clarify(state) is True
    assert state["answer"] is None


def test_skips_without_pending_and_avoids_vector_search(monkeypatch):
    """没有待答反问时直接早退，不得白跑向量检索。"""
    calls: list = []
    monkeypatch.setattr(svc, "find_pending_clarify", lambda session_id: {})
    monkeypatch.setattr(svc, "search_by_item_names", lambda names: calls.append(names) or {})

    state = create_query_default_state(session_id="s1", original_query=SUBJECT)

    assert svc.resolve_by_pending_clarify(state) is False
    assert calls == [], "无待答反问时白跑了向量检索"
    assert state["rewritten_query"] == ""


def test_skips_when_subject_not_in_candidates(monkeypatch, _pending, _catalog):
    """反问给的是 HAK 180、用户输 HAK 200 → 不接管，避免拼出垃圾问句。"""
    _patch_catalog(monkeypatch, OTHER)
    state = create_query_default_state(session_id="s1", original_query=OTHER)

    assert svc.resolve_by_pending_clarify(state) is False
    assert state["rewritten_query"] == ""


def test_skips_when_query_has_own_intent(_pending, _catalog):
    """本轮自带意图 → 尊重用户，不接管。"""
    state = create_query_default_state(session_id="s1", original_query=f"{SUBJECT}怎么保养")

    assert svc.resolve_by_pending_clarify(state) is False
    assert state["rewritten_query"] == ""


def test_skips_when_subject_not_alignable(_pending, monkeypatch):
    """主体对齐不上目录 → 不接管。"""
    monkeypatch.setattr(svc, "search_by_item_names", lambda names: {})
    monkeypatch.setattr(
        svc, "select_item_names",
        lambda result: {"confirmed_list": [], "option_list": [], "similar_list": []},
    )
    state = create_query_default_state(session_id="s1", original_query="不存在的设备")

    assert svc.resolve_by_pending_clarify(state) is False
    assert state["rewritten_query"] == ""


# ==================== confirm_item_name 的优先级 ====================

def test_confirm_item_name_prefers_rewrite_hint(monkeypatch, _catalog):
    """带 rewrite_hint 时必须走 resolve_by_hint，不得再查待答反问。"""
    monkeypatch.setattr(svc, "save_history_message", lambda state: None)
    monkeypatch.setattr(
        svc, "find_pending_clarify",
        lambda session_id: pytest.fail("有 rewrite_hint 时不应查询待答反问"),
    )

    state = create_query_default_state(
        session_id="s1", original_query=SUBJECT, rewrite_hint="hak180怎么安装"
    )
    svc.confirm_item_name(state)

    assert state["rewritten_query"] == f"{SUBJECT}怎么安装"


def test_confirm_item_name_uses_pending_clarify(monkeypatch, _catalog, _pending):
    """无 hint 但上一轮反问过且本轮是候选主体名 → 走兜底，不调模型。"""
    monkeypatch.setattr(svc, "save_history_message", lambda state: None)
    monkeypatch.setattr(
        svc, "call_llm_item_name_and_rewritten",
        lambda *args, **kwargs: pytest.fail("兜底接管后不应调用模型改写"),
    )

    state = create_query_default_state(session_id="s1", original_query=SUBJECT)
    svc.confirm_item_name(state)

    assert state["rewritten_query"] == f"{SUBJECT}是什么"
