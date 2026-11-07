"""离线样本与线上流量的 case_id 对齐。"""


def test_alignment_requires_every_offline_case_and_online_case_id():
    from app.rag_eval.alignment import validate_alignment
    cases = [{"case_id": "c1"}, {"case_id": "c2"}]
    rows = [{"case_id": "c1"}, {"case_id": "c2"}]
    result = validate_alignment(cases, rows)
    assert result["pass"] is True
    assert result["coverage"] == 1.0
    rows.append({"case_id": ""})
    result = validate_alignment(cases, rows)
    assert result["pass"] is False
    assert any("缺少 case_id" in item for item in result["blockers"])


def test_alignment_reports_unmatched_and_missing_cases():
    from app.rag_eval.alignment import validate_alignment
    cases = [{"case_id": "c1"}, {"case_id": "c2"}]
    rows = [{"case_id": "c1"}, {"case_id": "c3"}]
    result = validate_alignment(cases, rows)
    assert result["pass"] is False
    assert result["unmatched_online_case_ids"] == ["c3"]
    assert result["missing_online_case_ids"] == ["c2"]
    assert result["matched_count"] == 1
