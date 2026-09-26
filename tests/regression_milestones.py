"""
M0→M2 逐里程碑回归基线 harness。

两类测试：
1. infra 回归（依赖 Milvus/Mongo/BGE 模型，缺依赖自动 skip）：
   - M0 evolution 关闭 == baseline
   - M1 evolution 开启 + 空 kb_evolution_items == baseline（空集无侵入）
   - M2 参数自调后（param_registry 清空回退常量）== baseline（黄金不劣化）
2. 纯逻辑单测（不依赖外部服务，仅需 pymongo+mongomock 或轻量模块）：
   - M2 controller 止损回滚（mock Mongo）
   - M0 citations 来源回填逻辑
   - 配置 getters 在 evolution 关闭/registry 空时回退常量

运行：`python -m pytest tests/regression_milestones.py -k 'milestone or m0 or m1 or m2'`
本文件模块顶层不做重依赖导入，保证逻辑单测可独立收集运行。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# 项目根（测试位于 <root>/tests/）
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

ARTIFACTS_DIR = ROOT / "app" / "rag_eval" / "artifacts"
BASELINE_FILE = ARTIFACTS_DIR / "regression_baseline_m0.json"
TOL = 0.02


# --------------------------------------------------------------------------
# 纯工具（不导入重模块）
# --------------------------------------------------------------------------
def _set_evolution(flag: bool) -> None:
    """运行期切换自进化开关（模块级 dataclass 实例，可变字段）。"""
    from app.evolution.config import evolution_config
    evolution_config.enabled = flag


def _compare(base: dict, new: dict, tol: float = TOL) -> dict:
    """逐指标比 diff，返回 {item_name_hit_rate, layers{...}, pass}。"""
    bl = base.get("layers", {})
    nl = new.get("layers", {})
    all_pass = True
    dims: dict = {}
    delta = round(abs(new.get("avg_item_name_hit_rate", 0.0) - base.get("avg_item_name_hit_rate", 0.0)), 4)
    dims["item_name_hit_rate"] = delta
    if delta > tol:
        all_pass = False
    dims["layers"] = {}
    for layer, bb in bl.items():
        nn = nl.get(layer, {})
        ld = {}
        for metric in ("avg_precision", "avg_recall", "avg_must_hit_rate"):
            d = round(abs(nn.get(metric, 0.0) - bb.get(metric, 0.0)), 4)
            ld[metric] = d
            if d > tol:
                all_pass = False
        dims["layers"][layer] = ld
    dims["pass"] = all_pass
    return dims


def _save_report(tag: str, summary: dict, diffs: dict | None = None) -> Path:
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    path = ARTIFACTS_DIR / f"regression_{tag}.json"
    path.write_text(
        json.dumps({"tag": tag, "summary": summary, "diff_vs_baseline": diffs},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def _load_baseline() -> dict | None:
    if BASELINE_FILE.exists():
        data = json.loads(BASELINE_FILE.read_text(encoding="utf-8"))
        return data.get("summary")
    return None


# --------------------------------------------------------------------------
# infra 就绪探测（缺依赖整体降级为 skip，不报错）
# --------------------------------------------------------------------------
def _try_infra() -> bool:
    try:
        import pymongo  # noqa: F401
        import pymilvus  # noqa: F401
        from app.rag_eval.runner import batch_eval_ready, milvus_ready
        return bool(batch_eval_ready() and milvus_ready())
    except Exception:
        return False


_INFRA = _try_infra()


# --------------------------------------------------------------------------
# M0/M1/M2 infra 回归
# --------------------------------------------------------------------------
@pytest.mark.skipif(not _INFRA, reason="缺少 Milvus/Mongo/BGE 运行环境，跳过 infra 回归")
class TestMilestoneInfraRegression:
    """黄金回归：统一在可控状态下与 M0 baseline 对比（±2%）。"""

    def _insert_once(self) -> None:
        """评测数据只入库一次（题库文件已存在即复用）。"""
        from app.rag_eval import RagEvalTester
        from app.rag_eval.runner import close_mongo_client
        try:
            RagEvalTester().run_insert_test_data()
        finally:
            close_mongo_client()

    def _run_summary(self) -> dict:
        from app.rag_eval.runner import run_batch_eval, close_mongo_client
        try:
            return run_batch_eval()["summary"]
        finally:
            close_mongo_client()

    def _baseline_or_build(self, monkeypatch) -> dict:
        base = _load_baseline()
        if base is None:
            _set_evolution(False)
            self._insert_once()
            base = self._run_summary()
            _save_report("baseline_m0", base)
        return base

    def test_m0_baseline_regression_off(self, monkeypatch):
        base = self._baseline_or_build(monkeypatch)
        _set_evolution(False)
        new = self._run_summary()
        diffs = _compare(base, new)
        _save_report("m0_off", new, diffs)
        assert diffs["pass"], f"M0 回归超差: {diffs}"

    def test_m1_empty_collection_regression(self, monkeypatch):
        base = self._baseline_or_build(monkeypatch)
        _set_evolution(True)  # 空 kb_evolution_items → search_evolution_items 返回 []
        new = self._run_summary()
        diffs = _compare(base, new)
        _save_report("m1_empty", new, diffs)
        assert diffs["pass"], f"M1 空集合回归超差: {diffs}"
        _set_evolution(False)  # 复位，避免污染后续测试

    def test_m2_clear_params_regression(self, monkeypatch):
        base = self._baseline_or_build(monkeypatch)
        from app.evolution.repositories import get_evolution_mongo_tool
        try:
            get_evolution_mongo_tool().param_registry.delete_many({})
        except Exception:
            pass
        _set_evolution(False)  # 黄金回归走纯旁路
        new = self._run_summary()
        diffs = _compare(base, new)
        _save_report("m2_clear", new, diffs)
        assert diffs["pass"], f"M2 清参后黄金回归超差: {diffs}"


# --------------------------------------------------------------------------
# 纯逻辑单测（沙箱可跑，不依赖外部服务）
# --------------------------------------------------------------------------
def test_config_getters_fallback_without_evolution():
    """evolution 关闭 / registry 空时，getters 回退常量，读侧行为中性。"""
    from app.rag.query.config import get_rrf_k, get_rrf_top, get_rerank_topk
    from app.evolution.config import evolution_config

    evolution_config.enabled = False
    assert get_rrf_k() == 60
    assert get_rrf_top() == 5
    assert get_rerank_topk() == 6


def test_m0_feedback_citations_logic():
    """citations 来源回填：evo_doc_id → source=evolution，其余 → kb。"""
    try:
        from app.api.http.query_server import _build_citations
    except Exception as e:  # 查询服务依赖不可用（如缺 pymongo）则跳过
        pytest.skip(f"query_server 不可导入，跳过 citations 单测: {e}")
    from app.process.query.agent.state import create_query_default_state

    st = create_query_default_state(session_id="s", original_query="q")
    st["cited_chunk_ids"] = ["kb_1", "evo_abc"]
    st["faq_evo_ids"] = ["evo_abc"]
    cits = _build_citations(st)
    by_id = {c.faq_id: c.source for c in cits}
    assert by_id["evo_abc"] == "evolution"
    assert by_id["kb_1"] == "kb"


def test_m2_controller_rollback(monkeypatch):
    """坏条目→采纳率骤降→ controller 止损回退 RRF_K/RRF_TOP/RERANK_TOP_K 到 _MIN。"""
    mongomock = pytest.importorskip("mongomock")
    import time

    from app.evolution.config import evolution_config
    from app.evolution.tuning import controller, param_registry as pr_mod
    from app.evolution.online_eval import metrics as metrics_mod
    from app.evolution import repositories as repo_mod

    db = mongomock.MongoClient()["evo"]
    fake = object.__new__(repo_mod.EvolutionMongoTool)
    fake.fb_events = db["fb_events"]
    fake.k_gaps = db["k_gaps"]
    fake.k_metrics = db["k_metrics"]
    fake.param_registry = db["param_registry"]
    fake.param_registry.create_index([("key", 1)], unique=True)
    fake.get_param = lambda key: fake.param_registry.find_one({"key": key})

    def _set(key, value, updated_by="manual"):
        ex = fake.param_registry.find_one({"key": key})
        rev = (ex or {}).get("rev", 0) + 1
        fake.param_registry.update_one(
            {"key": key},
            {"$set": {"value": value, "updated_at": time.time(),
                      "updated_by": updated_by, "rev": rev}},
            upsert=True,
        )

    fake.set_param = _set

    # 各消费模块打桩（避免首次 import 绑定问题）
    monkeypatch.setattr(metrics_mod, "get_evolution_mongo_tool", lambda: fake)
    monkeypatch.setattr(controller, "get_evolution_mongo_tool", lambda: fake)
    monkeypatch.setattr(pr_mod, "get_evolution_mongo_tool", lambda: fake)
    monkeypatch.setattr(pr_mod, "_CACHE", {})
    monkeypatch.setattr(pr_mod, "_LAST_LOAD", 0.0)

    monkeypatch.setattr(evolution_config, "enabled", True)

    ts = time.time()
    for _ in range(3):
        fake.k_metrics.insert_one({"ts": ts, "adopt_rate": 0.9, "gap_rate": 0.0, "params_snapshot": {}})
    fake.fb_events.insert_many([
        {"session_id": f"bad{i}", "adopt": False, "thumbs": -1,
         "cited_chunk_ids": [], "source": "kb", "ts": ts}
        for i in range(10)
    ])

    changed = controller.adjust_step()
    assert changed is True
    for key, low in controller._MIN.items():
        assert pr_mod.get_param(key) == low, f"{key} 未回退到低位 {low}"
    rec = fake.param_registry.find_one({"key": "RRF_K"})
    assert rec["rev"] >= 1
    assert rec["updated_by"] == "metric"