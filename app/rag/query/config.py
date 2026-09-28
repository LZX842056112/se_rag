"""查询链路参数（阈值、检索条数、动态截断参数）。"""
from __future__ import annotations

from app.shared.config import settings
from app.shared.config.common import env_int, env_str

NODE_RRF_K = 60
NODE_RRF_LIMIT_TOP = 5

# reranker 重排序参数
RERANK_MAX_TOPK: int = 6
RERANK_MIN_TOPK: int = 2
RERANK_GAP_RATIO: float = 0.2
RERANK_GAP_ABS: float = 0.2
RERANK_MAX_INPUT_TOKENS: int = 512
RERANK_SUMMARY_CHAR_RATIO: float = 1.3
RERANK_MIN_SUMMARY_CHARS: int = 50
# 长文本压缩的并发上限（超过该值的文档串行处理，避免打爆模型侧限流）
RERANK_SUMMARY_MAX_WORKERS: int = 4
# 最终上下文里联网结果的条数上限：本地（知识库 / 自进化）有命中时，联网只作补充。
# 事故背景：某次提问的 4 条联网结果分数 0.999x，把本地手册与已审批 FAQ 全部挤出上下文，
# 表现为「明明入库了却仍答无法作答且无引用」。
WEB_MAX_IN_CONTEXT: int = env_int("WEB_MAX_IN_CONTEXT", 2)

# kb_chunks 集合 dense 向量检索的 metric_type。
# 该集合由导入端以 metric_type="COSINE" 创建（sparse 用 IP），而混合检索请求的默认值是
# IP；不一致会让 Milvus 报 "metric type not match" 并静默返回 None，表现为「召回 0 条 →
# RRF 抛错 → /query 500」。BGE-M3 稠密向量已 L2 归一化，COSINE 与 IP 排序等价。
CHUNK_DENSE_METRIC: str = env_str("CHUNK_DENSE_METRIC", "COSINE")


# ==================== 自进化读侧参数读取 ====================
def _read_param(key: str):
    """优先取参数注册表（自调）中的值；未开启或未命中时返回 None，由调用方回退常量。"""
    if not settings.evolution.enabled:
        return None
    try:
        # 走 param_registry 进程内缓存，避免每次查 Mongo
        from app.evolution.tuning.param_registry import get_param
        return get_param(key)
    except Exception:  # noqa: BLE001 - 参数注册表属增强能力，异常回退常量
        return None


def get_rrf_k() -> int:
    """RRF 平滑参数 k。"""
    value = _read_param("RRF_K")
    return int(value) if isinstance(value, (int, float)) else NODE_RRF_K


def get_rrf_top() -> int:
    """RRF 融合后保留条数。"""
    value = _read_param("RRF_TOP")
    return int(value) if isinstance(value, (int, float)) else NODE_RRF_LIMIT_TOP


def get_rerank_topk() -> int:
    """重排后最多保留条数。"""
    value = _read_param("RERANK_TOP_K")
    return int(value) if isinstance(value, (int, float)) else RERANK_MAX_TOPK
