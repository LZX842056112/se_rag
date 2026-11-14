"""评测执行器：成功、失败与汇总。"""
from __future__ import annotations

import json
from hashlib import sha256

import pytest
import requests

from app.rag_eval import runner
from app.rag_eval.config import EvalConfig
from app.rag_eval.http_client import EvalHTTPError, QueryResult
from app.rag_eval.report import render_markdown


class _FakeClient:
    def __init__(self, *_args, **_kwargs):
        pass

    def health(self):
        return {"code": 200}

    def ask(self, question: str) -> QueryResult:
        if "失败" in question:
            raise EvalHTTPError("模拟失败")
        return QueryResult(
            answer="标准答案",
            evidence=[{"chunk_id": "c1", "content": "相关正文"}],
            contexts=["实际上下文"],
            outcome="answered",
            retrieval_layers={"reranked": ["c1"], "embedding": ["c1"], "final": ["c1"]},
            latency_ms=12.0,
        )

    def close(self):
        pass


def _dataset(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([
        {
            "case_id": "ok",
            "question": "怎么设置",
            "ground_truth": "标准答案",
            "gold_chunk_ids": ["c1"],
            "must_hit_chunk_ids": ["c1"],
        },
        {
            "case_id": "bad",
            "question": "失败的问题",
            "ground_truth": "答案",
            "gold_chunk_ids": ["c1"],
        },
    ], ensure_ascii=False), encoding="utf-8")
    return path


def test_run_evaluation_reports_success_and_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "QueryClient", _FakeClient)
    config = EvalConfig(
        dataset_path=_dataset(tmp_path),
        output_dir=tmp_path / "out",
        run_ragas=False,
    )

    report = runner.run_evaluation(config)

    assert report["summary"]["total"] == 2
    assert report["summary"]["succeeded"] == 1
    assert report["summary"]["failed"] == 1
    assert report["summary"]["retrieval"]["reranked"]["recall@5"] == 1.0
    assert report["summary"]["retrieval"]["reranked"]["recall@5_n"] == 1
    failed = [row for row in report["cases"] if row["error"]]
    assert failed and failed[0]["case_id"] == "bad"
    assert failed[0]["status"] == "failed"
    assert any(row["status"] == "succeeded" for row in report["cases"])
    # 有失败样本时总体结论必须是 failed，而不是只藏在明细里
    assert report["summary"]["conclusion"]["status"] == "failed"
    assert "1 题执行失败" in report["summary"]["conclusion"]["reasons"]


def test_report_summarizes_degradation_and_actual_http_attempts(tmp_path, monkeypatch):
    class Session(requests.Session):
        def get(self, *args, **kwargs):
            return self.response(200, {"evaluation": {"service_source_sha256": "abc"}})

        def post(self, url, json, timeout):
            if "失败" in json["query"]:
                raise requests.Timeout("timed out")
            if not getattr(self, "retried", False):
                self.retried = True
                return self.response(503, {})
            return self.response(200, {
                "answer": "答案", "evaluation": {"outcome": "answered", "contexts": ["正文"]},
                "metrics": {
                    "degraded": True, "degraded_reasons": ["web_timeout", "web_timeout", "intent_timeout"],
                    "rerank_pairs": 7, "rerank_input_tokens": 1234,
                },
            })

        @staticmethod
        def response(status, payload):
            response = requests.Response()
            response.status_code = status
            response._content = json.dumps(payload).encode()
            return response

    monkeypatch.setattr(requests, "Session", Session)
    monkeypatch.setattr("app.rag_eval.http_client.time.sleep", lambda _seconds: None)
    config = EvalConfig(_dataset(tmp_path), tmp_path, run_ragas=False)

    report = runner.run_evaluation(config)

    failed, succeeded = report["cases"]
    assert failed["http_attempts"] == succeeded["http_attempts"] == 2
    assert failed["http_failure_reasons"] == ["timeout", "timeout"]
    assert succeeded["server_metrics"] == {
        "degraded": True, "degraded_reasons": ["web_timeout", "web_timeout", "intent_timeout"],
        "rerank_pairs": 7, "rerank_input_tokens": 1234,
    }
    assert report["summary"]["http_attempts"] == {
        "total": 4, "retried_cases": 2, "failed_attempts_by_reason": {"http_503": 1, "timeout": 2},
    }
    assert report["summary"]["degradation"] == {
        "reported": 1, "degraded": 1, "by_reason": {"web_timeout": 1, "intent_timeout": 1},
    }
    assert report["summary"]["server_timing_ms"] == {}
    assert report["meta"]["server"]["service_source_sha256"] == "abc"
    markdown = render_markdown(report)
    assert "HTTP 请求共 4 次" in markdown
    assert "`web_timeout` 1 题" in markdown


