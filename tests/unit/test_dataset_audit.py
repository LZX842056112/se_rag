"""评测集审计：规模、复核、答案与出处覆盖。"""
import copy
import hashlib
import json


def corpus(tmp_path, count=1):
    source = tmp_path / "doc" / "manual.pdf"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"manual")
    base = {
        "case_id": "case-1", "product": "HAK180", "category": "fact",
        "query": "HAK180 的范围是多少？", "reference": "范围是 50 到 170 mm。",
        "source": {"path": "doc/manual.pdf", "pdf_page": 1, "section": "参数",
                   "excerpt": "范围 50 到 170 mm",
                   "sha256": hashlib.sha256(b"manual").hexdigest()},
        "annotation_status": "draft", "reviewed_by": "", "history": [],
    }
    cases = []
    for index in range(count):
        case = copy.deepcopy(base)
        case["case_id"] = f"case-{index + 1}"
        case["query"] = f"HAK180 的范围问题 {index + 1}？"
        cases.append(case)
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    return tmp_path, path, cases


def test_dataset_below_reviewed_size_is_blocked(tmp_path):
    from app.rag_eval.dataset_audit import audit_answer_dataset
    root, path, cases = corpus(tmp_path, 1)
    result = audit_answer_dataset(path, min_cases=1, max_cases=1, require_reviewed=True, project_root=root)
    assert result["pass"] is False
    assert any("已复核" in item for item in result["blockers"])
    cases[0].update(annotation_status="reviewed", reviewed_by="reviewer")
    path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    result = audit_answer_dataset(path, min_cases=1, max_cases=1, require_reviewed=True, project_root=root)
    assert result["pass"] is True


def test_dataset_size_and_duplicate_queries_are_blocked(tmp_path):
    from app.rag_eval.dataset_audit import audit_answer_dataset
    root, path, cases = corpus(tmp_path, 2)
    for case in cases:
        case.update(annotation_status="reviewed", reviewed_by="reviewer")
    result = audit_answer_dataset(path, min_cases=3, max_cases=5, require_reviewed=True, project_root=root)
    assert result["pass"] is False
    assert any("少于下限" in item for item in result["blockers"])

    cases[1]["query"] = cases[0]["query"]
    path.write_text(json.dumps(cases, ensure_ascii=False), encoding="utf-8")
    result = audit_answer_dataset(path, min_cases=1, max_cases=5, require_reviewed=True, project_root=root)
    assert result["pass"] is False
    assert any("重复问题" in item for item in result["blockers"])
