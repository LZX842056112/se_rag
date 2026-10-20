"""Answer evaluation contracts, with no external model or database calls."""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def corpus(tmp_path):
    source = tmp_path / "doc" / "manual.pdf"
    source.parent.mkdir()
    source.write_bytes(b"manual-v1")
    case = {
        "case_id": "one", "product": "HAK 180", "category": "parameter",
        "query": "HAK 180 的范围是多少？", "reference": "范围是 50 到 170 mm。",
        "source": {"path": "doc/manual.pdf", "pdf_page": 1, "section": "设置",
                   "sha256": hashlib.sha256(b"manual-v1").hexdigest(), "excerpt": "范围 50 到 170 mm"},
        "annotation_status": "draft", "reviewed_by": "",
    }
    return tmp_path, [case]


def test_dataset_drafts_are_not_a_trusted_baseline(corpus):
    from app.rag_eval.answer_dataset import validate_answer_cases
    root, cases = corpus
    info = validate_answer_cases(cases, project_root=root)
    assert info["case_count"] == 1
    assert info["reference_count"] == 1
    assert info["ready_for_baseline"] is False
    cases[0].update(annotation_status="reviewed", reviewed_by="engineer")
    assert validate_answer_cases(cases, project_root=root)["ready_for_baseline"] is True


@pytest.mark.parametrize("change", ["duplicate", "empty_query", "invalid_page", "wrong_digest", "outside", "fake_review"])
def test_dataset_rejects_unusable_annotations(corpus, change):
    from app.rag_eval.answer_dataset import validate_answer_cases
    root, cases = corpus
    if change == "duplicate": cases.append(copy.deepcopy(cases[0]))
    if change == "empty_query": cases[0]["query"] = " "
    if change == "invalid_page": cases[0]["source"]["pdf_page"] = 0
    if change == "wrong_digest": cases[0]["source"]["sha256"] = "0" * 64
    if change == "outside": cases[0]["source"]["path"] = "../outside.pdf"
    if change == "fake_review": cases[0]["annotation_status"] = "reviewed"
    with pytest.raises(ValueError): validate_answer_cases(cases, project_root=root)


def test_collection_uses_real_question_and_unique_sessions_only(corpus):
    from app.rag_eval.answer_collection import collect_answers
    _, cases = corpus
    cases.append({**cases[0], "case_id": "two"})
    sessions = []
    def invoke(*, session_id, original_query, is_stream):
        assert original_query == "HAK 180 的范围是多少？"
        assert is_stream is False
        sessions.append(session_id)
        return {"intent": "rag", "answer": "范围是 50 到 170 mm。",
                "answer_contexts": ["实际父章节和子块"], "metrics": {"llm_calls": 2}}
    rows = collect_answers(cases, invoke)
    assert len(set(sessions)) == 2
    assert all(s.startswith("eval-") for s in sessions)
    assert rows[0]["retrieved_contexts"] == ["实际父章节和子块"]
    assert rows[0]["response"] == "范围是 50 到 170 mm。"


def test_collection_continues_after_failed_query(corpus):
    from app.rag_eval.answer_collection import collect_answers
    _, cases = corpus
    cases.append({**cases[0], "case_id": "two"})
    def invoke(**kwargs): raise TimeoutError("private API token must not leak")
    rows = collect_answers(cases, invoke)
    assert len(rows) == 2
    assert rows[0]["error"] == "TimeoutError"
    assert "private API" not in json.dumps(rows)


class FixedMetric:
    def __init__(self, value): self.value = value
    async def ascore(self, **kwargs):
        if isinstance(self.value, Exception): raise self.value
        return SimpleNamespace(value=self.value)


def row(case_id="one", **changes):
    return {"case_id": case_id, "query": "HAK 180 的范围是多少？", "actual_intent": "rag",
            "response": "范围是 50 到 170 mm。", "retrieved_contexts": ["50 到 170 mm"],
            "error": None, **changes}