def test_unannotated_cases_mark_conclusion_inconclusive(tmp_path, monkeypatch):
    """没有 gold_chunk_ids 时不能默认判「通过」，必须标记为无法判定。"""
    monkeypatch.setattr(runner, "QueryClient", _FakeClient)
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{
        "case_id": "a", "question": "怎么设置", "ground_truth": "标准答案",
    }], ensure_ascii=False), encoding="utf-8")
    config = EvalConfig(dataset_path=path, output_dir=tmp_path / "out", run_ragas=False)

    report = runner.run_evaluation(config)

    assert report["summary"]["conclusion"]["status"] == "inconclusive"
    assert any("gold_chunk_ids" in reason for reason in report["summary"]["conclusion"]["reasons"])


def test_tag_filter_and_empty_selection(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "QueryClient", _FakeClient)
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{
        "case_id": "a", "question": "q", "ground_truth": "g", "tags": ["x"]
    }]), encoding="utf-8")
    config = EvalConfig(dataset_path=path, output_dir=tmp_path / "out", run_ragas=False, tags=("y",))
    try:
        runner.run_evaluation(config)
    except ValueError as exc:
        assert "没有可执行样本" in str(exc)
    else:
        raise AssertionError("空选择应报错")


def test_closes_http_client_when_health_check_fails(tmp_path, monkeypatch):
    closed = []

    class Client(_FakeClient):
        def health(self):
            raise EvalHTTPError("health")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(runner, "QueryClient", Client)
    with pytest.raises(EvalHTTPError, match="health"):
        runner.run_evaluation(EvalConfig(_single_case(tmp_path), tmp_path))
    assert closed == [True]


def _single_case(tmp_path, **annotations):
    path = tmp_path / "cases.json"
    path.write_text(json.dumps([{
        "case_id": "case", "question": "q", "ground_truth": "标准答案",
        **annotations,
    }], ensure_ascii=False), encoding="utf-8")
    return path


def test_answerable_case_with_raw_evidence_but_no_contexts_fails(tmp_path, monkeypatch):
    class Client(_FakeClient):
        def ask(self, question):
            return QueryResult(answer="答案", evidence=[{"chunk_id": "c1", "content": "相关正文"}])

    monkeypatch.setattr(runner, "QueryClient", Client)
    config = EvalConfig(
        dataset_path=_single_case(tmp_path, gold_chunk_ids=["c1"]), output_dir=tmp_path, run_ragas=False,
    )
    report = runner.run_evaluation(config)

    assert report["summary"]["succeeded"] == 1
    assert report["summary"]["conclusion"]["status"] == "failed"
    assert report["cases"][0]["scoring_status"] == "missing_context"


