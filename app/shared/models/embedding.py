"""BGE-M3 稠密 + 稀疏向量模型（单例加载，原生 L2 归一化）。"""
from __future__ import annotations

from pymilvus.model.hybrid import BGEM3EmbeddingFunction

from app.shared.config import settings
from app.shared.runtime.logger import logger

_DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
_DEFAULT_EMBEDDING_DEVICE = "cpu"
_bge_m3_ef: BGEM3EmbeddingFunction | None = None


def get_bge_m3_ef() -> BGEM3EmbeddingFunction:
    """获取 BGE-M3 单例（首次调用加载模型，可能较慢）。"""
    global _bge_m3_ef
    if _bge_m3_ef is not None:
        return _bge_m3_ef

    model_name = settings.embedding.model_path or settings.embedding.model_name or _DEFAULT_EMBEDDING_MODEL
    device = settings.embedding.device or _DEFAULT_EMBEDDING_DEVICE
    logger.info(
        f"开始初始化 BGE-M3 模型：model={model_name}, device={device}, fp16={settings.embedding.fp16}"
    )
    try:
        _bge_m3_ef = BGEM3EmbeddingFunction(
            model_name=model_name,
            device=device,
            use_fp16=settings.embedding.fp16,
            normalize_embeddings=True,  # 模型原生对稠密+稀疏向量做 L2 归一化（适配 Milvus IP）
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(f"BGE-M3 模型初始化失败：{exc}", exc_info=True)
        raise
    logger.success("BGE-M3 模型初始化成功")
    return _bge_m3_ef


def generate_embeddings(texts: list[str]) -> dict[str, list]:
    """为文本列表生成稠密 + 稀疏向量。

    :return: ``{"dense": [[float, ...], ...], "sparse": [{int: float}, ...]}``
    """
    if not isinstance(texts, list) or len(texts) == 0:
        raise ValueError("参数 texts 必须是包含文本的非空列表")
    if any(not isinstance(text, str) for text in texts):
        raise ValueError("参数 texts 必须是字符串列表")

    try:
        model = get_bge_m3_ef()
        embeddings = model.encode_documents(texts)
        sparse = embeddings["sparse"]
        processed_sparse = []
        for i in range(len(texts)):
            start, end = sparse.indptr[i], sparse.indptr[i + 1]
            # 稀疏矩阵按行拆成 {特征索引: 权重}，并转成 Python 原生类型以便序列化
            indices = sparse.indices[start:end].tolist()
            data = sparse.data[start:end].tolist()
            processed_sparse.append(dict(zip(indices, data)))
        return {
            "dense": [emb.tolist() for emb in embeddings["dense"]],
            "sparse": processed_sparse,
        }
    except Exception as exc:  # noqa: BLE001 - 不吞异常，交由调用方决定重试/降级
        logger.error(f"文本向量生成失败：{exc}", exc_info=True)
        raise


def embed_text(text: str) -> dict[str, list]:
    """单条文本向量化。

    返回 ``{"dense": [vec], "sparse": [{...}]}``（单元素列表，便于多路检索复用同一份向量）。
    """
    return generate_embeddings([text])
