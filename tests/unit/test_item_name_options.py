"""「没确认到主体时给出相似主体供点选」的判定与状态测试。

真实场景：用户只输入型号前缀 ``hak180``，主体向量分只有 0.409（低于可选阈值 0.60），
旧实现直接丢弃候选、只回一句「请您明确主体再提问」；其实库内只有
``Brother HAK 180 烫金机``，应当直接列出来让用户点选。
"""
from __future__ import annotations

import pytest

from app.process.query.agent.state import create_query_default_state
from app.rag.item_name import catalog as catalog_module
from app.rag.item_name.match import select_item_names
from app.rag.query.item_name_confirm_service import apply_item_name_result


@pytest.fixture
def catalog(monkeypatch):
    """把主体目录替换成固定集合，避免真实连 Milvus。"""
    monkeypatch.setattr(catalog_module, "_CACHE", {"names": ["Brother HAK 180 烫金机"], "raws": ["Brother HAK 180 烫金机"], "ts": 9e9})
    return ["Brother HAK 180 烫金机"]


def test_find_similar_names_matches_model_prefix(catalog):
    """型号前缀 ``hak180`` 应能匹配到 ``Brother HAK 180 烫金机``。"""
    found = catalog_module.find_similar_names("hak180")
    assert found == [("Brother HAK 180 烫金机", "catalog_contains")]


def test_find_similar_names_ignores_too_short_noise(catalog):
    """过短关键词不能把整库都算成相似，但 3 字符以上的型号片段应当能匹配。"""
    assert catalog_module.find_similar_names("机") == []
    # “180”是型号片段（3 字符，落在允许下限），应当给出候选供用户确认
    assert catalog_module.find_similar_names("180") == [("Brother HAK 180 烫金机", "catalog_contains")]


def test_select_item_names_returns_similar_instead_of_dropping(catalog):
    """低分（0.409）时不再直接丢弃，而是给出相似主体供点选。"""
    milvus_result = {"hak180": [{"item_name": "Brother HAK 180 烫金机", "score": 0.4093}]}
    result = select_item_names(milvus_result)
    assert result["confirmed_list"] == []
    assert result["option_list"] == []
    names = [item["item_name"] for item in result["similar_list"]]
    assert names == ["Brother HAK 180 烫金机"]
    assert result["similar_list"][0]["matched_by"] == "catalog_contains"


def test_select_item_names_falls_back_to_small_catalog(monkeypatch):
    """小知识库（≤5 条）连相似都算不上时，直接列出目录，避免只给一句“请明确主体”。"""
    monkeypatch.setattr(catalog_module, "_CACHE",
                        {"names": ["A型号", "B型号"], "raws": ["A型号", "B型号"], "ts": 9e9})
    result = select_item_names({"完全无关的词": []})
    assert [item["item_name"] for item in result["similar_list"]] == ["A型号", "B型号"]
    assert all(item["matched_by"] == "catalog_all" for item in result["similar_list"])


def test_apply_item_name_result_exposes_options_and_guide_text(catalog):
    state = create_query_default_state(session_id="s1", original_query="hak180")
    list_dict = {
        "confirmed_list": [],
        "option_list": [],
        "similar_list": [{"item_name": "Brother HAK 180 烫金机", "score": None, "matched_by": "catalog_contains"}],
    }
    apply_item_name_result(state, list_dict, "hak180")

    assert state["item_name_options"] == list_dict["similar_list"]
    assert state["item_names"] == ["Brother HAK 180 烫金机"]  # 落库历史，供下一轮消解
    assert "请点击下方主体直接提问" in state["answer"]
    assert state["answer"] != "本次问题没有关联到任何主体，也没有找到相似主体。请补充产品名称后再提问。"


def test_apply_item_name_result_clears_options_when_confirmed(catalog):
    state = create_query_default_state(session_id="s1", original_query="HAK 180 烫金机 的功率？")
    state["item_name_options"] = [{"item_name": "旧选项"}]
    apply_item_name_result(state, {"confirmed_list": [{"item_name": "Brother HAK 180 烫金机"}],
                                   "option_list": [], "similar_list": []}, "HAK 180 烫金机 的功率？")
    assert state["item_names"] == ["Brother HAK 180 烫金机"]
    assert state["item_name_options"] == []
    assert state.get("answer") is None


def test_feedback_payload_carries_item_names():
    """前端点踩需回传主体名，保证缺口/候选的 item_names 贯通（与用户改动一致）。"""
    from pathlib import Path

    source = (Path(__file__).resolve().parents[2] / "app" / "resources" / "js" / "chat.js").read_text(encoding="utf-8")
    assert "item_names: meta.itemNames || []" in source


def test_find_names_mentioned_in_query(catalog):
    """回归 D17：问句里出现库内主体名的关键词（「烫金机」）也要能找到该主体。"""
    found = catalog_module.find_names_mentioned_in("烫金机怎么安装")
    assert found == [("Brother HAK 180 烫金机", "catalog_mentioned")]


def test_find_names_mentioned_in_ignores_unrelated_question(catalog):
    """问句里没有库内关键词时不得硬凑候选。"""
    assert catalog_module.find_names_mentioned_in("今天天气怎么样") == []
    assert catalog_module.find_names_mentioned_in("") == []


def test_similar_from_query_prefers_mention_then_small_catalog(monkeypatch):
    """没抽出主体时的兜底链：问句关键词 → 小目录全列。"""
    from app.rag.item_name import match as match_module

    monkeypatch.setattr(catalog_module, "_CACHE",
                        {"names": ["Brother HAK 180 烫金机"], "raws": ["Brother HAK 180 烫金机"], "ts": 9e9})
    monkeypatch.setattr(match_module, "load_item_names", lambda: ["Brother HAK 180 烫金机"])

    options = match_module.similar_from_query("烫金机怎么安装")
    assert [item["item_name"] for item in options] == ["Brother HAK 180 烫金机"]
    assert options[0]["matched_by"] == "catalog_mentioned"

    # 问句完全无关时，小知识库（≤5 条）直接列目录，仍给用户可点的出路
    options = match_module.similar_from_query("今天天气怎么样")
    assert [item["matched_by"] for item in options] == ["catalog_all"]


def test_confirm_item_name_offers_options_when_no_name_extracted(monkeypatch):
    """回归 D17：模型返回空 item_names 时，也要按问句给出相似主体（不再只说“请补充产品名称”）。"""
    from app.rag.item_name import match as match_module
    from app.rag.query import item_name_confirm_service as service
    from app.shared.clients import history_repository as history_module

    monkeypatch.setattr(catalog_module, "_CACHE",
                        {"names": ["Brother HAK 180 烫金机"], "raws": ["Brother HAK 180 烫金机"], "ts": 9e9})
    monkeypatch.setattr(match_module, "load_item_names", lambda: ["Brother HAK 180 烫金机"])
    monkeypatch.setattr(service, "get_history_messages_and_context", lambda session_id: "")
    monkeypatch.setattr(
        service, "call_llm_item_name_and_rewritten",
        lambda history_text, original_query: {"item_names": [], "rewritten_query": "烫金机怎么安装"},
    )
    monkeypatch.setattr(history_module.history_repository, "save_message", lambda **kwargs: None)

    state = create_query_default_state(session_id="s1", original_query="烫金机怎么安装")
    result = service.confirm_item_name(state)

    assert result["item_name_options"], "没抽出主体时必须给出可点选主体"
    assert result["item_name_options"][0]["item_name"] == "Brother HAK 180 烫金机"
    assert "请点击下方主体直接提问" in result["answer"]
