"""
M0→M2 逐里程碑回归基线 driver（单进程运行，模型只加载一次）。

从 pytest 独立运行：`python -B tests/_run_regression_milestones.py`
包含防重入库：评测数据已存在则跳过（insert_batch_eval_dataset 非幂等）。
输出：app/rag_eval/artifacts/regression_{baseline_m0,m0_off,m1_empty,m2_clear}.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

ART = ROOT / "app" / "rag_eval" / "artifacts"
ART.mkdir(parents=True, exist_ok=True)
BASELINE_FILE = ART / "regression_baseline_m0.json"
TOL = 0.02


def set_evolution(flag: bool) -> None:
    from app.evolution.config import evolution_config
    evolution_config.enabled = flag


def run_summary() -> dict:
    from app.rag_eval.runner import run_batch_eval
    return run_batch_eval()["summary"]


def close_mongo() -> None:
    try:
        from app.rag_eval.runner import close_mongo_client
        close_mongo_client()
    except Exception:
        pass


def insert_once_if_missing() -> None:
    """评测数据已存在则跳过；数量异常不完整则重入库；数量超期望则中止。"""
    from app.rag_eval.dataset import TEST_FILE_TITLE, build_import_chunks
    from app.infra.vector_store.milvus_gateway import milvus_gateway
    from app.rag_eval.runner import milvus_ready

    if not milvus_ready():
        print("[insert] Milvus 不可用，跳过入库，将尝试直接评测。")
        return
    expected = len(build_import_chunks())
    rows = milvus_gateway.client.query(
        collection_name=milvus_gateway.chunk_collection_name,
        filter=f"file_title == '{TEST_FILE_TITLE}'",
        output_fields=["chunk_id"],
        limit=16384,
    )
    if len(rows) == expected:
        print(f"[insert] 评测数据已存在（{len(rows)} 条），跳过入库。")
        return
    if len(rows) > expected:
        raise SystemExit(
            f"[insert] {TEST_FILE_TITLE} 下存在 {len(rows)} 条，期望 {expected} 条，"
            "数量异常，请人工核查后重跑。"
        )
    print(f"[insert] 评测数据不完整（{len(rows)}/{expected}），重新入库…")
    from app.rag_eval import RagEvalTester
    r = RagEvalTester().run_insert_test_data()
    print(f"[insert] 入库完成，case_count={r.get('case_count')}")
    close_mongo()


def clear_evolution_params() -> None:
    from app.evolution.repositories import get_evolution_mongo_tool
    tool = get_evolution_mongo_tool()
    tool.param_registry.delete_many({"key": {"$in": ["RRF_K", "RRF_TOP", "RERANK_TOP_K"]}})
    print("[m2] 已清空 param_registry 的 RRF_K/RRF_TOP/RERANK_TOP_K（读侧回退常量）。")


def compare(base: dict, new: dict, tol: float = TOL) -> dict:
    all_pass = True
    bl, nl = base.get("layers", {}), new.get("layers", {})
    dims = {"item_name_hit_rate": round(abs(new.get("avg_item_name_hit_rate", 0) - base.get("avg_item_name_hit_rate", 0)), 4)}
    if dims["item_name_hit_rate"] > tol:
        all_pass = False
    dims["layers"] = {}
    for layer, bb in bl.items():
        nn = nl.get(layer, {})
        ld = {}
        for m in ("avg_precision", "avg_recall", "avg_must_hit_rate"):
            d = round(abs(nn.get(m, 0) - bb.get(m, 0)), 4)
            ld[m] = d
            if d > tol:
                all_pass = False
        dims["layers"][layer] = ld
    dims["pass"] = all_pass
    return dims


def save(tag: str, summary: dict, diffs: dict | None = None) -> Path:
    p = ART / f"regression_{tag}.json"
    p.write_text(json.dumps({"tag": tag, "summary": summary, "diff_vs_baseline": diffs}, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def load_baseline() -> dict | None:
    if BASELINE_FILE.exists():
        return json.loads(BASELINE_FILE.read_text(encoding="utf-8")).get("summary")
    return None


def main() -> int:
    insert_once_if_missing()

    # ---- M0 基线（evolution 关闭）----
    set_evolution(False)
    base = load_baseline()
    if base is None:
        print("[M0] 首次运行，建立 M0 基线…")
        base = run_summary()
        save("baseline_m0", base)
        close_mongo()
    else:
        print(f"[M0] 复用已有基线：cases={base.get('case_count')}")
    print("[M0] 基线概要:")
    print(json.dumps(base.get("layers", {}), ensure_ascii=False))

    # ---- M0 复跑：evolution 关闭 == baseline ----
    set_evolution(False)
    new_m0 = run_summary()
    save("m0_off", new_m0, compare(base, new_m0))
    close_mongo()

    # ---- M1：evolution 开启 + 空 kb_evolution_items == baseline ----
    set_evolution(True)
    new_m1 = run_summary()
    save("m1_empty", new_m1, compare(base, new_m1))
    close_mongo()
    set_evolution(False)

    # ---- M2：evolution 关闭 + 清参 == baseline ----
    clear_evolution_params()
    set_evolution(False)
    new_m2 = run_summary()
    save("m2_clear", new_m2, compare(base, new_m2))
    close_mongo()

    # ---- 汇总 ----
    print("\n================ M0→M2 回归汇总 ================")
    ok = True
    for tag in ("m0_off", "m1_empty", "m2_clear"):
        data = json.loads((ART / f"regression_{tag}.json").read_text(encoding="utf-8"))
        diffs = data["diff_vs_baseline"]
        status = "PASS" if diffs["pass"] else "FAIL"
        if not diffs["pass"]:
            ok = False
        print(f"[{tag}] {status}  主体命中率delta={diffs['item_name_hit_rate']}")
        for layer, ld in diffs["layers"].items():
            print(f"        {layer}: {ld}")
    print("\n结论:", "全部通过（≤2%）" if ok else "存在超差，需排查")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        close_mongo()