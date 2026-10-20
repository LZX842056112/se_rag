"""The starter corpus must be source-backed and must not claim human approval."""
from app.rag_eval.answer_dataset import load_answer_cases, validate_answer_cases


def test_default_answer_corpus_covers_three_products_and_is_explicitly_draft():
    cases = load_answer_cases()
    info = validate_answer_cases(cases)
    assert info["case_count"] == 40
    assert info["reference_count"] == 40
    assert info["products"] == {"HAK180": 16, "HL3040": 12, "H3C ER2100": 12}
    assert set(info["categories"]) == {"fact", "parameter", "procedure", "safety"}
    assert info["reviewed_count"] == 0
    assert info["ready_for_baseline"] is False
    assert all(case["annotation_status"] == "draft" and not case["reviewed_by"] for case in cases)
