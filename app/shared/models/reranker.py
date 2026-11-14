"""BGE-Reranker 交叉编码重排模型（单例加载）。"""
from __future__ import annotations

from threading import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from FlagEmbedding import FlagReranker

from app.shared.config import settings
from app.shared.runtime.logger import logger

_reranker_model: FlagReranker | None = None
# The fast tokenizer updates shared padding and truncation settings when encoding.
reranker_tokenizer_lock = Lock()


def get_reranker_model() -> FlagReranker:
    """获取重排模型单例（首次调用加载，可能较慢）。"""
    from FlagEmbedding import FlagReranker

    global _reranker_model
    if _reranker_model is None:
        logger.info("开始初始化重排模型")
        _reranker_model = FlagReranker(
            model_name_or_path=settings.reranker.model_path,
            devices=settings.reranker.device or None,
            use_fp16=settings.reranker.fp16,
        )
        logger.success("重排模型初始化成功")
    return _reranker_model


def score_pairs(reranker: FlagReranker, pairs: list[list[str]]) -> list[float]:
    if not pairs:
        return []
    if getattr(reranker, "target_devices", None) != ["cpu"]:
        return reranker.compute_score(pairs, normalize=True)

    # FlagEmbedding probes the first batch for GPU memory, then repeats its forward
    # pass. On CPU, prepare the same inputs and score each batch once.
    import torch

    tokenizer = reranker.tokenizer
    max_length = reranker.max_length
    query_limit = reranker.query_max_length or max_length * 3 // 4
    with reranker_tokenizer_lock:
        queries = tokenizer([q for q, _ in pairs], add_special_tokens=False,
                            max_length=query_limit, truncation=True)["input_ids"]
        passages = tokenizer([p for _, p in pairs], add_special_tokens=False,
                             max_length=max_length, truncation=True)["input_ids"]
        inputs = [tokenizer.prepare_for_model(q, p, truncation="only_second", max_length=max_length, padding=False)
                  for q, p in zip(queries, passages)]
    order = sorted(range(len(inputs)), key=lambda i: len(inputs[i]["input_ids"]), reverse=True)
    scores = [0.0] * len(inputs)
    with torch.inference_mode():
        for start in range(0, len(order), reranker.batch_size):
            indexes = order[start:start + reranker.batch_size]
            batch = tokenizer.pad([inputs[i] for i in indexes], padding=True, return_tensors="pt")
            logits = reranker.model(**batch, return_dict=True).logits.flatten()
            for i, score in zip(indexes, logits.double().sigmoid().tolist()):
                scores[i] = score
    return scores
