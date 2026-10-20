"""Public entry defaults to validation and cannot silently query live resources."""
import copy
import json
from types import SimpleNamespace

import pytest

pytest_plugins = ["test_answer_evaluation"]


def test_entry_validates_without_loading_models_or_query_pipeline(corpus, monkeypatch):
    from app.rag_eval import answer_quality
    root, cases = corpus
    path = root / "cases.json"
    path.write_text(json.dumps(cases), encoding="utf-8")
    def forbidden(*args, **kwargs): raise AssertionError("external work started")
    monkeypatch.setattr(answer_quality, "_collect_real", forbidden)
    monkeypatch.setattr(answer_quality, "_build_backend", forbidden)
    report = answer_quality.run_answer_eval(path, project_root=root)
    assert report["mode"] == "validation_only"
    assert report["dataset"]["case_count"] == 1
    assert not (root / "output").exists()


def test_entry_requires_review_before_real_execution(corpus):
    from app.rag_eval.answer_quality import run_answer_eval
    root, cases = corpus
    path = root / "cases.json"
    path.write_text(json.dumps(cases), encoding="utf-8")
    with pytest.raises(ValueError, match="复核"):
        run_answer_eval(path, allow_external=True, project_root=root)


def test_eval_model_must_be_explicit_and_endpoint_defaults_are_reused(monkeypatch):
    from app.rag_eval.ragas_adapter import evaluation_config
    from app.shared.config import settings
    monkeypatch.setattr(settings.evaluation, "model", "")
    with pytest.raises(ValueError, match="EVAL_LLM_MODEL"): evaluation_config(settings)
    monkeypatch.setattr(settings.evaluation, "model", "judge-model")
    monkeypatch.setattr(settings.evaluation, "base_url", "")
    monkeypatch.setattr(settings.evaluation, "api_key", "")
    monkeypatch.setattr(settings.llm, "base_url", "http://models.invalid/v1")
    monkeypatch.setattr(settings.llm, "api_key", "private-token")
    config, metadata = evaluation_config(settings)
    assert config.model == "judge-model"
    assert config.base_url == "http://models.invalid/v1"
    assert config.api_key == "private-token"
    assert "private-token" not in json.dumps(metadata)


@pytest.mark.parametrize("field", ["mongo", "chunks", "items", "same", "evolution"])
def test_isolation_guard_blocks_before_any_external_call(field):
    from app.rag_eval.answer_collection import require_isolated_resources
    settings = SimpleNamespace(mongo=SimpleNamespace(db_name="eval_rag"),
        milvus=SimpleNamespace(chunks_collection="eval_chunks", item_name_collection="eval_items"),
        evolution=SimpleNamespace(enabled=False))
    if field == "mongo": settings.mongo.db_name = "production"
    if field == "chunks": settings.milvus.chunks_collection = "chunks"
    if field == "items": settings.milvus.item_name_collection = "items"
    if field == "same": settings.milvus.item_name_collection = "eval_chunks"
    if field == "evolution": settings.evolution.enabled = True
    with pytest.raises(ValueError): require_isolated_resources(settings)


def test_settings_read_eval_config_centrally(monkeypatch):
    from app.shared.config.settings import _build_settings
    monkeypatch.setenv("EVAL_LLM_MODEL", "independent-judge")
    monkeypatch.setenv("EVAL_LLM_BASE_URL", "http://judge.invalid/v1")
    config = _build_settings()
    assert config.evaluation.model == "independent-judge"
    assert config.evaluation.base_url == "http://judge.invalid/v1"


def test_snapshot_identity_is_checked_before_rescoring(corpus):
    from app.rag_eval.answer_dataset import validate_answer_cases
    from app.rag_eval.answer_quality import validate_snapshots
    root, cases = corpus
    manifest = validate_answer_cases(cases, project_root=root)
    snapshot = {"schema_version": 1, "dataset_sha256": manifest["dataset_sha256"],
                "documents": manifest["documents"], "generation": {"revision": "original"},
                "results": [{"case_id": "one", "query": cases[0]["query"], "response": "answer",
                             "retrieved_contexts": ["evidence"], "actual_intent": "rag", "error": None}]}
    validate_snapshots(snapshot, cases, manifest)
    for key in ("dataset_sha256", "documents"):
        changed = copy.deepcopy(snapshot)
        changed[key] = "different"
        with pytest.raises(ValueError): validate_snapshots(changed, cases, manifest)


def test_saved_answers_can_be_rescored_without_querying(corpus, monkeypatch, tmp_path):
    from app.rag_eval import answer_quality
    from app.rag_eval.answer_dataset import validate_answer_cases
    from app.shared.config import settings
    from test_answer_evaluation import FixedMetric, row
    root, cases = corpus
    cases[0].update(annotation_status="reviewed", reviewed_by="engineer")
    case_path = root / "cases.json"
    case_path.write_text(json.dumps(cases), encoding="utf-8")
    manifest = validate_answer_cases(cases, project_root=root)
    snapshot_path = root / "snapshot.json"
    snapshot_path.write_text(json.dumps({"schema_version": 1, "dataset_sha256": manifest["dataset_sha256"],
        "documents": manifest["documents"], "generation": {"revision": "source-revision"},
        "results": [row()]}), encoding="utf-8")
    def forbidden(*args, **kwargs): raise AssertionError("query pipeline was invoked")
    monkeypatch.setattr(answer_quality, "_collect_real", forbidden)
    monkeypatch.setattr(settings.evaluation, "model", "judge")
    monkeypatch.setattr(settings.llm, "base_url", "http://model.invalid/v1")
    monkeypatch.setattr(settings.llm, "api_key", "test-key")
    metrics = {n: FixedMetric(.8) for n in ("faithfulness", "answer_relevancy", "factual_correctness")}
    monkeypatch.setattr(answer_quality, "_build_backend", lambda config: (metrics, None))
    output = root / "report.json"
    report = answer_quality.run_answer_eval(case_path, output, allow_external=True,
        snapshots_path=snapshot_path, project_root=root)
    assert report["generation"]["revision"] == "source-revision"
    assert report["ready_for_baseline"] is True
    assert json.loads(output.read_text(encoding="utf-8"))["summary"]["case_count"] == 1


