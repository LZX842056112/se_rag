"""
Milvus 访问层：客户端单例、集合名、索引注册、混合检索与过滤表达式工具。

历史问题：
1. ``app.infra.vector_store.milvus_gateway`` 只是对 ``shared.clients.milvus_utils`` 的
   转发包装，两层做同一件事；现合并为单文件。
2. 各业务处自己拼 ``item_name in [...]`` 且未转义（Python repr），主体名含引号时
   Milvus 表达式解析失败甚至可被注入；现统一用 ``in_expr`` / ``eq_expr`` 构造。
"""
from __future__ import annotations

from typing import Any, Optional

from pymilvus import AnnSearchRequest, DataType, MilvusClient, WeightedRanker

from app.shared.config import settings
from app.shared.runtime.logger import logger

_milvus_client: Optional[MilvusClient] = None


def get_milvus_client() -> Optional[MilvusClient]:
    """获取全局单例 MilvusClient；连接失败返回 None（调用方需容忍）。"""
    global _milvus_client
    try:
        if _milvus_client is None:
            uri = settings.milvus.url
            if not uri:
                logger.error("Milvus 客户端连接失败：缺少 MILVUS_URL 环境变量配置")
                return None
            _milvus_client = MilvusClient(uri=uri)
            logger.info("Milvus 客户端连接成功")
        return _milvus_client
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Milvus 客户端连接异常：{exc}", exc_info=True)
        return None


def escape_milvus_string(value: Any) -> str:
    """Milvus 单引号字符串字面量转义：反斜杠与单引号，并把换行折成空格。"""
    if value is None:
        return ""
    text = str(value).replace("\\", "\\\\").replace("'", "\\'")
    return text.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def eq_expr(field: str, value: Any) -> str:
    """构造 ``field == 'value'`` 过滤表达式（自动转义）。"""
    return f"{field} == '{escape_milvus_string(value)}'"


def in_expr(field: str, values: list[Any]) -> str:
    """构造 ``field in ['a','b']`` 过滤表达式（自动转义）。"""
    literals = ", ".join(f"'{escape_milvus_string(v)}'" for v in values)
    return f"{field} in [{literals}]"


def register_vector_fields_and_indexes(schema, index_params, *, dense_metric: str = "IP",
                                       dense_dim: int = 1024) -> None:
    """为集合 schema 注册统一的稠密/稀疏向量字段与索引。

    各集合（kb_chunks / kb_item_names / kb_evolution_items）标量字段各异，但向量字段
    与索引完全一致，故集中一处维护，保证三处索引口径统一。

    :param dense_metric: 稠密向量度量，默认 IP；BGE-M3 稠密向量已 L2 归一化，
                         与 COSINE 排序等价，历史集合以 COSINE 创建故按需传入。
    :param dense_dim: 稠密向量维度，默认 1024（BGE-M3）。
    """
    schema.add_field(field_name="dense_vector", datatype=DataType.FLOAT_VECTOR, dim=dense_dim)
    schema.add_field(field_name="sparse_vector", datatype=DataType.SPARSE_FLOAT_VECTOR)

    index_params.add_index(
        field_name="dense_vector",
        index_type="HNSW",
        index_name="dense_vector_index",
        metric_type=dense_metric,
        params={"M": 64, "efConstruction": 100},
    )
    index_params.add_index(
        field_name="sparse_vector",
        index_type="SPARSE_INVERTED_INDEX",
        index_name="sparse_vector_index",
        metric_type="IP",
        params={"inverted_index_algo": "DAAT_MAXSCORE"},
    )


def create_hybrid_search_requests(dense_vector, sparse_vector, dense_params=None,
                                  sparse_params=None, expr=None, limit=5) -> list[AnnSearchRequest]:
    """构造「稠密 + 稀疏」两路检索请求。"""
    dense_params = dense_params or {"metric_type": "IP"}
    sparse_params = sparse_params or {"metric_type": "IP"}
    return [
        AnnSearchRequest(
            data=[dense_vector], anns_field="dense_vector", param=dense_params, expr=expr, limit=limit
        ),
        AnnSearchRequest(
            data=[sparse_vector], anns_field="sparse_vector", param=sparse_params, expr=expr, limit=limit
        ),
    ]


def hybrid_search(client, collection_name, reqs, ranker_weights=(0.5, 0.5), norm_score=False,
                  limit=5, output_fields=None, search_params=None):
    """执行加权混合检索；失败返回 None（调用方降级为空结果）。"""
    if client is None:
        logger.error(f"Milvus 混合搜索跳过：客户端不可用，集合[{collection_name}]")
        return None
    try:
        ranker = WeightedRanker(ranker_weights[0], ranker_weights[1], norm_score=norm_score)
        res = client.hybrid_search(
            collection_name=collection_name,
            reqs=reqs,
            ranker=ranker,
            limit=limit,
            output_fields=output_fields if output_fields is not None else ["item_name"],
            search_params=search_params,
        )
        logger.debug(f"Milvus 混合搜索完成，集合[{collection_name}]命中{len(res[0]) if res else 0}条")
        return res
    except Exception as exc:  # noqa: BLE001
        logger.error(f"Milvus 混合搜索执行失败，集合[{collection_name}]：{exc}", exc_info=True)
        return None


class MilvusGateway:
    """集合名与客户端访问入口（业务层不直接读配置）。"""

    @property
    def chunk_collection_name(self) -> str:
        """知识库分块集合名。"""
        return settings.milvus.chunks_collection

    @property
    def item_name_collection_name(self) -> str:
        """主体名集合名。"""
        return settings.milvus.item_name_collection

    @property
    def evolution_collection_name(self) -> str:
        """自进化知识条目集合名。"""
        return settings.evolution.collection

    @property
    def milvus_client(self):
        """全局单例客户端（连接失败返回 None）。"""
        return get_milvus_client()

    def create_requests(self, dense_vector, sparse_vector, expr: str = None, limit: int = 5,
                        dense_params: dict | None = None, sparse_params: dict | None = None):
        """构造混合检索请求（默认沿用底层 IP 度量）。"""
        return create_hybrid_search_requests(
            dense_vector=dense_vector,
            sparse_vector=sparse_vector,
            dense_params=dense_params,
            sparse_params=sparse_params,
            expr=expr,
            limit=limit,
        )

    def hybrid_search(self, *, collection_name: str, reqs: list[Any],
                      ranker_weights: tuple[float, float] = (0.5, 0.5), norm_score: bool = False,
                      limit: int = 5, output_fields: list[str] | None = None,
                      search_params: dict | None = None):
        """执行混合检索（见模块级 ``hybrid_search``）。"""
        return hybrid_search(
            client=self.milvus_client,
            collection_name=collection_name,
            reqs=reqs,
            ranker_weights=ranker_weights,
            norm_score=norm_score,
            limit=limit,
            output_fields=output_fields,
            search_params=search_params,
        )


milvus_gateway = MilvusGateway()
