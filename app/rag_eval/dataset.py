"""评测集：带标准答案与出处的样本读写与校验。

支持两种输入：
- JSON 数组（推荐，便于人工编辑）；
- JSONL（一行一条，便于流式生成）。

必填字段：``case_id``、``question``、``ground_truth``。
检索指标需要 ``gold_chunk_ids``；关键内容可另填 ``must_hit_chunk_ids``。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


class DatasetError(ValueError):
    """评测集格式错误。"""


@dataclass(frozen=True)
class EvalCase:
    """单条评测样本。"""

    case_id: str
    question: str
    ground_truth: str
    expected_item_names: tuple[str, ...] = field(default_factory=tuple)
    gold_chunk_ids: tuple[str, ...] = field(default_factory=tuple)
    must_hit_chunk_ids: tuple[str, ...] = field(default_factory=tuple)
    tags: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, raw: dict) -> "EvalCase":
        if not isinstance(raw, dict):
            raise DatasetError("每条样本必须是对象")
        case_id = str(raw.get("case_id") or "").strip()
        question = str(raw.get("question") or "").strip()
        ground_truth = str(raw.get("ground_truth") or "").strip()
        if not case_id:
            raise DatasetError("样本缺少 case_id")
        if not question:
            raise DatasetError(f"样本 {case_id} 缺少 question")
        if not ground_truth:
            raise DatasetError(f"样本 {case_id} 缺少 ground_truth")
        return cls(
            case_id=case_id,
            question=question,
            ground_truth=ground_truth,
            expected_item_names=_str_tuple(raw.get("expected_item_names")),
            gold_chunk_ids=_str_tuple(raw.get("gold_chunk_ids")),
            must_hit_chunk_ids=_str_tuple(raw.get("must_hit_chunk_ids")),
            tags=_str_tuple(raw.get("tags")),
        )


def _str_tuple(value: object) -> tuple[str, ...]:
    """把列表字段转成去重、去空白的字符串元组。"""
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def load_cases(path: Path) -> list[EvalCase]:
    """读取评测集；文件不存在或格式错误时抛出 ``DatasetError``。"""
    if not path.is_file():
        raise DatasetError(f"评测集不存在：{path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise DatasetError(f"评测集为空：{path}")

    raw_items: list[dict]
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise DatasetError(f"评测集 JSON 解析失败：{exc}") from exc
        if not isinstance(parsed, list):
            raise DatasetError("评测集 JSON 顶层必须是数组")
        raw_items = parsed
    else:
        raw_items = []
        for line_no, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                raw_items.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise DatasetError(f"评测集第 {line_no} 行 JSON 解析失败：{exc}") from exc

    cases = [EvalCase.from_dict(item) for item in raw_items]
    if not cases:
        raise DatasetError(f"评测集没有有效样本：{path}")
    _validate_unique(cases)
    return cases


def _validate_unique(cases: list[EvalCase]) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.case_id in seen:
            raise DatasetError(f"case_id 重复：{case.case_id}")
        seen.add(case.case_id)
