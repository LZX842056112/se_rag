"""针对成对评估与来源的离线校验。"""
import copy
import hashlib
import json
from types import SimpleNamespace

import pytest


@pytest.fixture
def eval_corpus(tmp_path):
    from app.rag_eval.answer_dataset import validate_answer_cases
    source = tmp_path / "doc" / "manual.pdf"
    source.parent.mkdir()
    source.write_bytes(b"manual")
    cases = [{"case_id": "one", "query": "What is the range?", "product": "X",
              "category": "multi_hop", "reference": "1 to 3", "annotation_status": "draft",
              "reviewed_by": "", "source": {"path": "doc/manual.pdf", "pdf_page": 1,
                  "section": "Range", "excerpt": "1 to 3", "sha256": hashlib.sha256(b"manual").hexdigest()}}]
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(cases), encoding="utf-8")
    return tmp_path, path, cases, validate_answer_cases(cases, project_root=tmp_path)


def isolated_settings(**changes):
    evolution = dict(enabled=True, recall_enabled=True, write_enabled=False,
                     collection="eval_faq", scheduler_enabled=False, auto_tune_enabled=False)
    evolution.update(changes)
    return SimpleNamespace(mongo=SimpleNamespace(db_name="eval_db"),
        milvus=SimpleNamespace(chunks_collection="eval_chunks", item_name_collection="eval_items"),
        evolution=SimpleNamespace(**evolution))


def test_faq_reads_are_allowed_only_when_isolated_and_writes_are_off():
    from app.rag_eval.answer_collection import require_isolated_resources
    assert require_isolated_resources(isolated_settings())["evolution_collection"] == "eval_faq"
    for changes in ({"collection": "prod_faq"}, {"write_enabled": True},
                    {"scheduler_enabled": True}, {"auto_tune_enabled": True}, {"collection": "eval_chunks"}):
        with pytest.raises(ValueError):
            require_isolated_resources(isolated_settings(**changes))


def test_collection_saves_internal_provenance_without_leaking_gold(eval_corpus):
    from app.rag_eval.answer_collection import collect_answers
    _, _, cases, _ = eval_corpus
    def invoke(**kwargs):
        assert set(kwargs) == {"session_id", "original_query", "is_stream"}
        return {"intent": "rag", "answer": "1 to 3", "answer_contexts": ["parent"],
                "agentic_trace": [{"tool": "read", "status": "ok"}],
                "evidence_manifest": [{"evidence_id": "a"}], "agentic_stop_reason": "supported",
                "agentic_metrics": {"tool_calls": 1}}
    row = collect_answers(cases, invoke)[0]
    assert row["agentic_trace"] == [{"tool": "read", "status": "ok"}]
    assert row["evidence_manifest"] == [{"evidence_id": "a"}]
    assert row["agentic_stop_reason"] == "supported"
    assert row["agentic_metrics"]["tool_calls"] == 1


def eval_snapshot():
    return {"snapshot_id": "frozen-1", "corpus_version": "docs-v1", "index_version": "chunks-v1",
            "approved_faq": {"enabled": False}}


def test_comparison_default_does_not_build_models_or_connect(eval_corpus, monkeypatch):
    from app.rag_eval import agentic_comparison as module
    root, path, _, _ = eval_corpus
    def forbidden(*a, **kw):
        raise AssertionError("external work started")
    monkeypatch.setattr(module, "_collect_pair", forbidden)
    monkeypatch.setattr(module, "_build_backend", forbidden)
    result = module.run_comparison(path, project_root=root)
    assert result["mode"] == "validation_only"
    assert result["ready_for_baseline"] is False
    assert not (root / "output").exists()


def saved_pair(cases, manifest):
    from app.rag_eval.answer_dataset import json_digest
    shared = {"dataset_sha256": manifest["dataset_sha256"], "documents": manifest["documents"],
              "evaluation_snapshot": eval_snapshot(), "answer_model": "answer-model",
              "budgets": {"max_tool_calls": 4}, "prompts_sha256": "p" * 64,
              "code_sha256": "c" * 64, "policy_version": "v1"}
    row = {"case_id": "one", "query": cases[0]["query"], "response": "1 to 3",
           "actual_intent": "rag", "retrieved_contexts": ["1 to 3"], "error": None,
           "elapsed_ms": 20, "query_metrics": {"cost_usd": None}}
    return {"schema_version": 1, "shared": shared, "shared_sha256": json_digest(shared),
            "runs": {mode: {"mode": mode, "shared_sha256": json_digest(shared), "results": [copy.deepcopy(row)]}
                     for mode in ("legacy", "agentic")}}