def test_scores_actual_contexts_and_reports_server_provenance(tmp_path, monkeypatch):
    class Client(_FakeClient):
        def health(self):
            return {"code": 200, "evaluation": {
                "service_commit": "abc", "knowledge_base_version": "kb", "worker_count": 4,
                "runtime_backend": "redis",
            }}

        def ask(self, question):
            return QueryResult(
                answer="请打开 Wi-Fi", contexts=["父块正文"], item_names=["产品"],
                retrieval_layers={"agentic_retrieval": ["x", "c1"], "final": ["c1"]},
                outcome="answered", query_mode="agentic", evaluation_schema_version=1,
            )

    class Scorer:
        metadata = {"judge_model": "judge"}

        def __init__(self, settings):
            pass

        def score(self, *, contexts, **_args):
            return {
                "faithfulness": 1.0 if contexts == ["父块正文"] else 0.0,
                "answer_relevancy": 1.0, "context_precision": 1.0, "context_recall": 1.0,
                "retries": [{"metric": "faithfulness", "max_tokens": 8192, "previous_error": "max_tokens",
                             "status": "scored"}],
            }

        def close(self):
            pass

    monkeypatch.setattr(runner, "QueryClient", Client)
    monkeypatch.setattr(runner, "RagasScorer", Scorer)
    path = _single_case(
        tmp_path, expected_outcome="answered", gold_chunk_ids=["c1"], must_hit_chunk_ids=["c1"],
        expected_item_names=["产品"], required_answer_terms=["wi-fi"], forbidden_answer_terms=["重置"],
    )
    report = runner.run_evaluation(EvalConfig(path, tmp_path, ks=(1,)))

    assert report["meta"]["dataset_sha256"] == sha256(path.read_bytes()).hexdigest()
    assert report["meta"]["server"]["worker_count"] == 4
    assert report["meta"]["evaluation_schema_versions"] == [1]
    assert report["summary"]["query_modes"] == {"agentic": 1}
    assert report["summary"]["conclusion"]["status"] == "passed"
    assert report["summary"]["retrieval"]["final"]["recall@5"] == 1
    assert report["cases"][0]["retrieval_layers"]["agentic_retrieval"] == ["x", "c1"]
    assert report["cases"][0]["candidate_counts"]["agentic_retrieval"] == 2
    assert report["cases"][0]["ragas"]["faithfulness"] == 1
    assert report["cases"][0]["ragas_retries"] == [
        {"metric": "faithfulness", "max_tokens": 8192, "previous_error": "max_tokens", "status": "scored"},
    ]
    assert report["summary"]["ragas_retries"] == {"faithfulness": 1}
    assert "faithfulness 以 8192 token 重试成功" in render_markdown(report)


@pytest.mark.parametrize(("answer", "outcome", "status"), [
    ("请提供型号", "clarification", "passed"),
    ("请提供型号并重置", "clarification", "failed"),
    ("请提供型号", "handoff", "failed"),
])
def test_expected_non_answer_needs_correct_outcome_and_terms(tmp_path, monkeypatch, answer, outcome, status):
    class Client(_FakeClient):
        def ask(self, question):
            return QueryResult(answer=answer, outcome=outcome)

    monkeypatch.setattr(runner, "QueryClient", Client)
    path = _single_case(
        tmp_path, expected_outcome="clarification", required_answer_terms=["型号"], forbidden_answer_terms=["重置"],
    )
    report = runner.run_evaluation(EvalConfig(path, tmp_path))

    assert report["summary"]["conclusion"]["status"] == status
    if status == "passed":
        assert report["cases"][0]["scoring_status"] == "expected_no_score"


def test_cli_fails_when_required_scorer_cannot_start_and_writes_report(tmp_path, monkeypatch):
    from app.rag_eval.cli import main

    class Client(_FakeClient):
        def ask(self, question):
            return QueryResult(answer="答案", contexts=["正文"], outcome="answered")

    class Scorer:
        def __init__(self, settings):
            raise ValueError("judge init failure")

    monkeypatch.setattr(runner, "QueryClient", Client)
    monkeypatch.setattr(runner, "RagasScorer", Scorer)
    path = _single_case(tmp_path, expected_outcome="answered")
    baseline = tmp_path / "baseline.json"
    assert main([
        "run", "--dataset", str(path), "--output-dir", str(tmp_path), "--save-baseline", str(baseline),
    ]) == 1
    assert not baseline.exists()
    report = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert report["summary"]["coverage"]["http"] == {"required": 1, "completed": 1}
    assert report["summary"]["coverage"]["ragas"]["faithfulness"] == {"required": 1, "completed": 0}
    assert report["cases"][0]["scoring_status"] == "scorer_failed"


