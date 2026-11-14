"""评测集读写与校验。"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from app.rag_eval.dataset import DatasetError, load_cases


def _write(tmp_path: Path, payload) -> Path:
    path = tmp_path / "cases.json"
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _case(case_id: str, **extra) -> dict:
    return {"case_id": case_id, "question": "问题", "ground_truth": "答案", **extra}


def test_load_json_array_and_parse_fields(tmp_path):
    path = _write(tmp_path, [_case("a", gold_chunk_ids=["x", "x", " "], tags=["t"])])
    cases = load_cases(path)
    assert len(cases) == 1
    assert cases[0].gold_chunk_ids == ("x",)
    assert cases[0].tags == ("t",)


def test_load_jsonl(tmp_path):
    lines = "\n".join(json.dumps(_case(case_id)) for case_id in ("a", "b"))
    path = _write(tmp_path, lines)
    assert [case.case_id for case in load_cases(path)] == ["a", "b"]


def test_duplicate_case_id_rejected(tmp_path):
    path = _write(tmp_path, [_case("a"), _case("a")])
    with pytest.raises(DatasetError, match="重复"):
        load_cases(path)


def test_missing_required_field_rejected(tmp_path):
    path = _write(tmp_path, [{"case_id": "a", "question": "问题"}])
    with pytest.raises(DatasetError, match="ground_truth"):
        load_cases(path)


def test_missing_file_rejected(tmp_path):
    with pytest.raises(DatasetError, match="不存在"):
        load_cases(tmp_path / "missing.json")


def test_invalid_json_rejected(tmp_path):
    path = _write(tmp_path, "[{")
    with pytest.raises(DatasetError, match="解析失败"):
        load_cases(path)


def test_load_behavior_expectations(tmp_path):
    path = _write(tmp_path, [_case(
        "a", expected_outcome="clarification", required_answer_terms=[" 型号 "],
        forbidden_answer_terms=["重置"],
    )])

    case = load_cases(path)[0]
    assert case.expected_outcome == "clarification"
    assert case.required_answer_terms == ("型号",)
    assert case.forbidden_answer_terms == ("重置",)


@pytest.mark.parametrize(("field", "value"), [
    ("expected_outcome", "refused"),
    ("expected_outcome", []),
    ("expected_item_names", "产品"),
    ("required_answer_terms", ["正确", " "]),
    ("forbidden_answer_terms", [4]),
])
def test_reject_malformed_behavior_annotations(tmp_path, field, value):
    path = _write(tmp_path, [_case("a", **{field: value})])

    with pytest.raises(DatasetError, match=field):
        load_cases(path)


def test_equivalent_chunks_must_belong_to_one_annotated_fact(tmp_path):
    case = _case("a", gold_chunk_ids=["first", "second"], chunk_equivalents={"first": ["another"]})
    assert load_cases(_write(tmp_path, [case]))[0].chunk_equivalents == (("first", ("another",)),)

    for invalid in ({"missing": ["another"]}, {"first": ["second"]}, {"first": ["x"], "second": ["x"]}):
        with pytest.raises(DatasetError, match="chunk_equivalents"):
            load_cases(_write(tmp_path, [{**case, "chunk_equivalents": invalid}]))


def test_sample_wireless_questions_use_the_supported_model():
    path = Path(__file__).resolve().parents[2] / "app" / "rag_eval" / "datasets" / "sample_cases.json"
    cases = {case.case_id: case for case in load_cases(path)}
    for suffix in ("071", "072", "075"):
        case = cases[f"eval_20261112_{suffix}"]
        assert "HL-3070CW" in case.question
        assert "HL-3040CN" not in case.question


def test_sample_dataset_covers_required_scenarios():
    """示例集必须覆盖正常场景与边界场景，且 case_id 唯一。"""
    path = Path(__file__).resolve().parents[2] / "app" / "rag_eval" / "datasets" / "sample_cases.json"
    cases = load_cases(path)
    tags = {tag for case in cases for tag in case.tags}
    assert len(cases) == 300
    assert len({case.case_id for case in cases}) == len(cases)
    assert len({case.question for case in cases}) == len(cases)
    assert Counter(case.expected_outcome for case in cases) == {
        "answered": 270,
        "clarification": 8,
        "handoff": 8,
        "out_of_scope": 8,
        "chitchat": 6,
    }
    for required in (
        "参数设置", "故障排查", "操作步骤", "维护保养",
        "边界-模糊输入", "边界-超范围", "边界-否定问题", "边界-长输入",
    ):
        assert required in tags, f"缺少场景：{required}"

    items = {name for case in cases for name in case.expected_item_names}
    assert len(items) == 20, f"主体数不符：{len(items)}"

    for case in cases:
        if case.expected_outcome == "answered":
            assert case.expected_item_names, case.case_id
            assert case.must_hit_chunk_ids, case.case_id
            assert set(case.must_hit_chunk_ids) <= set(case.gold_chunk_ids), case.case_id
        else:
            assert not case.gold_chunk_ids and not case.must_hit_chunk_ids, case.case_id