def test_zero_score_is_valid_and_not_confused_with_judge_error(corpus):
    from app.rag_eval.answer_scoring import score_snapshots
    _, cases = corpus
    report = score_snapshots(cases, [row()], {
        "faithfulness": FixedMetric(0.0), "answer_relevancy": FixedMetric(.75),
        "factual_correctness": FixedMetric(ValueError("secret")),
    })
    assert report["summary"]["metrics"]["faithfulness"] == {"mean": 0.0, "valid_count": 1, "coverage": 1.0}
    assert report["summary"]["metrics"]["factual_correctness"]["mean"] is None
    assert report["results"][0]["metrics"]["factual_correctness"]["reason"] == "ValueError"
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -2, 2, True, "0.8", None])
def test_invalid_scores_are_null_with_a_reason(corpus, value):
    from app.rag_eval.answer_scoring import score_snapshots
    _, cases = corpus
    report = score_snapshots(cases, [row()], {name: FixedMetric(value) for name in (
        "faithfulness", "answer_relevancy", "factual_correctness")})
    result = report["results"][0]["metrics"]["faithfulness"]
    assert result == {"value": None, "status": "error", "reason": "invalid_score"}
    json.dumps(report, allow_nan=False)


def test_empty_evidence_and_missing_reference_do_not_invent_scores(corpus):
    from app.rag_eval.answer_scoring import score_snapshots
    _, cases = corpus
    cases[0]["reference"] = ""
    metrics = {name: FixedMetric(.8) for name in ("faithfulness", "answer_relevancy", "factual_correctness")}
    report = score_snapshots(cases, [row(retrieved_contexts=[])], metrics)
    scores = report["results"][0]["metrics"]
    assert scores["faithfulness"]["reason"] == "missing_contexts"
    assert scores["factual_correctness"]["reason"] == "missing_reference"
    assert scores["answer_relevancy"]["value"] == .8


def test_failures_and_wrong_routing_remain_in_report_denominator(corpus):
    from app.rag_eval.answer_scoring import score_snapshots
    _, cases = corpus
    cases = [{**cases[0], "case_id": str(i)} for i in range(4)]
    rows = [row("0"), row("1", error="query_failed"), row("2", actual_intent="chitchat"), row("3", response="")]
    metrics = {name: FixedMetric(1) for name in ("faithfulness", "answer_relevancy", "factual_correctness")}
    report = score_snapshots(cases, rows, metrics)
    assert report["summary"]["case_count"] == 4
    assert report["summary"]["query_errors"] == 1
    assert report["summary"]["routing_errors"] == 1
    assert report["summary"]["empty_answers"] == 1
    assert report["summary"]["metrics"]["faithfulness"]["coverage"] == .25


def test_judge_timeout_is_per_metric_and_does_not_abort_batch(corpus):
    import asyncio
    from app.rag_eval.answer_scoring import score_snapshots
    class SlowMetric:
        async def ascore(self, **kwargs):
            await asyncio.sleep(1)
            return SimpleNamespace(value=1)
    _, cases = corpus
    report = score_snapshots(cases, [row()], {"faithfulness": SlowMetric(),
        "answer_relevancy": FixedMetric(1), "factual_correctness": FixedMetric(1)}, timeout_seconds=.01)
    scores = report["results"][0]["metrics"]
    assert scores["faithfulness"]["reason"] == "TimeoutError"
    assert scores["factual_correctness"]["value"] == 1


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "query", "contexts"])
def test_rescoring_rejects_inconsistent_snapshot_rows(corpus, mutation):
    from app.rag_eval.answer_scoring import score_snapshots
    _, cases = corpus
    rows = [row()]
    if mutation == "missing": rows = []
    if mutation == "duplicate": rows.append(row())
    if mutation == "query": rows[0]["query"] = "另一问题"
    if mutation == "contexts": rows[0]["retrieved_contexts"] = "错误类型"
    with pytest.raises(ValueError): score_snapshots(cases, rows, {})


def test_baseline_rejects_changed_dataset_documents_or_judge():
    from app.rag_eval.answer_scoring import compare_baseline
    report = {"dataset_sha256": "a", "documents": {"manual": "b"},
              "scoring_config": {"judge_model": "judge-v1"}, "ready_for_baseline": True,
              "summary": {"metrics": {"faithfulness": {"mean": .8, "coverage": 1}}}}
    baseline = copy.deepcopy(report)
    baseline["summary"]["metrics"]["faithfulness"]["mean"] = .5
    assert compare_baseline(report, baseline)["faithfulness"]["mean_delta"] == pytest.approx(.3)
    for key in ("dataset_sha256", "documents", "scoring_config"):
        changed = copy.deepcopy(baseline)
        changed[key] = "changed"
        with pytest.raises(ValueError): compare_baseline(report, changed)
    baseline["ready_for_baseline"] = False
    with pytest.raises(ValueError): compare_baseline(report, baseline)
