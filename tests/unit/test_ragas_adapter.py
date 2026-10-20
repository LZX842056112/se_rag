"""Exercise the pinned Ragas algorithms with scripted judge output and dense vectors."""
import importlib.util
import json
import time

import pytest


def _dense_vectors(texts):
    return {"dense": [[0., 1.] if t == "irrelevant" else [1., 0.] for t in texts]}


def _slow_vectors(texts):
    time.sleep(2)
    return {"dense": [[1., 0.] for _ in texts]}


@pytest.fixture
def backend(monkeypatch, request):
    if importlib.util.find_spec("ragas") is None:
        pytest.skip("Install the optional eval dependency group")
    monkeypatch.setenv("RAGAS_DO_NOT_TRACK", "true")
    from app.rag_eval import ragas_adapter
    from app.shared.config.settings import EvaluationSettings
    original = ragas_adapter.make_bge_embeddings
    encode = getattr(request, "param", _dense_vectors)
    monkeypatch.setattr(ragas_adapter, "make_bge_embeddings", lambda: original(encode))
    config = EvaluationSettings("test-judge", "http://model.invalid/v1", "test-key")
    metrics, client = ragas_adapter.create_ragas_scorers(config)
    return metrics, client


def _verdicts(values):
    return {"statements": [{"statement": "claim", "reason": "scripted judgement", "verdict": v} for v in values]}


@pytest.mark.parametrize("scenario,expected", [
    ("correct", (1., 1., 1.)), ("wrong_fact", (0., 1., 0.)),
    ("omission", (1., 1., .67)), ("irrelevant", (0., 0., 0.)),
])
def test_real_ragas_scores_correct_wrong_incomplete_and_irrelevant_answers(backend, scenario, expected):
    from app.rag_eval.answer_scoring import score_snapshots
    metrics, client = backend
    verdict = 1 if scenario in ("correct", "omission") else 0
    queue = [
        {"statements": ["response claim"]}, _verdicts([verdict]),
        *[{"question": "irrelevant" if scenario == "irrelevant" else "question", "noncommittal": 0} for _ in range(3)],
        {"claims": ["response claim"]}, _verdicts([verdict]),
        {"claims": ["reference claim", "missing claim"]} if scenario == "omission" else {"claims": ["reference claim"]},
        _verdicts([1, 0]) if scenario == "omission" else _verdicts([verdict]),
    ]
    async def judge(prompt, response_model):
        return response_model.model_validate(queue.pop(0))
    for metric in metrics.values(): metric.llm.agenerate = judge
    case = {"case_id": "one", "product": "product", "category": "fact", "query": "question",
            "reference": "reference", "source": {}, "annotation_status": "reviewed"}
    row = {"case_id": "one", "query": "question", "response": "answer", "actual_intent": "rag",
           "retrieved_contexts": ["actual evidence"], "error": None}
    report = score_snapshots([case], [row], metrics, close=client.close)
    scores = report["results"][0]["metrics"]
    assert tuple(scores[n]["value"] for n in metrics) == pytest.approx(expected)
    assert queue == []


def test_invalid_json_from_judge_is_reported_without_aborting_other_metrics(backend):
    from app.rag_eval.answer_scoring import score_snapshots
    metrics, client = backend
    async def invalid_json(prompt, response_model):
        raise json.JSONDecodeError("invalid JSON", "", 0)
    for metric in metrics.values(): metric.llm.agenerate = invalid_json
    case = {"case_id": "one", "product": "p", "category": "fact", "query": "q",
            "reference": "r", "source": {}, "annotation_status": "draft"}
    row = {"case_id": "one", "query": "q", "response": "a", "actual_intent": "rag",
           "retrieved_contexts": ["evidence"], "error": None}
    report = score_snapshots([case], [row], metrics, close=client.close)
    assert all(v["value"] is None and v["reason"] == "JSONDecodeError" for v in report["results"][0]["metrics"].values())


@pytest.mark.parametrize("backend", [_slow_vectors], indirect=True)
def test_embedding_timeout_does_not_delay_report_or_start_more_bge_work(backend):
    from app.rag_eval.answer_scoring import score_snapshots
    from test_answer_evaluation import FixedMetric
    metrics, client = backend
    async def judge(prompt, response_model):
        return response_model.model_validate({"question": "question", "noncommittal": 0})
    metrics["answer_relevancy"].llm.agenerate = judge
    scorers = {"faithfulness": FixedMetric(1), "answer_relevancy": metrics["answer_relevancy"],
               "factual_correctness": FixedMetric(1)}
    cases = [{"case_id": str(i), "product": "p", "category": "fact", "query": "question",
              "reference": "r", "source": {}, "annotation_status": "draft"} for i in range(2)]
    rows = [{"case_id": str(i), "query": "question", "response": "answer", "actual_intent": "rag",
             "retrieved_contexts": ["evidence"], "error": None} for i in range(2)]
    started = time.perf_counter()
    report = score_snapshots(cases, rows, scorers, timeout_seconds=.05, close=client.close)
    elapsed = time.perf_counter() - started
    assert report["results"][0]["metrics"]["answer_relevancy"]["reason"] == "TimeoutError"
    assert elapsed < .8
    assert report["results"][1]["metrics"]["answer_relevancy"]["reason"] == "EmbeddingWorkerUnavailableError"
