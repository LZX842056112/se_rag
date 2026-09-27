from app.shared.config.common import env_str

NODE_RRF_K = 60
NODE_RRF_LIMIT_TOP = 5

# reranker重排序参数
RERANK_MAX_TOPK: int = 6
RERANK_MIN_TOPK: int = 2
RERANK_GAP_RATIO: float = 0.2
RERANK_GAP_ABS: float = 0.2
RERANK_MAX_INPUT_TOKENS: int = 512
RERANK_SUMMARY_CHAR_RATIO: float = 1.3
RERANK_MIN_SUMMARY_CHARS: int = 50

SUPPORTED_IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp")


# ==================== 自进化读侧参数读取 ====================
# 优先取 param_registry；未开启或未命中回退到本文件常量（现状值）。
def _read_param(key: str):
    from app.evolution.config import evolution_config
    if not getattr(evolution_config, "enabled", False):
        return None
    try:
        # 走 tuning/param_registry 进程内缓存，避免每次查 Mongo；未命中回退常量
        from app.evolution.tuning.param_registry import get_param
        return get_param(key)
    except Exception:
        return None


def get_rrf_k() -> int:
    v = _read_param("RRF_K")
    return int(v) if isinstance(v, (int, float)) else NODE_RRF_K


def get_rrf_top() -> int:
    v = _read_param("RRF_TOP")
    return int(v) if isinstance(v, (int, float)) else NODE_RRF_LIMIT_TOP


def get_rerank_topk() -> int:
    v = _read_param("RERANK_TOP_K")
    return int(v) if isinstance(v, (int, float)) else RERANK_MAX_TOPK


# 主体确认判定参数与主体名目录参数见 app/rag/item_name/config.py
# （导入端与查询端共用，放在 query 下会让导入端反向依赖查询端）。

# kb_chunks 集合 dense 向量检索的 metric_type。
# 该集合由 app/rag/import_/index_service.py 以 metric_type="COSINE" 创建（sparse 用 IP），
# 而 shared/clients/milvus_utils.create_hybrid_search_requests 的默认值是 IP，
# 不一致会让 kb_chunks 检索直接报 "metric type not match" 并静默返回 None，
# 表现为"召回 0 条 -> rrf_service 抛错 -> /query 500"。
# BGE-M3 稠密向量已 L2 归一化，COSINE 与 IP 排序等价，故对齐集合实际口径。
CHUNK_DENSE_METRIC: str = env_str("CHUNK_DENSE_METRIC", "COSINE")
