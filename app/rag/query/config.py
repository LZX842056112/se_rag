
from app.shared.config.common import env_float, env_str

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


# ==================== 主体确认判定参数（P0 修复，env 可覆盖） ====================
# 为什么不用单一绝对阈值：item_name 向量检索的分数量级依赖 Milvus 版本与 norm_score
# 语义（旧注释假设满分≈0.875，实测"提问与库内主体名完全相同"也仅 0.662~0.664），
# 一刀切下调会把"永远反问"换成"静默认错"（如 query=HAK 180 时 top1 与 top2 仅差 0.014）。
# 故判定以「top1 下限 + top1/top2 间距」为主，精确同名再叠加"库内标准名直通"。
ITEM_NAME_CONFIRM_MIN_SCORE: float = env_float("ITEM_NAME_CONFIRM_MIN_SCORE", 0.65)
ITEM_NAME_CONFIRM_MARGIN: float = env_float("ITEM_NAME_CONFIRM_MARGIN", 0.02)
ITEM_NAME_OPTION_MIN_SCORE: float = env_float("ITEM_NAME_OPTION_MIN_SCORE", 0.60)
# 主体名目录（kb_item_names 全量主体名）进程内缓存 TTL（秒）
ITEM_NAME_CATALOG_TTL_SECONDS: float = env_float("ITEM_NAME_CATALOG_TTL_SECONDS", 60.0)
# 主体名检索返回条数：需覆盖 top2（不同主体）以计算间距
ITEM_NAME_SEARCH_LIMIT: int = int(env_float("ITEM_NAME_SEARCH_LIMIT", 10))
# 主体名集合 dense 向量检索的 metric_type。
# 必须与集合实际索引一致：kb_item_names 由 app/rag/import_/item_name_service.py
# 以 metric_type="COSINE" 创建（dense_vector 用 HNSW/COSINE、sparse_vector 用 IP），
# 而 shared/clients/milvus_utils.create_hybrid_search_requests 的默认值是 IP，
# 二者不一致会导致检索直接报 "metric type not match" 并静默返回 None。
# BGE-M3 稠密向量已 L2 归一化，COSINE 与 IP 排序等价，故对齐集合实际口径即可。
ITEM_NAME_DENSE_METRIC: str = env_str("ITEM_NAME_DENSE_METRIC", "COSINE")

# kb_chunks 集合 dense 向量检索的 metric_type。
# 该集合由 app/rag/import_/index_service.py 以 metric_type="COSINE" 创建（sparse 用 IP），
# 而 shared/clients/milvus_utils.create_hybrid_search_requests 的默认值是 IP，
# 不一致会让 kb_chunks 检索直接报 "metric type not match" 并静默返回 None，
# 表现为"召回 0 条 -> rrf_service 抛错 -> /query 500"。
# BGE-M3 稠密向量已 L2 归一化，COSINE 与 IP 排序等价，故对齐集合实际口径。
CHUNK_DENSE_METRIC: str = env_str("CHUNK_DENSE_METRIC", "COSINE")