@pytest.mark.parametrize("mutation", ["query", "dataset", "faq", "mode", "run_digest", "missing_run"])
def test_pair_rejects_spoofed_or_unpaired_snapshots(eval_corpus, mutation):
    from app.rag_eval.agentic_comparison import validate_pair
    _, _, cases, manifest = eval_corpus
    pair = saved_pair(cases, manifest)
    if mutation == "query": pair["runs"]["agentic"]["results"][0]["query"] = "spoof"
    if mutation == "dataset": pair["shared"]["dataset_sha256"] = "spoof"
    if mutation == "faq": pair["shared"]["evaluation_snapshot"]["approved_faq"] = {"enabled": True}
    if mutation == "mode": pair["runs"]["agentic"]["mode"] = "legacy"
    if mutation == "run_digest": pair["runs"]["agentic"]["shared_sha256"] = "spoof"
    if mutation == "missing_run": del pair["runs"]["legacy"]
    with pytest.raises(ValueError): validate_pair(pair, cases, manifest)


def test_failed_queries_remain_in_latency_cost_and_quality_denominators(eval_corpus):
    from app.rag_eval.agentic_comparison import score_pair
    _, _, cases, manifest = eval_corpus
    pair = saved_pair(cases, manifest)
    pair["runs"]["agentic"]["results"][0].update(error="TimeoutError", elapsed_ms=60)
    class Metric:
        async def ascore(self, **kw): return SimpleNamespace(value=.8)
    scorers = {n: Metric() for n in ("faithfulness", "answer_relevancy", "factual_correctness")}
    result = score_pair(pair, cases, manifest, scorers, {"judge_model": "judge"})
    assert result["runs"]["agentic"]["summary"]["query_errors"] == 1
    assert result["runs"]["agentic"]["runtime"]["latency_ms"]["p95"] == 60
    assert result["runs"]["agentic"]["runtime"]["cost_usd"]["mean"] is None
    assert result["runs"]["agentic"]["summary"]["metrics"]["faithfulness"]["coverage"] == 0
    assert result["comparison"]["faithfulness"]["paired_count"] == 0
    assert result["comparison"]["faithfulness"]["mean_delta"] is None
    assert result["ready_for_baseline"] is False


def test_baseline_rejects_changed_approved_faq_snapshot():
    from app.rag_eval.answer_scoring import compare_baseline
    report = {"dataset_sha256": "d", "documents": {}, "scoring_config": {},
              "ready_for_baseline": True, "summary": {"metrics": {}},
              "approved_faq": {"content_sha256": "a", "index_version": "1"}}
    baseline = copy.deepcopy(report)
    baseline["approved_faq"]["content_sha256"] = "b"
    with pytest.raises(ValueError, match="approved_faq"):
        compare_baseline(report, baseline)


def test_faq_snapshot_is_computed_from_rows_not_declaration():
    from app.rag_eval.agentic_comparison import faq_content_digest
    rows = [{"evo_doc_id": "1", "faq_answer": "a", "status": "active", "item_name": "X"},
            {"evo_doc_id": "2", "faq_answer": "b", "status": "active", "item_name": "X"}]
    assert faq_content_digest(rows) == faq_content_digest(list(reversed(rows)))
    changed = copy.deepcopy(rows)
    changed[0]["faq_answer"] = "changed"
    assert faq_content_digest(rows) != faq_content_digest(changed)


def test_additional_document_changes_invalidate_multi_hop_case(eval_corpus):
    from app.rag_eval.answer_dataset import validate_answer_cases
    root, _, cases, _ = eval_corpus
    cases[0]["additional_sources"] = [{**cases[0]["source"], "sha256": "0" * 64}]
    with pytest.raises(ValueError):
        validate_answer_cases(cases, project_root=root)


def test_extension_review_cannot_hide_unreviewed_sources_or_uncreated_fixture(eval_corpus):
    from app.rag_eval.answer_dataset import validate_answer_cases
    root, _, cases, _ = eval_corpus
    cases[0].update(annotation_status="reviewed", reviewed_by="engineer",
                    source_review={"status": "unreviewed", "reviewed_by": ""})
    with pytest.raises(ValueError):
        validate_answer_cases(cases, project_root=root)


def test_faq_capture_rejects_declared_digest_without_matching_actual_rows(monkeypatch):
    from app.rag_eval.agentic_comparison import capture_faq_snapshot
    from app.shared.clients.milvus_gateway import milvus_gateway
    from app.evolution import governance
    class Iterator:
        def __init__(self): self.rows = [[{"evo_doc_id": "1", "faq_answer": "real", "status": "active"}], []]
        def next(self): return self.rows.pop(0)
        def close(self): pass
    client = SimpleNamespace(has_collection=lambda **kw: True, query_iterator=lambda **kw: Iterator())
    monkeypatch.setattr(type(milvus_gateway), "milvus_client", property(lambda self: client))
    monkeypatch.setattr(governance, "approved_faq_snapshot", lambda: [])
    settings = isolated_settings()
    settings.milvus.timeout_seconds = 10
    expected = {"enabled": True, "collection": "eval_faq", "content_sha256": "0" * 64,
                "approval_sha256": "0" * 64, "index_version": "empty"}
    with pytest.raises(ValueError, match="content_sha256"):
        capture_faq_snapshot(settings, expected)


