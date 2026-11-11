"""评测执行器：成功、失败与汇总。"""
from __future__ import annotations

import json

from app.rag_eval import runner
from app.rag_eval.config import EvalConfig
from app.rag_eval.http_client import EvalHTTPError, QueryResult


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
            retrieval_layers={"reranked": ["c1"], "embedding": ["c1"]},
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
