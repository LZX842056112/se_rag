"""模型能力统一入口：业务层通过 ``llm_providers`` 获取对话/视觉/向量/重排能力。"""
from __future__ import annotations

from collections.abc import Iterator

import httpx
from langchain_openai import ChatOpenAI
from openai import APIConnectionError

from app.shared.config import settings
from app.shared.models.embedding import embed_text, generate_embeddings
from app.shared.models.llm import get_llm_client
from app.shared.models.reranker import get_reranker_model
from app.shared.runtime.deadline import remaining
from app.shared.runtime.logger import logger
from app.shared.runtime.query_metrics import instrument_client, mark_degraded


class LLMProvider:
    """模型能力门面（薄封装，便于业务层替换与测试打桩）。"""

    def chat(self, model_name: str | None = None, json_mode: bool = False, *, timeout_seconds: float | None = None, max_retries: int | None = None) -> ChatOpenAI:
        """获取对话模型；``model_name`` 为空时使用配置中的默认模型。"""
        options = {}
        available = remaining()
        if available is not None:
            timeout_seconds = min(available, timeout_seconds or settings.import_.llm_timeout_seconds)
            max_retries = 0
        if timeout_seconds is not None:
            options["timeout_seconds"] = timeout_seconds
        if max_retries is not None:
            options["max_retries"] = max_retries
        return instrument_client(get_llm_client(model=model_name, json_mode=json_mode, **options), model_name)

    def stream_chat_text(self, messages) -> Iterator[str]:
        """Retry one transport failure before the model emits any text."""
        for attempt in range(2):
            emitted_text = False
            try:
                for chunk in self.chat().stream(messages):
                    if chunk.content:
                        emitted_text = True
                        yield chunk.content
                return
            except (APIConnectionError, httpx.NetworkError, httpx.RemoteProtocolError, httpx.TimeoutException) as exc:
                if emitted_text or attempt:
                    raise
                available = remaining()
                if available is not None and available < 1.0:
                    raise
                mark_degraded("llm_stream_retry")
                logger.warning(f"流式模型在输出文本前断开，重试一次。错误类型 {type(exc).__name__}")

    def vision_chat(self, vision_model_name: str | None = None) -> ChatOpenAI:
        """获取视觉语言模型（用于 Markdown 图片理解）。"""
        return get_llm_client(vision_model_name or settings.llm.vl_model)

    def generate_embeddings(self, texts: list[str]) -> dict[str, list]:
        """批量生成稠密 + 稀疏向量。"""
        return generate_embeddings(texts)

    def embed_text(self, text: str) -> dict[str, list]:
        """单条文本向量化（同一次查询的多路检索可复用该结果）。"""
        return embed_text(text)

    def reranker_model(self):
        """获取重排模型对象。"""
        return get_reranker_model()


llm_providers = LLMProvider()