def test_real_collection_switches_modes_and_restores_settings_on_failure(eval_corpus, monkeypatch):
    from app.rag_eval import agentic_comparison as module
    from app.shared.config import settings
    from app.rag.query import pipeline
    from app.shared.clients import mongo
    root, _, cases, manifest = eval_corpus
    monkeypatch.setattr(settings.agentic, "mode", "legacy")
    monkeypatch.setattr(settings.agentic, "gray_percent", 17)
    monkeypatch.setattr(module, "require_isolated_resources", lambda s: {})
    monkeypatch.setattr(module, "capture_faq_snapshot", lambda *a: {"enabled": False})
    monkeypatch.setattr(module, "_revision", lambda root: {"code_sha256": "c" * 64, "revision": "x"})
    closed = []
    monkeypatch.setattr(mongo, "close_mongo_client", lambda: closed.append(True))
    modes = []
    def collect(cases, invoke):
        modes.append((settings.agentic.mode, settings.agentic.gray_percent))
        if settings.agentic.mode == "agentic": raise RuntimeError("failure")
        return []
    monkeypatch.setattr(module, "collect_answers", collect)
    monkeypatch.setattr(pipeline, "invoke_query_graph", lambda **kw: None)
    with pytest.raises(RuntimeError, match="failure"):
        module._collect_pair(cases, manifest, eval_snapshot(), root, standalone=True)
    assert modes == [("legacy", 100), ("agentic", 100)]
    assert (settings.agentic.mode, settings.agentic.gray_percent) == ("legacy", 17)
    assert closed == [True]


def test_live_pair_collection_cannot_mutate_a_service_process(eval_corpus):
    from app.rag_eval.agentic_comparison import _collect_pair
    root, _, cases, manifest = eval_corpus
    with pytest.raises(ValueError, match="CLI"):
        _collect_pair(cases, manifest, eval_snapshot(), root)


def test_saved_pair_rescores_both_modes_with_one_judge_and_no_query(eval_corpus, monkeypatch):
    from app.rag_eval import agentic_comparison as module
    from app.shared.config import settings
    root, path, cases, manifest = eval_corpus
    snapshot_path = root / "pair.json"
    snapshot_path.write_text(json.dumps(saved_pair(cases, manifest)), encoding="utf-8")
    monkeypatch.setattr(settings.evaluation, "model", "judge")
    monkeypatch.setattr(settings.evaluation, "base_url", "http://invalid.test")
    monkeypatch.setattr(settings.evaluation, "api_key", "test-key")
    calls = []
    class Metric:
        async def ascore(self, **kw):
            calls.append(kw)
            return SimpleNamespace(value=.7)
    backends = []
    def backend(config):
        backends.append(config.model)
        return {name: Metric() for name in ("faithfulness", "answer_relevancy", "factual_correctness")}, None
    def forbidden(*a, **kw): raise AssertionError("query pipeline called")
    monkeypatch.setattr(module, "_build_backend", backend)
    monkeypatch.setattr(module, "_collect_pair", forbidden)
    report = module.run_comparison(path, root / "report.json", snapshots_path=snapshot_path,
                                   allow_external=True, allow_unreviewed=True, project_root=root)
    assert backends == ["judge"]
    assert len(calls) == 6
    assert report["comparison"]["faithfulness"]["mean_delta"] == 0
    assert report["ready_for_baseline"] is False
    assert json.loads((root / "report.json").read_text())["scoring_config"]["judge_model"] == "judge"


def test_fixture_requirement_cannot_be_bypassed_with_allow_unreviewed(eval_corpus):
    from app.rag_eval.agentic_comparison import run_comparison
    root, path, cases, _ = eval_corpus
    cases[0]["scenario_requirements"] = {"status": "not_created", "snapshot_id": ""}
    path.write_text(json.dumps(cases), encoding="utf-8")
    snapshot = root / "eval-snapshot.json"
    snapshot.write_text(json.dumps(eval_snapshot()), encoding="utf-8")
    with pytest.raises(ValueError, match="夹具"):
        run_comparison(path, root / "report.json", evaluation_snapshot_path=snapshot,
                       allow_external=True, allow_unreviewed=True, project_root=root)


def test_old_snapshot_cannot_hide_enabled_faq_without_digest(eval_corpus):
    from app.rag_eval.answer_quality import validate_snapshots
    root, _, cases, manifest = eval_corpus
    snapshot = {"schema_version": 1, "dataset_sha256": manifest["dataset_sha256"],
                "documents": manifest["documents"], "generation": {"revision": "a",
                    "resources": {"evolution_collection": "eval_faq"}},
                "results": saved_pair(cases, manifest)["runs"]["legacy"]["results"]}
    with pytest.raises(ValueError, match="FAQ"):
        validate_snapshots(snapshot, cases, manifest)