def test_cli_uses_final_evidence_for_acceptance_and_keeps_complete_baseline(tmp_path, monkeypatch):
    from app.rag_eval.cli import main

    class Client(_FakeClient):
        def ask(self, question):
            return QueryResult(
                answer="答案", contexts=["正文"], outcome="answered", query_mode="legacy",
                retrieval_layers={"reranked": ["c1"], "final": ["c2"]}, evaluation_schema_version=1,
            )

    monkeypatch.setattr(runner, "QueryClient", Client)
    path = _single_case(tmp_path, gold_chunk_ids=["c1"], must_hit_chunk_ids=["c1"])
    baseline = tmp_path / "baseline.json"
    assert main([
        "run", "--dataset", str(path), "--output-dir", str(tmp_path),
        "--no-ragas", "--save-baseline", str(baseline),
    ]) == 1
    report = json.loads(baseline.read_text(encoding="utf-8"))
    assert report["summary"]["retrieval"]["reranked"]["recall@5"] == 1.0
    assert report["summary"]["retrieval"]["final"]["recall@5"] == 0.0


def test_cli_keeps_inconclusive_smoke_report_without_saving_a_baseline(tmp_path, monkeypatch):
    from app.rag_eval.cli import main

    class Client(_FakeClient):
        def ask(self, question):
            return QueryResult(answer="答案", contexts=["正文"], outcome="answered")

    monkeypatch.setattr(runner, "QueryClient", Client)
    path = _single_case(tmp_path)
    baseline = tmp_path / "baseline.json"
    assert main([
        "run", "--dataset", str(path), "--output-dir", str(tmp_path),
        "--no-ragas", "--save-baseline", str(baseline),
    ]) == 1
    assert not baseline.exists()
    assert json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))["summary"]["conclusion"]["status"] == "inconclusive"


def test_cli_run_only_fails_on_comparable_regressions(tmp_path, monkeypatch):
    from app.rag_eval.cli import main

    class Client(_FakeClient):
        def ask(self, question):
            return QueryResult(
                answer="答案", contexts=["正文"], outcome="answered", evaluation_schema_version=1,
                latency_ms=4000.0, retrieval_layers={"final": ["c1"]},
            )

    monkeypatch.setattr(runner, "QueryClient", Client)
    path = _single_case(tmp_path, gold_chunk_ids=["c1"])
    baseline = runner.run_evaluation(EvalConfig(path, tmp_path, run_ragas=False))
    baseline["summary"]["latency_ms"] = {"avg": 100.0, "p95": 100.0}
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    argv = [
        "run", "--dataset", str(path), "--output-dir", str(tmp_path), "--no-ragas",
        "--baseline", str(baseline_path), "--fail-on-regression",
    ]
    assert main(argv) == 1

    baseline["meta"]["dataset_sha256"] = "b" * 64
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    assert main(argv) == 0
    saved = json.loads((tmp_path / "latest.json").read_text(encoding="utf-8"))
    assert saved["summary"]["conclusion"]["status"] == "passed"
    assert saved["comparison"]["comparable"] is False


def test_expected_subject_fails_when_response_includes_extra_subject(tmp_path, monkeypatch):
    class Client(_FakeClient):
        def ask(self, question):
            return QueryResult(
                answer="答案", contexts=["正文"], outcome="answered", item_names=["A", "B"],
            )

    monkeypatch.setattr(runner, "QueryClient", Client)
    path = _single_case(tmp_path, expected_item_names=["A"])
    report = runner.run_evaluation(EvalConfig(path, tmp_path, run_ragas=False))

    assert report["summary"]["conclusion"]["status"] == "failed"
    assert report["summary"]["coverage"]["behavior"]["failed"] == 1
