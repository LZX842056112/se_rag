"""BGE-Reranker 交叉编码重排模型（单例加载）。"""
from __future__ import annotations

from FlagEmbedding import FlagReranker

from app.shared.config import settings
from app.shared.runtime.logger import logger

_reranker_model: FlagReranker | None = None


def get_reranker_model() -> FlagReranker:
    """获取重排模型单例（首次调用加载，可能较慢）。"""
    global _reranker_model
    if _reranker_model is None:
        logger.info("开始初始化重排模型")
        _reranker_model = FlagReranker(
            model_name_or_path=settings.reranker.model_path,
            device=settings.reranker.device,
            use_fp16=settings.reranker.fp16,
        )
        logger.success("重排模型初始化成功")
    return _reranker_model
