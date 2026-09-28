"""主体名相关的配置常量（导入端与查询端共用）。"""
from __future__ import annotations

from app.shared.config.common import env_float, env_int, env_str

# 未打商品标签的知识条目使用该占位主体名，视为「全局可召回」
GLOBAL_ITEM_NAME = "default_item_name"

# ==================== 主体确认判定参数（env 可覆盖） ====================
# 为什么不用单一绝对阈值：item_name 向量检索的分数量级依赖 Milvus 版本与 norm_score
# 语义（实测「提问与库内主体名完全相同」也仅 0.662~0.664），一刀切下调会把「永远反问」
# 换成「静默认错」。故判定以「top1 下限 + top1/top2 间距」为主，精确同名再叠加目录直通。
ITEM_NAME_CONFIRM_MIN_SCORE: float = env_float("ITEM_NAME_CONFIRM_MIN_SCORE", 0.65)
ITEM_NAME_CONFIRM_MARGIN: float = env_float("ITEM_NAME_CONFIRM_MARGIN", 0.02)
ITEM_NAME_OPTION_MIN_SCORE: float = env_float("ITEM_NAME_OPTION_MIN_SCORE", 0.60)
# 主体名目录进程内缓存 TTL（秒）
ITEM_NAME_CATALOG_TTL_SECONDS: float = env_float("ITEM_NAME_CATALOG_TTL_SECONDS", 60.0)
# 主体名检索返回条数：需覆盖 top2（不同主体）以计算间距
ITEM_NAME_SEARCH_LIMIT: int = env_int("ITEM_NAME_SEARCH_LIMIT", 10)

# 主体名集合 dense 向量检索的 metric_type，必须与集合实际索引一致
# （kb_item_names 由导入端以 HNSW/COSINE 创建，sparse 用 IP）。BGE-M3 稠密向量已 L2
# 归一化，COSINE 与 IP 排序等价，故按集合实际口径对齐；不一致会让 Milvus 报
# "metric type not match" 并静默返回 None。
ITEM_NAME_DENSE_METRIC: str = env_str("ITEM_NAME_DENSE_METRIC", "COSINE")
