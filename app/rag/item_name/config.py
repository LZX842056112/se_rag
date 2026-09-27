"""主体名相关的配置常量。

从 ``app/rag/query/config.py`` 迁出：这些常量被导入端与查询端共用，
放在 query 下会让导入端反向依赖查询端。

这里刻意**不**维护"别名组"名单（也不需要环境变量）：同一实体的多种写法
（如 `HAK 180 烫金机` 与 `Brother HAK 180烫金机`）由两处通用机制收敛：
  1. 写入端：导入识别后按向量判定归并到库内已有同一实体，从源头避免近重复名
     （见 ``app/rag/import_/item_name_service.py::resolve_item_name_against_catalog``）；
  2. 读取端：``catalog.is_same_entity`` 按"短名是长名后缀"判同一实体，
     用于间距判定与召回扩展，兜底存量数据。
"""
from app.shared.config.common import env_float, env_str

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
