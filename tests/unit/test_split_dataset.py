"""评估样本测试：gold 由内容关键词推导，且评测切片出自真实切分器。"""
from __future__ import annotations

from app.rag_eval.split_dataset import (
    build_cases_from_split,
    build_eval_document,
    split_eval_document,
    split_real_artifact,
)
from app.shared.utils.paths import PROJECT_ROOT


def test_eval_slices_come_from_the_real_splitter():
    """评测切片必须带确定性 id 与父子结构——这是「切分器进入评估链路」的证据。"""
    split = split_eval_document()
    children = split["children"]
    assert children
    assert all(c["chunk_id"] for c in children)
    assert all(c["parent_id"] for c in children)
    assert all(c["heading_path"] for c in children)
    parent_ids = {p["parent_id"] for p in split["parents"]}
    assert {c["parent_id"] for c in children} <= parent_ids


def test_eval_split_is_deterministic():
    """同一文档重复切分必须得到同一批 chunk_id，否则 gold 会随导入漂移。"""
    first = [c["chunk_id"] for c in split_eval_document()["children"]]
    second = [c["chunk_id"] for c in split_eval_document()["children"]]
    assert first == second


def test_eval_document_keeps_table_and_code_block_atomic():
    """评测文档刻意包含表格与代码块，用于覆盖这两条容易被切碎的路径。"""
    blocks = split_eval_document()["blocks"]
    types = [b.type for b in blocks]
    assert "table" in types
    assert "code" in types
    table = next(b for b in blocks if b.type == "table")
    assert table.text.split("\n")[0] == "| 参数项 | 取值范围 | 默认值 | 说明 |"


def test_gold_is_derived_from_content_keywords():
    split = split_eval_document()
    cases = build_cases_from_split(split)
    all_ids = {c["chunk_id"] for c in split["children"]}

    for case in cases:
        assert case["gold_chunk_ids"], f"{case['case_id']} 的 gold 不能为空"
        assert set(case["gold_chunk_ids"]) <= all_ids
        assert set(case["must_hit_chunk_ids"]) <= set(case["gold_chunk_ids"])


def test_gold_lands_on_the_chunk_holding_the_key_fact():
    """关键事实所在切片必须被标为必命中，且该切片正文确实包含关键事实。"""
    split = split_eval_document()
    cases = {case["case_id"]: case for case in build_cases_from_split(split)}
    by_id = {c["chunk_id"]: c for c in split["children"]}

    case = cases["hak180_local_region_eval_001"]
    assert case["must_hit_chunk_ids"]
    for chunk_id in case["must_hit_chunk_ids"]:
        assert "50mm" in by_id[chunk_id]["content"]
        assert "170mm" in by_id[chunk_id]["content"]


def test_cases_do_not_depend_on_chunk_ordering():
    """gold 由内容推导：即使打乱切片顺序，标注结果也不变。"""
    split = split_eval_document()
    baseline = {c["case_id"]: sorted(c["gold_chunk_ids"]) for c in build_cases_from_split(split)}

    shuffled = dict(split)
    shuffled["children"] = list(reversed(split["children"]))
    after = {c["case_id"]: sorted(c["gold_chunk_ids"]) for c in build_cases_from_split(shuffled)}
    assert baseline == after


def test_real_artifact_split_when_available():
    """真实解析产物存在时，应能切出可用的父子结构（缺失则跳过，不影响检索评测）。"""
    split = split_real_artifact(PROJECT_ROOT)
    if split is None:
        return
    assert split["children"]
    assert all(c["heading_path"] for c in split["children"])
    assert {c["parent_id"] for c in split["children"]} <= {p["parent_id"] for p in split["parents"]}


def test_eval_document_is_non_trivial():
    assert len(build_eval_document()) > 500
