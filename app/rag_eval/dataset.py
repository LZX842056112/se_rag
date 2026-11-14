"""评测集：带标准答案与出处的样本读写与校验。

支持两种输入：
- JSON 数组（推荐，便于人工编辑）；
- JSONL（一行一条，便于流式生成）。

必填字段：``case_id``、``question``、``ground_truth``。
检索指标需要 ``gold_chunk_ids``；关键内容可另填 ``must_hit_chunk_ids``。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

OUTCOMES = ("answered", "clarification", "handoff", "chitchat", "out_of_scope")


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
    expected_outcome: str | None = None
    required_answer_terms: tuple[str, ...] = field(default_factory=tuple)
    forbidden_answer_terms: tuple[str, ...] = field(default_factory=tuple)
    chunk_equivalents: tuple[tuple[str, tuple[str, ...]], ...] = field(default_factory=tuple)

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
        expected_outcome = raw.get("expected_outcome")
        if "expected_outcome" in raw and expected_outcome not in OUTCOMES:
            raise DatasetError(f"样本 {case_id} 的 expected_outcome 必须是 {', '.join(OUTCOMES)} 之一")
        gold = _str_tuple(raw.get("gold_chunk_ids"))
        must_hit = _str_tuple(raw.get("must_hit_chunk_ids"))
        return cls(
            case_id=case_id,
            question=question,
            ground_truth=ground_truth,
            expected_item_names=_behavior_terms(raw, "expected_item_names", case_id),
            gold_chunk_ids=gold,
            must_hit_chunk_ids=must_hit,
            tags=_str_tuple(raw.get("tags")),
            expected_outcome=expected_outcome,
            required_answer_terms=_behavior_terms(raw, "required_answer_terms", case_id),
            forbidden_answer_terms=_behavior_terms(raw, "forbidden_answer_terms", case_id),
            chunk_equivalents=_chunk_equivalents(raw.get("chunk_equivalents", {}), set(gold) | set(must_hit), case_id),
        )


def _chunk_equivalents(raw: object, annotated: set[str], case_id: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    error = f"样本 {case_id} 的 chunk_equivalents 必须将已标注切片映射到互不重叠的等效切片"
    if not isinstance(raw, dict):
        raise DatasetError(error)
    claimed = set(annotated)
    groups = []
    for primary, alternatives in raw.items():
        if primary not in annotated or not isinstance(alternatives, list) or not alternatives:
            raise DatasetError(error)
        if any(not isinstance(value, str) or not value.strip() for value in alternatives):
            raise DatasetError(error)
        ids = tuple(value.strip() for value in alternatives)
        if claimed.intersection(ids) or len(set(ids)) != len(ids):
            raise DatasetError(error)
        claimed.update(ids)
        groups.append((primary, ids))
    return tuple(groups)


def _behavior_terms(raw: dict, key: str, case_id: str) -> tuple[str, ...]:
    values = raw.get(key, [])
    if not isinstance(values, list) or any(not isinstance(value, str) or not value.strip() for value in values):
        raise DatasetError(f"样本 {case_id} 的 {key} 必须是非空字符串组成的列表")
    return _str_tuple(values)


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
    return load_dataset(path)[0]


def load_dataset(path: Path) -> tuple[list[EvalCase], str]:
    """读取评测集并计算同一份文件内容的 SHA-256。"""
    if not path.is_file():
        raise DatasetError(f"评测集不存在：{path}")
    data = path.read_bytes()
    text = data.decode("utf-8-sig").strip()
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
    return cases, hashlib.sha256(data).hexdigest()


def _validate_unique(cases: list[EvalCase]) -> None:
    seen: set[str] = set()
    for case in cases:
        if case.case_id in seen:
            raise DatasetError(f"case_id 重复：{case.case_id}")
        seen.add(case.case_id)