def test_public_tester_exposes_safe_validation(corpus):
    from app.rag_eval import RagEvalTester
    root, cases = corpus
    path = root / "cases.json"
    path.write_text(json.dumps(cases), encoding="utf-8")
    result = RagEvalTester().run_answer_eval(path, project_root=root)
    assert result["mode"] == "validation_only"


def _configured_settings(monkeypatch):
    from app.shared.config import settings
    monkeypatch.setattr(settings.evaluation, "model", "judge")
    monkeypatch.setattr(settings.llm, "base_url", "http://model.invalid/v1")
    monkeypatch.setattr(settings.llm, "api_key", "test-key")
    monkeypatch.setattr(settings.mongo, "db_name", "eval_rag")
    monkeypatch.setattr(settings.milvus, "chunks_collection", "eval_chunks")
    monkeypatch.setattr(settings.milvus, "item_name_collection", "eval_items")
    monkeypatch.setattr(settings.evolution, "enabled", False)


def _saved_fixture(corpus):
    from app.rag_eval.answer_dataset import validate_answer_cases
    from test_answer_evaluation import row
    root, cases = corpus
    cases[0].update(annotation_status="reviewed", reviewed_by="engineer")
    case_path = root / "cases.json"
    case_path.write_text(json.dumps(cases), encoding="utf-8")
    manifest = validate_answer_cases(cases, project_root=root)
    snapshot_path = root / "snapshot.json"
    snapshot_path.write_text(json.dumps({"schema_version": 1, "dataset_sha256": manifest["dataset_sha256"],
        "documents": manifest["documents"], "generation": {"revision": "source"}, "results": [row()]}), encoding="utf-8")
    return root, case_path, snapshot_path


def test_missing_optional_backend_fails_before_collecting_answers(corpus, monkeypatch):
    from app.rag_eval import answer_quality
    root, case_path, _ = _saved_fixture(corpus)
    _configured_settings(monkeypatch)
    def missing(*a, **k): raise RuntimeError("optional backend missing")
    def forbidden(*a, **k): raise AssertionError("query ran before dependency check")
    monkeypatch.setattr(answer_quality, "_build_backend", missing)
    monkeypatch.setattr(answer_quality, "_collect_real", forbidden)
    with pytest.raises(RuntimeError, match="optional backend"):
        answer_quality.run_answer_eval(case_path, root / "report.json", allow_external=True, project_root=root)


@pytest.mark.parametrize("overwrite", ["cases", "snapshots", "source"])
def test_output_cannot_overwrite_input_artifacts(corpus, monkeypatch, overwrite):
    from app.rag_eval import answer_quality
    root, case_path, snapshot_path = _saved_fixture(corpus)
    _configured_settings(monkeypatch)
    output = {"cases": case_path, "snapshots": snapshot_path, "source": root / "doc/manual.pdf"}[overwrite]
    before = output.read_bytes()
    def forbidden(*a, **k): raise AssertionError("backend ran before path validation")
    monkeypatch.setattr(answer_quality, "_build_backend", forbidden)
    with pytest.raises(ValueError, match="覆盖"):
        answer_quality.run_answer_eval(case_path, output, allow_external=True,
            snapshots_path=snapshot_path, project_root=root)
    assert output.read_bytes() == before


def test_partial_scoring_report_is_saved_when_a_baseline_was_requested(corpus, monkeypatch):
    from app.rag_eval import answer_quality
    from test_answer_evaluation import FixedMetric
    root, case_path, snapshot_path = _saved_fixture(corpus)
    _configured_settings(monkeypatch)
    metrics = {n: FixedMetric(.8) for n in ("faithfulness", "answer_relevancy", "factual_correctness")}
    monkeypatch.setattr(answer_quality, "_build_backend", lambda config: (metrics, None))
    baseline = root / "baseline.json"
    answer_quality.run_answer_eval(case_path, baseline, allow_external=True,
        snapshots_path=snapshot_path, project_root=root)
    metrics["faithfulness"] = FixedMetric(TimeoutError())
    output = root / "candidate.json"
    result = answer_quality.run_answer_eval(case_path, output, allow_external=True,
        snapshots_path=snapshot_path, baseline_path=baseline, project_root=root)
    assert result["ready_for_baseline"] is False
    assert result["comparison_error"] == "incomplete_scoring"
    assert json.loads(output.read_text(encoding="utf-8"))["summary"]["metrics"]["faithfulness"]["coverage"] == 0
