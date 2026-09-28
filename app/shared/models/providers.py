"""模型能力统一入口：业务层通过 ``llm_providers`` 获取对话/视觉/向量/重排能力。"""
from __future__ import annotations

from langchain_openai import ChatOpenAI

from app.shared.config import settings
from app.shared.models.embedding import embed_text, generate_embeddings, get_bge_m3_ef
from app.shared.models.llm import get_llm_client
from app.shared.models.reranker import get_reranker_model


class LLMProvider:
    """模型能力门面（薄封装，便于业务层替换与测试打桩）。"""

    def chat(self, model_name: str | None = None, json_mode: bool = False) -> ChatOpenAI:
        """获取对话模型；``model_name`` 为空时使用配置中的默认模型。"""
        return get_llm_client(model=model_name, json_mode=json_mode)

    def vision_chat(self, vision_model_name: str | None = None) -> ChatOpenAI:
        """获取视觉语言模型（用于 Markdown 图片理解）。"""
        return get_llm_client(vision_model_name or settings.llm.vl_model)

    def bge_m3_embedding(self):
        """获取 BGE-M3 模型对象。"""
        return get_bge_m3_ef()

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
