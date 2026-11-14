"""RAGAS 生成指标适配层（可选依赖，按需加载）。

指标：
- Faithfulness：答案是否只依据给定上下文；
- Answer Relevancy：答案与问题的相关程度；
- Context Precision：上下文中有用内容是否排在前面；
- Context Recall：标准答案需要的信息是否都在上下文里。

judge 模型来自 ``settings.evaluation``，与回答模型解耦；未配置时直接报错，
避免「自己评自己」。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import math
from copy import copy
from dataclasses import replace
from functools import partial

RAGAS_VERSION = "0.4.3"
_JUDGE_MAX_TOKENS = 4096
_RETRY_MAX_TOKENS = 8192

METRIC_NAMES: tuple[str, ...] = (
    "faithfulness",
    "answer_relevancy",
    "context_precision",
    "context_recall",
)


class RagasUnavailable(RuntimeError):
    """未安装 RAGAS 或版本不匹配。"""


def judge_config(settings):
    """解析 judge 模型配置；metadata 不包含密钥。"""
    config = replace(
        settings.evaluation,
        model=(settings.evaluation.model or "").strip(),
        base_url=settings.evaluation.base_url or settings.llm.base_url,
        api_key=settings.evaluation.api_key or settings.llm.api_key,
    )
    if not config.model:
        raise ValueError("请配置 EVAL_LLM_MODEL；评测不能复用回答模型自评")
    if not config.base_url or not config.api_key:
        raise ValueError("请配置评测接口与密钥，或复用 OPENAI_BASE_URL / OPENAI_API_KEY")
    if not math.isfinite(config.timeout_seconds) or config.timeout_seconds <= 0:
        raise ValueError("EVAL_LLM_TIMEOUT_SECONDS 必须是大于零的有限数值")
    return config, {
        "ragas_version": RAGAS_VERSION,
        "judge_model": config.model,
        "provider_sha256": hashlib.sha256(config.base_url.encode()).hexdigest(),
        "embedding_model": settings.embedding.model_path or settings.embedding.model_name or "BAAI/bge-m3",
        "embedding_device": settings.embedding.device or "cpu",
        "temperature": 0,
        "answer_relevancy_strictness": 3,
    }


def _make_embeddings():
    """用现有 BGE-M3 稠密向量适配 RAGAS 的嵌入接口。

    同步实现直接调用模型，避免在 RAGAS 的事件循环里嵌套 ``asyncio.run``。
    """
    from ragas.embeddings.base import BaseRagasEmbedding

    from app.shared.models.embedding import generate_embeddings

    class _BgeEmbeddings(BaseRagasEmbedding):
        def embed_text(self, text, **kwargs):
            return self.embed_texts([text], **kwargs)[0]

        async def aembed_text(self, text, **kwargs):
            return (await self.aembed_texts([text], **kwargs))[0]

        def embed_texts(self, texts, **kwargs):
            return _dense_vectors(generate_embeddings(list(texts)), len(texts))

        async def aembed_texts(self, texts, **kwargs):
            return await asyncio.to_thread(self.embed_texts, texts, **kwargs)

    return _BgeEmbeddings()


def _dense_vectors(payload: dict, expected: int) -> list[list[float]]:
    vectors = payload.get("dense") or []
    if len(vectors) != expected:
        raise ValueError("BGE-M3 返回的向量数量与文本数量不一致")
    for vector in vectors:
        if not vector or not all(math.isfinite(value) for value in vector):
            raise ValueError("BGE-M3 返回了无效稠密向量")
    if len({len(vector) for vector in vectors}) != 1:
        raise ValueError("BGE-M3 稠密向量维度不一致")
    return vectors


class RagasScorer:
    """一次运行内复用的 RAGAS 评分器。"""

    def __init__(self, settings):
        os_version = _ensure_ragas()
        config, self.metadata = judge_config(settings)
        self.metadata["installed_version"] = os_version

        from openai import AsyncOpenAI
        from ragas.llms import llm_factory
        from ragas.metrics.collections import (
            AnswerRelevancy,
            ContextPrecision,
            ContextRecall,
            Faithfulness,
        )

        self.client = AsyncOpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            timeout=config.timeout_seconds,
            # 云端 judge 在并发下偶发连接重置，开启重试避免整题指标丢失
            max_retries=3,
        )
        self._runner = asyncio.Runner()
        try:
            self._make_llm = partial(llm_factory, config.model, client=self.client, temperature=0, max_retries=2)
            self._retry_llm = None
            llm = self._make_llm(max_tokens=_JUDGE_MAX_TOKENS)
            self.embeddings = _make_embeddings()
            self.faithfulness = Faithfulness(llm=llm)
            self.answer_relevancy = AnswerRelevancy(llm=llm, embeddings=self.embeddings, strictness=3)
            self.context_precision = ContextPrecision(llm=llm)
            self.context_recall = ContextRecall(llm=llm)
        except BaseException:
            self.close()
            raise

    def score(self, question: str, answer: str, contexts: list[str], ground_truth: str) -> dict:
        """同步评分入口；单个指标失败只记录原因，不影响其他指标。"""
        return self._runner.run(self.ascore(question, answer, contexts, ground_truth))

    async def ascore(self, question: str, answer: str, contexts: list[str], ground_truth: str) -> dict:
        contexts = [text for text in contexts if text.strip()]
        if not answer.strip() or not contexts:
            # 没有答案或上下文时指标无定义，直接返回 None 并说明
            reason = "答案为空" if not answer.strip() else "没有可用上下文"
            return {name: None for name in METRIC_NAMES} | {"error": reason}

        inputs = {
            "faithfulness": {"user_input": question, "response": answer, "retrieved_contexts": contexts},
            "answer_relevancy": {"user_input": question, "response": answer},
            "context_precision": {"user_input": question, "reference": ground_truth, "retrieved_contexts": contexts},
            "context_recall": {"user_input": question, "reference": ground_truth, "retrieved_contexts": contexts},
        }
        scores: dict = {}
        errors: dict[str, str] = {}
        retries: list[dict] = []
        # 逐项评分：四项同时发请求会放大云端 judge 的连接抖动
        for key, kwargs in inputs.items():
            metric = getattr(self, key)
            retry = None
            try:
                try:
                    result = await metric.ascore(**kwargs)
                except Exception as exc:
                    if not _output_was_truncated(exc):
                        raise
                    retry = {
                        "metric": key, "max_tokens": _RETRY_MAX_TOKENS,
                        "previous_error": "max_tokens", "status": "scorer_failed",
                    }
                    retries.append(retry)
                    if self._retry_llm is None:
                        self._retry_llm = self._make_llm(max_tokens=_RETRY_MAX_TOKENS)
                    # 并发题目继续使用原评分器的 token 上限。
                    retry_metric = copy(metric)
                    retry_metric.llm = self._retry_llm
                    result = await retry_metric.ascore(**kwargs)
            except Exception as exc:  # noqa: BLE001 - 单指标失败不影响其余指标
                scores[key] = None
                errors[key] = str(exc) or exc.__class__.__name__
                continue
            value = getattr(result, "value", result)
            if finite_score(value):
                scores[key] = round(float(value), 4)
                if retry is not None:
                    retry["status"] = "scored"
            else:
                scores[key] = None
                errors[key] = "评分器未返回有限数值"
        if errors:
            scores["error"] = "; ".join(f"{key}: {message}" for key, message in errors.items())
        if retries:
            scores["retries"] = retries
        return scores

    def close(self) -> None:
        """关闭 judge 客户端。"""
        try:
            self._runner.run(self.client.close())
        except Exception:  # noqa: BLE001 - 关闭失败不影响报告
            pass
        finally:
            self._runner.close()


def _output_was_truncated(error: BaseException | None) -> bool:
    from instructor.core import IncompleteOutputException
    from openai import LengthFinishReasonError

    while error is not None:
        if isinstance(error, (IncompleteOutputException, LengthFinishReasonError)):
            return True
        error = error.__cause__
    return False


def _ensure_ragas() -> str:
    """确认 RAGAS 已安装且版本匹配，返回已安装版本。"""
    try:
        installed = importlib.metadata.version("ragas")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RagasUnavailable("缺少评测依赖；请执行 uv sync --group eval") from exc
    if installed != RAGAS_VERSION:
        raise RagasUnavailable(f"评测要求 ragas=={RAGAS_VERSION}，当前为 {installed}")
    return installed


def aggregate_ragas(case_rows: list[dict]) -> dict:
    """按指标做宏平均；失败样本不参与平均。"""
    summary: dict[str, float | int | dict] = {}
    for name in METRIC_NAMES:
        values = [
            row["ragas"][name]
            for row in case_rows
            if finite_score(row.get("ragas", {}).get(name))
        ]
        if values:
            summary[name] = round(sum(values) / len(values), 4)
            summary[f"{name}_n"] = len(values)
    failed = [row["case_id"] for row in case_rows if row.get("ragas", {}).get("error")]
    if failed:
        summary["failed_cases"] = failed
    return summary


def finite_score(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
