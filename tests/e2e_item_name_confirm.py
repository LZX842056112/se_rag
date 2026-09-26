"""主体确认链路回归用例（回填 golden 覆盖不到的盲区）。

背景（为什么需要本文件）：
    golden 回归（``app/rag_eval/runner.py``）在构造查询 state 时**直接注入题库的
    ``expected_item_names``**，注释写明"避免主体识别波动影响召回评测本身"，
    即完全绕过 ``node_item_name_confirm``。因此：
      - 主体确认判定、反问轮历史闸门这两处改动，golden **不会**报警；
      - 历史上"永远反问拿不到答案"的缺陷正是这样逃逸的。
    本文件用纯逻辑用例把这些契约固定下来，另留一个依赖外部服务的集成用例。

分两类：
    1. 纯逻辑（无外部依赖，任何环境都能跑）：判定分支、state 写入契约、历史闸门；
    2. 集成（依赖 Milvus/BGE，缺失自动 skip）：真实主体名目录 + 真实检索判定。

运行：
    uv run pytest tests/e2e_item_name_confirm.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# --------------------------------------------------------------------------
# 被测模块
# --------------------------------------------------------------------------
from app.rag.query import item_name_confirm_service as svc  # noqa: E402
from app.rag.query.item_name_catalog import normalize_item_name  # noqa: E402


# --------------------------------------------------------------------------
# 1. 纯逻辑：归一化与判定分支
# --------------------------------------------------------------------------
class TestNormalize:
    def test_whitespace_and_case_insensitive(self):
        assert normalize_item_name("HAK 180 烫金机") == normalize_item_name("HAK180烫金机")
        assert normalize_item_name("  hak 180 ") == normalize_item_name("HAK180")

    def test_fullwidth_normalized(self):
        # NFKC：全角字符折叠为半角
        assert normalize_item_name("ＨＡＫ　１８０") == normalize_item_name("HAK 180")

    def test_empty(self):
        assert normalize_item_name(None) == ""
        assert normalize_item_name("   ") == ""


class TestSelectItemNames:
    """判定顺序：目录精确直通 -> 相对判定 -> 可选区间 -> 丢弃。"""

    def _patch_catalog(self, monkeypatch, available: list[str]):
        """把目录查询替换为内存字典（归一化比较，语义与真实实现一致）。"""
        table = {normalize_item_name(n): n for n in available}
        monkeypatch.setattr(
            svc, "match_catalog_exact",
            lambda name: table.get(normalize_item_name(name)),
        )

    def test_catalog_exact_with_margin_confirms(self, monkeypatch):
        self._patch_catalog(monkeypatch, ["HAK 180 烫金机", "HAK 180"])
        result = svc.select_item_names({
            "HAK 180 烫金机": [
                {"item_name": "HAK 180 烫金机", "score": 0.7879},
                {"item_name": "HAK 180", "score": 0.3374},
            ]
        })
        assert len(result["confirmed_list"]) == 1
        assert result["confirmed_list"][0]["item_name"] == "HAK 180 烫金机"
        assert result["confirmed_list"][0]["matched_by"] == "catalog_exact"
        assert result["option_list"] == []

    def test_catalog_exact_but_ambiguous_falls_back_to_asking(self, monkeypatch):
        """父型号/子型号歧义：名字精确但间距不足 -> 不能静默确认，必须反问。"""
        self._patch_catalog(monkeypatch, ["HAK 180", "HAK 180 烫金机"])
        result = svc.select_item_names({
            "HAK 180": [
                {"item_name": "HAK 180", "score": 0.655},
                {"item_name": "HAK 180 烫金机", "score": 0.6409},
            ]
        })
        assert result["confirmed_list"] == []
        assert [i["item_name"] for i in result["option_list"]] == ["HAK 180", "HAK 180 烫金机"]

    def test_relative_confirm_without_catalog_hit(self, monkeypatch):
        self._patch_catalog(monkeypatch, [])
        result = svc.select_item_names({
            "烫金机": [
                {"item_name": "HAK 180 烫金机", "score": 0.7206},
                {"item_name": "e2e", "score": 0.3344},
            ]
        })
        assert len(result["confirmed_list"]) == 1
        assert result["confirmed_list"][0]["matched_by"] == "relative"

    def test_option_band_asks_user(self, monkeypatch):
        """top1 落在可选区间（OPTION_MIN <= s < MIN）-> 反问，不确认。"""
        self._patch_catalog(monkeypatch, [])
        result = svc.select_item_names({
            "扫描仪": [
                {"item_name": "HAK 180 烫金机", "score": 0.6279},
                {"item_name": "e2e", "score": 0.346},
            ]
        })
        assert result["confirmed_list"] == []
        assert [i["item_name"] for i in result["option_list"]] == ["HAK 180 烫金机"]

    def test_below_all_thresholds_drops(self, monkeypatch):
        self._patch_catalog(monkeypatch, [])
        result = svc.select_item_names({
            "完全无关": [
                {"item_name": "HAK 180 烫金机", "score": 0.40},
                {"item_name": "e2e", "score": 0.31},
            ]
        })
        assert result["confirmed_list"] == []
        assert result["option_list"] == []

    def test_vector_miss_but_catalog_exact_still_confirms(self, monkeypatch):
        """检索失败/无命中时，目录精确同名仍应直通（度量不一致等异常下的兜底）。"""
        self._patch_catalog(monkeypatch, ["HAK 180 烫金机"])
        result = svc.select_item_names({"HAK 180 烫金机": []})
        assert [i["item_name"] for i in result["confirmed_list"]] == ["HAK 180 烫金机"]

    def test_single_candidate_has_no_ambiguity(self, monkeypatch):
        self._patch_catalog(monkeypatch, [])
        result = svc.select_item_names({
            "唯一主体": [{"item_name": "HAK 180 烫金机", "score": 0.70}],
        })
        assert len(result["confirmed_list"]) == 1


# --------------------------------------------------------------------------
# 2. 纯逻辑：state 写入契约（P1 核心）
# --------------------------------------------------------------------------
class TestApplyItemNameResult:
    def test_option_branch_writes_candidates_for_history(self):
        """P1：反问轮必须写入候选主体，否则下一轮历史为空、永远无法消解。"""
        state: dict = {"session_id": "s", "original_query": "q"}
        svc.apply_item_name_result(
            state,
            {"confirmed_list": [], "option_list": [
                {"item_name": "HAK 180", "score": 0.66},
                {"item_name": "HAK 180 烫金机", "score": 0.64},
                {"item_name": "HAK 180", "score": 0.63},  # 重复项应去重
            ]},
            "HAK 180 怎么用",
        )
        assert state["item_names"] == ["HAK 180", "HAK 180 烫金机"]
        assert state["rewritten_query"] == "HAK 180 怎么用"
        assert "请您再次确认" in state["answer"]

    def test_confirmed_branch_sets_item_names_and_clears_answer(self):
        state: dict = {"session_id": "s", "original_query": "q", "answer": "旧回答"}
        svc.apply_item_name_result(
            state,
            {"confirmed_list": [{"item_name": "HAK 180 烫金机", "score": 0.79}], "option_list": []},
            "rewritten",
        )
        assert state["item_names"] == ["HAK 180 烫金机"]
        assert state["answer"] is None
        assert state["rewritten_query"] == "rewritten"

    def test_no_candidate_branch_keeps_item_names_untouched(self):
        state: dict = {"session_id": "s", "original_query": "q"}
        svc.apply_item_name_result(state, {"confirmed_list": [], "option_list": []}, "r")
        assert "item_names" not in state
        assert "请您明确主体再提问" in state["answer"]


# --------------------------------------------------------------------------
# 3. 纯逻辑：历史闸门（反问轮落库后，下一轮应能看到"关联主体"）
# --------------------------------------------------------------------------
class _FakeHistoryRepo:
    def __init__(self):
        self.saved: list[dict] = []

    def save_message(self, **kwargs):
        self.saved.append(dict(kwargs))
        return "fake-id"

    def list_recent(self, session_id, limit=10):
        return [m for m in self.saved if m.get("session_id") == session_id][-limit:]


class TestHistoryGate:
    def test_ask_turn_is_visible_to_next_turn(self, monkeypatch):
        """反问轮落库后，历史拼接不应再返回"无有效对话记录"（死循环根因）。"""
        fake = _FakeHistoryRepo()
        monkeypatch.setattr(svc, "history_repository", fake)

        state: dict = {"session_id": "loop_sid", "original_query": "扫描仪怎么用"}
        svc.apply_item_name_result(
            state,
            {"confirmed_list": [], "option_list": [{"item_name": "HAK 180 烫金机", "score": 0.63}]},
            "扫描仪怎么用",
        )
        svc.save_history_message(state)

        assert fake.saved[-1]["item_names"] == ["HAK 180 烫金机"]
        history_text = svc.get_history_messages_and_context("loop_sid")
        assert "无有效对话记录" not in history_text
        assert "HAK 180 烫金机" in history_text

    def test_legacy_empty_item_names_still_filtered(self, monkeypatch):
        """回归保护：主体为空的消息仍按原语义过滤（不改动既有行为）。"""
        fake = _FakeHistoryRepo()
        monkeypatch.setattr(svc, "history_repository", fake)
        fake.save_message(session_id="s2", role="user", text="q", item_names=[])
        assert svc.get_history_messages_and_context("s2") == "无有效对话记录!"


# --------------------------------------------------------------------------
# 4. 文档化断言：golden 绕过主体确认链路（该盲区是既有事实）
# --------------------------------------------------------------------------
def test_golden_eval_bypasses_item_name_confirm():
    """golden 直接注入 expected_item_names，故本文件覆盖的契约需单独守住。"""
    runner_src = (ROOT / "app" / "rag_eval" / "runner.py").read_text(encoding="utf-8")
    assert 'item_names=case_data["expected_item_names"]' in runner_src


# --------------------------------------------------------------------------
# 5. 集成：真实目录 + 真实检索（缺 Milvus/BGE 自动 skip）
# --------------------------------------------------------------------------
def _milvus_ready() -> bool:
    try:
        from app.infra.vector_store.milvus_gateway import milvus_gateway
        return milvus_gateway.milvus_client.has_collection(
            collection_name=milvus_gateway.item_name_collection_name
        )
    except Exception:
        return False


@pytest.mark.skipif(not _milvus_ready(), reason="缺少 Milvus 环境，跳过主体确认集成用例")
class TestIntegration:
    def test_catalog_loads_and_exact_match(self):
        from app.rag.query.item_name_catalog import load_item_names, match_catalog_exact
        names = load_item_names(force=True)
        if not names:
            pytest.skip("kb_item_names 目录为空，跳过")
        target = names[0]
        assert match_catalog_exact(target) == target
        # 去掉空格后仍应命中（归一化容错）
        assert match_catalog_exact(target.replace(" ", "")) == target

    def test_search_and_select_on_real_data(self):
        from app.rag.query.item_name_catalog import load_item_names
        from app.rag.query.item_name_confirm_service import search_by_item_names, select_item_names

        names = load_item_names(force=True)
        if not names:
            pytest.skip("kb_item_names 目录为空，跳过")

        raw = search_by_item_names(names[:3])
        # 检索不应静默全空（metric_type 与集合索引不一致时会退化为全空）
        assert any(hits for hits in raw.values()), f"主体名检索全部为空: {raw}"

        selected = select_item_names(raw)
        assert selected["confirmed_list"], f"库内主体名本身应能确认: {selected}"
