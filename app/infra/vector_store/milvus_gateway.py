from typing import Any

from app.infra.config.providers import infra_config
from app.shared.clients import get_milvus_client, create_hybrid_search_requests, hybrid_search


class MilvusGateway:
    """Milvus 网关：屏蔽底层配置细节，统一向各调用方提供集合名与客户端。"""

    @property
    def chunk_collection_name(self) -> str:
        """知识库分块集合名。"""
        return infra_config.milvus_config.chunks_collection

    @property
    def item_name_collection_name(self) -> str:
        """商品名集合名（用于商品识别/目录/导入）。"""
        return infra_config.milvus_config.item_name_collection

    @property
    def evolution_collection_name(self) -> str:
        """自进化知识条目集合名。"""
        return infra_config.milvus_config.evolution_collection

    @property
    def milvus_client(self):
        """获取全局单例 Milvus 客户端（连接失败返回 None）。"""
        return get_milvus_client()

    def create_requests(
            self,
            dense_vector: list[float],
            sparse_vector: dict[int, float],
            expr: str = None,
            limit: int = 5,
            dense_params: dict | None = None,
            sparse_params: dict | None = None,
    ):
        # dense_params/sparse_params 默认 None -> 沿用底层 IP 默认值，保持既有调用方行为不变；
        # 需要与"非 IP 索引"的集合（如 kb_item_names 的 HNSW/COSINE）对齐时显式传入。
        return create_hybrid_search_requests(
            dense_vector=dense_vector,
            sparse_vector=sparse_vector,
            dense_params=dense_params,
            sparse_params=sparse_params,
            expr=expr,
            limit=limit,
        )

    def hybrid_search(
            self,
            *,
            collection_name: str,
            reqs: list[Any],
            ranker_weights: tuple[float, float] = (0.5, 0.5),
            norm_score: bool = False,
            limit: int = 5,
            output_fields: list[str] | None = None,
            search_params: dict | None = None,
    ):
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