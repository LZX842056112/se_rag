"""Source-backed answer cases. Validation uses only the Python standard library."""
from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = Path(__file__).with_name("artifacts") / "answer_quality_draft.json"


def json_digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False).encode("utf-8")).hexdigest()


def load_answer_cases(path: Path | str = DEFAULT_CASES) -> list[dict]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_answer_cases(cases: list[dict], *, project_root: Path = PROJECT_ROOT) -> dict:
    """Verify annotations and source bytes without loading models or databases."""
    if not isinstance(cases, list) or not cases:
        raise ValueError("答案题库必须是非空列表")
    ids: set[str] = set()
    documents: dict[str, str] = {}
    reviewed = 0
    references = 0
    root = project_root.resolve()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("每条样本必须是对象")
        for key in ("case_id", "query", "product", "category"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ValueError(f"样本缺少有效字段：{key}")
        if case["case_id"] in ids:
            raise ValueError("样本 ID 重复")
        ids.add(case["case_id"])
        if case.get("history") or case.get("expected_intent", "rag") != "rag":
            raise ValueError("答案题库第一版只支持单轮产品问答")
        if not isinstance(case.get("reference", ""), str):
            raise ValueError("参考答案必须是字符串")
        has_reference = bool(case.get("reference", "").strip())
        references += has_reference
        status = case.get("annotation_status")
        if status not in ("draft", "reviewed"):
            raise ValueError("复核状态必须是 draft 或 reviewed")
        if not isinstance(case.get("reviewed_by", ""), str):
            raise ValueError("复核人必须是字符串")
        if status == "reviewed":
            if not case.get("reviewed_by", "").strip() or not has_reference:
                raise ValueError("已复核样本必须填写复核人和参考答案")
            reviewed += 1
        source = case.get("source")
        if not isinstance(source, dict):
            raise ValueError("样本缺少文档出处")
        for key in ("path", "section", "excerpt", "sha256"):
            if not isinstance(source.get(key), str) or not source[key].strip():
                raise ValueError(f"出处缺少有效字段：{key}")
        page = source.get("pdf_page")
        if type(page) is not int or page < 1:
            raise ValueError("pdf_page 必须是从 1 开始的 PDF 页序号")
        relative = Path(source["path"])
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root / "doc") or not path.is_file():
            raise ValueError("出处必须是项目 doc 目录内的实际文件")
        name = relative.as_posix()
        if name not in documents:
            with path.open("rb") as stream:
                documents[name] = hashlib.file_digest(stream, "sha256").hexdigest()
        if source["sha256"] != documents[name]:
            raise ValueError(f"来源文档已变化，请重新核对参考答案：{name}")
    return {
        "case_count": len(cases), "reviewed_count": reviewed, "reference_count": references,
        "categories": dict(Counter(c["category"] for c in cases)),
        "products": dict(Counter(c["product"] for c in cases)),
        "ready_for_baseline": reviewed == len(cases),
        "dataset_sha256": json_digest(cases), "documents": dict(sorted(documents.items())),
    }
