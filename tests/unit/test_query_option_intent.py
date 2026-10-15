"""点选主体后的意图保真回归。

回归背景（联调实测）：
用户问“hak180怎么安装” → 系统反问主体 → 用户点选“Brother HAK 180 烫金机”，
结果答的是上一个问题（蓝牙连接）。根因有两层，本文件锁住两层修复：

1. 前端 ``optionQuestion`` 用「原问题 <= 12 字符」判断「用户只是在指这个主体」，
   “hak180怎么安装”（10 字符）被误判，意图词「怎么安装」被丢弃，
   点选后只发出纯主体名 → 该用例已由 ``test_frontend_pages.py`` 覆盖；
2. 主体消解把「一句没有意图的问句」交给模型去补，模型顺着历史话题补成了“蓝牙”。

修复：前端把「主体名」与「意图原句」拆成两个信令（``query`` / ``rewrite_hint``），
服务端用 ``compose_query_with_hint`` 确定性拼装，模型不再有推断意图的机会。
"""
from __future__ import annotations

import pytest

from app.process.query.agent.state import create_query_default_state
from app.rag.query import item_name_confirm_service as svc
from app.rag.query.item_name_confirm_service import (
    compose_query_with_hint,
    resolve_by_hint,
)

SUBJECT = "Brother HAK 180 烫金机"


# ==================== compose_query_with_hint ====================

@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        # 型号别名要剔除，否则拼出 “烫金机hak180怎么安装”
        ("hak180怎么安装", "Brother HAK 180 烫金机怎么安装"),
        ("hak180烫金机怎么换膜", "Brother HAK 180 烫金机怎么换膜"),
        # 品类词也要剔除，否则拼出 “烫金机烫金机如何保养”
        ("烫金机如何保养", "Brother HAK 180 烫金机如何保养"),
        # 原句已含完整主体名 → 直接用原句，不重复拼接
        ("Brother HAK 180 烫金机怎么安装", "Brother HAK 180 烫金机怎么安装"),
        ("Brother HAK 180 烫金机", "Brother HAK 180 烫金机"),
        # 原句无任何主体别名 → 纯拼装
        ("怎么安装", "Brother HAK 180 烫金机怎么安装"),
        ("额定功率是多少", "Brother HAK 180 烫金机额定功率是多少"),
        # 原句只是主体别名 → 退回主体名
        ("hak180", "Brother HAK 180 烫金机"),
        ("haK-180", "Brother HAK 180 烫金机"),
        # 空原句 → 退回主体名
        ("", "Brother HAK 180 烫金机"),
    ],
)
def test_compose_query_keeps_intent_and_drops_duplicate_subject(hint: str, expected: str):
    """拼装后必须同时满足：意图保留、主体不重复、问句通顺。"""
    assert compose_query_with_hint(SUBJECT, hint) == expected


def test_compose_query_never_returns_empty_when_any_input_present():
    """只要两侧有一侧非空，就不得产出空问句（空问句会让下游检索直接失败）。

    两侧都为空是调用方的契约违约，此处只要求不抛异常并返回空串。
    """
    for subject in (SUBJECT, "", "  "):
        for hint in ("", "  ", "hak180", "怎么安装"):
            got = compose_query_with_hint(subject, hint).strip()
            if not subject.strip() and not hint.strip():
                assert got == "", "两侧皆空应返回空串"
                continue
            assert got, f"subject={subject!r} hint={hint!r} 产出空问句"


# ==================== resolve_by_hint ====================

@pytest.fixture
def _patched_catalog(monkeypatch):
    """把目录对齐桩成「总能确认到 SUBJECT」，聚焦拼装逻辑本身。"""
    monkeypatch.setattr(svc, "search_by_item_names", lambda names: {"raw": names})
    monkeypatch.setattr(
        svc,
        "select_item_names",
        lambda result: {"confirmed_list": [{"item_name": SUBJECT}], "option_list": [], "similar_list": []},
    )


def test_resolve_by_hint_composes_query_without_llm(monkeypatch, _patched_catalog):
    """点选消解不得调用模型——意图由前端直传，模型没有推断余地。"""
    called: list = []

    def _boom(*args, **kwargs):
        called.append(args)
        raise AssertionError("点选消解不应调用 LLM")

    monkeypatch.setattr(svc, "call_llm_item_name_and_rewritten", _boom)

    state = create_query_default_state(
        session_id="s1", original_query=SUBJECT, rewrite_hint="hak180怎么安装"
    )
    assert resolve_by_hint(state) is True
    assert called == [], "点选消解路径不应经过模型改写"
    assert state["rewritten_query"] == f"{SUBJECT}怎么安装"
    assert state["item_names"] == [SUBJECT]
    assert state["item_name_options"] == []


def test_resolve_by_hint_clears_preset_answer(_patched_catalog):
    """确认主体后必须清掉 answer，否则路由会短路去答反问话术。"""
    state = create_query_default_state(
        session_id="s1", original_query=SUBJECT, rewrite_hint="怎么安装"
    )
    state["answer"] = "没有识别到明确的主体……"
    assert resolve_by_hint(state) is True
    assert state["answer"] is None


def test_resolve_by_hint_returns_false_without_hint():
    """手动输入（无 hint）时必须回退常规流程，保持既有行为不变。"""
    state = create_query_default_state(
        session_id="s1", original_query="hak180怎么安装", rewrite_hint=""
    )
    assert resolve_by_hint(state) is False


def test_resolve_by_hint_falls_back_when_subject_unmatched(monkeypatch):
    """前端传的主体名不在目录里时必须回退，不能拿一个对不上的主体去检索。"""
    monkeypatch.setattr(svc, "search_by_item_names", lambda names: {"raw": names})
    monkeypatch.setattr(svc, "select_item_names", lambda result: {"confirmed_list": []})

    state = create_query_default_state(
        session_id="s1", original_query="不存在的设备名", rewrite_hint="怎么安装"
    )
    assert resolve_by_hint(state) is False
    assert state.get("rewritten_query", "") == "", "回退时不得写入半成品改写结果"


def test_confirm_item_name_short_circuits_on_hint(monkeypatch, _patched_catalog):
    """入口层：带 hint 时走确定性路径，且仍然落库本轮提问（供下轮消解）。"""
    monkeypatch.setattr(svc, "save_history_message", lambda state: None)
    monkeypatch.setattr(
        svc,
        "call_llm_item_name_and_rewritten",
        lambda *a, **k: pytest.fail("带 hint 时不应调用 LLM"),
    )

    state = create_query_default_state(
        session_id="s1", original_query=SUBJECT, rewrite_hint="烫金机如何保养"
    )
    svc.confirm_item_name(state)
    assert state["rewritten_query"] == f"{SUBJECT}如何保养"


# ==================== 请求模型 ====================

def test_query_request_schema_accepts_optional_rewrite_hint():
    """rewrite_hint 必须可选——老前端不带该字段时行为不变。"""
    from app.api.schema.query_schema import QueryRequestSchema

    assert QueryRequestSchema(query="hak180怎么安装").rewrite_hint is None
    assert (
        QueryRequestSchema(query=SUBJECT, rewrite_hint=" 怎么安装 ").rewrite_hint
        == "怎么安装"
    )
