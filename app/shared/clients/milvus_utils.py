"""
工具模块，负责提供 milvus 相关的辅助能力。
"""
from pymilvus import MilvusClient, AnnSearchRequest, WeightedRanker, DataType
from app.shared.config.milvus_config import milvus_config
from app.shared.runtime.logger import logger

# 全局Milvus客户端实例，实现单例复用
_milvus_client: MilvusClient | None = None


def get_milvus_client() -> MilvusClient | None:
    """
    Milvus客户端单例获取方法
    实现客户端连接复用，避免重复创建连接消耗资源
    :return: MilvusClient实例，连接失败返回None
    """
    try:
        global _milvus_client
        # 单例判断：未初始化则创建新连接
        if _milvus_client is None:
            milvus_uri = milvus_config.milvus_url
            # 校验Milvus连接地址配置
            if not milvus_uri:
                logger.error("Milvus客户端连接失败：缺少MILVUS_URL环境变量配置")
                return None
            # 初始化Milvus客户端
            _milvus_client = MilvusClient(uri=milvus_uri)
            logger.info("Milvus客户端连接成功")
        return _milvus_client
    except Exception as e:
        logger.error(f"Milvus客户端连接异常：{str(e)}", exc_info=True)
        return None


def register_vector_fields_and_indexes(schema, index_params, *, dense_metric: str = "IP",
                                       dense_dim: int = 1024):
    """
    为集合 schema 注册统一的稠密/稀疏向量字段，并在 index_params 上添加对应索引。

    各集合（kb_chunks / kb_item_names / kb_evolution_items）的标量字段各异，
    但向量字段与索引完全一致，故抽到此处复用，保持三处索引口径统一。

    :param schema: Milvus 的 CollectionSchema 对象（已用 add_field 添加标量字段）
    :param index_params: prepare_index_params() 返回的索引参数对象
    :param dense_metric: 稠密向量相似度度量，默认 IP；BGE-M3 稠密向量已 L2 归一化，
                         与 COSINE 排序等价。因历史原因部分集合用 COSINE，按需传入。
    :param dense_dim: 稠密向量维度，默认 1024（BGE-M3）
    :return: 无
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


def create_hybrid_search_requests(dense_vector, sparse_vector, dense_params=None, sparse_params=None, expr=None,
                                  limit=5):
    """
    构建Milvus混合搜索请求对象
    分别创建稠密/稀疏向量的搜索请求，用于后续混合搜索融合
    :param dense_vector: 文本生成的稠密向量
    :param sparse_vector: 文本生成的稀疏向量
    :param dense_params: 稠密向量搜索参数，默认使用余弦相似度
    :param sparse_params: 稀疏向量搜索参数，默认使用内积相似度
    :param expr: 搜索过滤表达式，用于精准筛选数据
    :param limit: 单向量搜索返回结果数量，默认5
    :return: 搜索请求列表，包含[dense_req, sparse_req]
    """

    # 稠密向量默认搜索参数：内积（IP）。BGE-M3 稠密向量已 L2 归一化，
    # 与现有 kb_chunks/kb_item_names 集合的 AUTOINDEX(IP) 一致，IP==COSINE 排序等价。
    if dense_params is None:
        dense_params = {"metric_type": "IP"}
    # 稀疏向量默认搜索参数：内积（IP），适配BGE-M3稀疏向量
    if sparse_params is None:
        sparse_params = {"metric_type": "IP"}

    # 稠密向量搜索请求：关联 dense_vector 字段
    dense_req = AnnSearchRequest(
        data=[dense_vector],
        anns_field="dense_vector",
        param=dense_params,
        expr=expr,  # 混合搜索的过滤条件
        limit=limit
    )

    # 稀疏向量搜索请求：关联 sparse_vector 字段
    sparse_req = AnnSearchRequest(
        data=[sparse_vector],
        anns_field="sparse_vector",
        param=sparse_params,
        expr=expr,
        limit=limit
    )

    return [dense_req, sparse_req]


def hybrid_search(client, collection_name, reqs, ranker_weights=(0.5, 0.5), norm_score=False, limit=5,
                  output_fields=None, search_params=None):
    """
    执行Milvus稠密+稀疏向量混合搜索
    基于WeightedRanker实现双向量搜索结果加权融合，提升检索准确性
    :param client: MilvusClient实例
    :param collection_name: 集合名称
    :param reqs: 搜索请求列表，固定为[dense_req, sparse_req]
    :param ranker_weights: 加权融合权重，默认(0.5,0.5)，依次对应稠密/稀疏向量
    :param norm_score: 是否归一化评分后再融合，避免评分量级差异导致权重失效
    :param limit: 混合搜索最终返回结果数量，默认5
    :param output_fields: 需要返回的字段列表，默认返回item_name
    :param search_params: 搜索参数，如ef/topk等，默认None
    :return: 混合搜索结果列表，搜索失败返回None
    """
    try:
        # 初始化加权排名器：按权重融合稠密/稀疏向量的搜索结果
        # norm_score=True：先将两个向量评分归一化到0~1区间，再加权计算
        # 权重排名器  *nums =   0.5  0.5  0.5 0.5 + = 1  -> reqs -> [列表 -> AnnSearchRequest..]
        # norm_score=True -> 分数区间拉倒 0 - 1 分 -> 稠密向量 满分是 1分 稀疏向量满分 0.75
        rerank = WeightedRanker(ranker_weights[0], ranker_weights[1], norm_score=norm_score)
        # RRFRanker
        # RRFRanker(k=100)
        # 默认返回字段：文档标识字段
        if output_fields is None:
            output_fields = ["item_name"]

        # 执行混合搜索：融合稠密+稀疏向量结果，按权重重新排序
        res = client.hybrid_search(
            collection_name=collection_name,
            reqs=reqs,
            ranker=rerank,
            limit=limit,
            output_fields=output_fields,
            search_params=search_params
        )
        # res [[{id:111,distance:0.9,entity:{item_name:烫金机}},{},{},{},{}]]  || data = [1,2,3] => [[],[],[]]
        logger.info(f"Milvus混合搜索完成，集合[{collection_name}]共检索到{len(res[0])}条结果")
        return res
    except Exception as e:
        logger.error(f"Milvus混合搜索执行失败，集合[{collection_name}]：{str(e)}", exc_info=True)
        return None
