"""评测集读写与校验。"""
from __future__ import annotations

import json
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


def test_sample_dataset_covers_required_scenarios():
    """示例集必须覆盖正常场景与边界场景，且 case_id 唯一。"""
    path = Path(__file__).resolve().parents[2] / "app" / "rag_eval" / "datasets" / "sample_cases.json"
    cases = load_cases(path)
    tags = {tag for case in cases for tag in case.tags}
    assert len(cases) == 100
    assert len({case.case_id for case in cases}) == len(cases)
    for required in (
        "参数设置", "故障排查", "操作步骤", "维护保养",
        "边界-模糊输入", "边界-超范围", "边界-否定问题", "边界-长输入",
    ):
        assert required in tags, f"缺少场景：{required}"

    items = {name for case in cases for name in case.expected_item_names}
    assert len(items) >= 10, f"主体覆盖不足：{len(items)}"

    # 超范围与否定问题不标注切片，避免把「知识库不覆盖」误算成检索失败
    for case in cases:
        if set(case.tags) & {"边界-超范围", "边界-否定问题"}:
            assert case.gold_chunk_ids == (), f"{case.case_id} 不应标注 gold_chunk_ids"

    annotated = [case for case in cases if case.gold_chunk_ids]
    assert len(annotated) >= 80, f"可计算检索指标的题太少：{len(annotated)}"
