"""Optional Ragas integration. Importing this module does not load Ragas."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import hashlib
import importlib.metadata
import math
import os

RAGAS_VERSION = "0.4.3"


def evaluation_config(settings):
    """Resolve centralized settings; metadata never includes API keys."""
    config = replace(settings.evaluation, model=settings.evaluation.model.strip(),
                     base_url=settings.evaluation.base_url or settings.llm.base_url,
                     api_key=settings.evaluation.api_key or settings.llm.api_key)
    if not config.model:
        raise ValueError("请明确配置 EVAL_LLM_MODEL；不能自动使用回答模型评分")
    if not config.base_url or not config.api_key:
        raise ValueError("请配置评测接口与密钥，或现有 OPENAI_BASE_URL / OPENAI_API_KEY")
    if not math.isfinite(config.timeout_seconds) or config.timeout_seconds <= 0:
        raise ValueError("EVAL_LLM_TIMEOUT_SECONDS 必须是大于零的有限数值")
    return config, {
        "ragas_version": RAGAS_VERSION, "judge_model": config.model,
        "provider_sha256": hashlib.sha256(config.base_url.encode()).hexdigest(),
        "embedding_model": settings.embedding.model_path or settings.embedding.model_name or "BAAI/bge-m3",
        "embedding_device": settings.embedding.device or "cpu", "embedding_fp16": settings.embedding.fp16,
        "temperature": 0, "top_p": 1, "max_tokens": 4096, "instructor_max_attempts": 2,
        "metric_timeout_seconds": config.timeout_seconds, "answer_relevancy_strictness": 3,
        "factual_correctness_mode": "f1", "factual_correctness_atomicity": "low",
        "factual_correctness_coverage": "low", "prompt_language": "ragas-default",
        "embedding_execution": "single_cancellable_process",
    }


def make_bge_embeddings(encode=None):
    """Use the existing dense BGE-M3 vectors through the modern embedding API."""
    from ragas.embeddings.base import BaseRagasEmbedding
    from app.rag_eval.embedding_worker import DenseEmbeddingWorker
    if encode is None:
        from app.shared.models.embedding import generate_embeddings
        encode = generate_embeddings

    class BGEEmbeddings(BaseRagasEmbedding):
        def __init__(self):
            super().__init__()
            self.worker = DenseEmbeddingWorker(encode)

        def embed_text(self, text, **kwargs):
            return self.embed_texts([text], **kwargs)[0]

        async def aembed_text(self, text, **kwargs):
            return (await self.aembed_texts([text], **kwargs))[0]

        def embed_texts(self, texts, **kwargs):
            return asyncio.run(self.aembed_texts(texts, **kwargs))

        async def aembed_texts(self, texts, **kwargs):
            if not texts:
                return []
            vectors = (await self.worker.encode(texts))["dense"]
            if len(vectors) != len(texts) or any(not v or not all(math.isfinite(x) for x in v)
                                                or not any(x != 0 for x in v) for v in vectors):
                raise ValueError("BGE-M3 返回了无效稠密向量")
            if len({len(v) for v in vectors}) != 1:
                raise ValueError("BGE-M3 稠密向量维度不一致")
            return vectors

        def close(self):
            self.worker.close()

    return BGEEmbeddings()


class _EvaluationResources:
    def __init__(self, client, embeddings):
        self.client = client
        self.embeddings = embeddings

    async def close(self):
        try:
            await self.client.close()
        finally:
            self.embeddings.close()


def create_ragas_scorers(config):
    os.environ["RAGAS_DO_NOT_TRACK"] = "true"
    try:
        installed = importlib.metadata.version("ragas")
    except importlib.metadata.PackageNotFoundError as exc:
        raise RuntimeError("缺少可选评测依赖；请执行 uv sync --group eval") from exc
    if installed != RAGAS_VERSION:
        raise RuntimeError(f"评测要求 ragas=={RAGAS_VERSION}，当前为 {installed}")
    from openai import AsyncOpenAI
    from ragas.llms import llm_factory
    from ragas.metrics.collections import AnswerRelevancy, FactualCorrectness, Faithfulness

    client = AsyncOpenAI(base_url=config.base_url, api_key=config.api_key,
                         timeout=config.timeout_seconds, max_retries=0)
    llm = llm_factory(config.model, client=client, temperature=0, top_p=1,
                      max_tokens=4096, max_retries=2)
    embeddings = make_bge_embeddings()
    return {
        "faithfulness": Faithfulness(llm=llm),
        "answer_relevancy": AnswerRelevancy(llm=llm, embeddings=embeddings, strictness=3),
        "factual_correctness": FactualCorrectness(llm=llm, mode="f1", atomicity="low", coverage="low"),
    }, _EvaluationResources(client, embeddings)
