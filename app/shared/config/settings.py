"""
全局配置聚合模块（全项目唯一配置出口）。

设计约定：
1. 只有 ``common`` + 本模块读取环境变量，业务模块统一使用 ``settings``；
2. 每个领域一个子配置 dataclass，避免同一环境变量在多处重复解析
   （历史问题：``EVOLUTION_COLLECTION`` 曾被 Milvus 配置与自进化配置各读一次）；
3. 字段命名统一 snake_case，解析集中在本文件，便于比对与审查。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.shared.config.common import env_bool, env_float, env_int, env_str


@dataclass
class LLMSettings:
    """对话 / 视觉模型（OpenAI 兼容协议）配置。"""

    base_url: str
    api_key: str
    llm_model: str
    vl_model: str
    temperature: float


@dataclass
class EmbeddingSettings:
    """BGE-M3 稠密+稀疏向量模型配置。"""

    model_path: str
    model_name: str
    device: str
    fp16: bool


@dataclass
class RerankerSettings:
    """BGE-Reranker 重排模型配置。"""

    model_path: str
    device: str
    fp16: bool


@dataclass
class MilvusSettings:
    """Milvus 连接与集合名（集合名属于数据契约，改名需同步迁移存量数据）。"""

    url: str
    chunks_collection: str
    item_name_collection: str
    evolution_collection: str


@dataclass
class MinioSettings:
    """MinIO 对象存储配置。"""

    endpoint: str
    access_key: str
    secret_key: str
    bucket_name: str
    image_dir: str
    secure: bool


@dataclass
class MineruSettings:
    """MinerU（PDF → Markdown）服务配置。"""

    base_url: str
    api_key: str


@dataclass
class McpSettings:
    """百炼联网搜索 MCP 配置。"""

    base_url: str
    api_key: str
    timeout_seconds: int


@dataclass
class MongoSettings:
    """MongoDB 连接与集合名（集合名属于数据契约）。"""

    url: str
    db_name: str
    chat_message_collection: str
    fb_events_collection: str
    k_gaps_collection: str
    k_candidates_collection: str
    k_metrics_collection: str
    param_registry_collection: str
    # 章节级父块：只按 parent_id 批量取回、从不参与向量检索，故存 Mongo 而非 Milvus，
    # 同时避开 Milvus VARCHAR(65535) 字节上限，长章节可完整存储
    parent_chunks_collection: str


@dataclass
class ApiSettings:
    """两个 HTTP 服务的基础配置。"""

    import_app_name: str
    query_app_name: str
    app_env: str
    host: str
    import_port: int
    query_port: int
    cors_origins: tuple[str, ...]


@dataclass
class EvolutionSettings:
    """自进化闭环的全部开关、阈值与集合名。"""

    enabled: bool
    collection: str
    rrf_weight: float
    observe_window_days: int
    groundedness_min: float
    gray_enabled: bool
    scheduler_enabled: bool
    schedule_interval_minutes: int
    scan_batch: int
    gen_context_enabled: bool
    gap_weight_user: float
    gap_weight_retrieval: float
    gap_weight_generation: float
    gap_strong_threshold: float
    gap_weak_threshold: float
    attain_rate_min: float
    reject_rate_max: float
    alarm_adopt_rate_drop: float
    recall_limit: int
    # 闭环运行周期：指标快照/自调与回测的触发间隔（0 表示每轮都跑）
    metric_interval_minutes: int
    backtest_enabled: bool
    backtest_interval_hours: int
    backtest_min_hits: int


@dataclass
class RuntimeSettings:
    """进程内运行期参数（内存占用、缓存过期等）。"""

    task_state_ttl_seconds: int


@dataclass
class Settings:
    """聚合全部子配置，业务模块通过 ``settings.<domain>`` 访问。"""

    llm: LLMSettings
    embedding: EmbeddingSettings
    reranker: RerankerSettings
    milvus: MilvusSettings
    minio: MinioSettings
    mineru: MineruSettings
    mcp: McpSettings
    mongo: MongoSettings
    api: ApiSettings
    evolution: EvolutionSettings
    runtime: RuntimeSettings


def _build_settings() -> Settings:
    """集中构造配置；所有环境变量解析都在此完成。"""
    return Settings(
        llm=LLMSettings(
            base_url=env_str("OPENAI_BASE_URL"),
            api_key=env_str("OPENAI_API_KEY"),
            llm_model=env_str("LLM_DEFAULT_MODEL"),
            vl_model=env_str("VL_MODEL"),
            temperature=env_float("LLM_DEFAULT_TEMPERATURE"),
        ),
        embedding=EmbeddingSettings(
            model_path=env_str("BGE_M3_PATH"),
            model_name=env_str("BGE_M3"),
            device=env_str("BGE_DEVICE"),
            fp16=env_bool("BGE_FP16"),
        ),
        reranker=RerankerSettings(
            model_path=env_str("BGE_RERANKER_LARGE"),
            device=env_str("BGE_RERANKER_DEVICE"),
            fp16=env_bool("BGE_RERANKER_FP16"),
        ),
        milvus=MilvusSettings(
            url=env_str("MILVUS_URL"),
            chunks_collection=env_str("CHUNKS_COLLECTION"),
            item_name_collection=env_str("ITEM_NAME_COLLECTION"),
            evolution_collection=env_str("EVOLUTION_COLLECTION", "kb_evolution_items"),
        ),
        minio=MinioSettings(
            endpoint=env_str("MINIO_ENDPOINT"),
            access_key=env_str("MINIO_ACCESS_KEY"),
            secret_key=env_str("MINIO_SECRET_KEY"),
            bucket_name=env_str("MINIO_BUCKET_NAME"),
            image_dir=env_str("MINIO_IMG_DIR"),
            secure=env_bool("MINIO_SECURE"),
        ),
        mineru=MineruSettings(
            base_url=env_str("MINERU_BASE_URL"),
            api_key=env_str("MINERU_API_TOKEN"),
        ),
        mcp=McpSettings(
            base_url=env_str("MCP_DASHSCOPE_BASE_URL"),
            api_key=env_str("OPENAI_API_KEY"),
            timeout_seconds=env_int("MCP_TIMEOUT_SECONDS", 30),
        ),
        mongo=MongoSettings(
            url=env_str("MONGO_URL"),
            db_name=env_str("MONGO_DB_NAME"),
            chat_message_collection=env_str("MONGO_CHAT_COLLECTION", "chat_message"),
            fb_events_collection=env_str("EVOLUTION_FB_EVENTS_COLLECTION", "fb_events"),
            k_gaps_collection=env_str("EVOLUTION_K_GAPS_COLLECTION", "k_gaps"),
            k_candidates_collection=env_str("EVOLUTION_K_CANDIDATES_COLLECTION", "k_candidates"),
            k_metrics_collection=env_str("EVOLUTION_K_METRICS_COLLECTION", "k_metrics"),
            param_registry_collection=env_str("EVOLUTION_PARAM_REGISTRY_COLLECTION", "param_registry"),
            parent_chunks_collection=env_str("PARENT_CHUNKS_COLLECTION", "kb_parent_chunks"),
        ),
        api=ApiSettings(
            import_app_name=env_str("IMPORT_APP_NAME", "Enterprise RAG Import Service"),
            query_app_name=env_str("QUERY_APP_NAME", "Enterprise RAG Query Service"),
            app_env=env_str("APP_ENV", "dev"),
            host=env_str("APP_HOST", "0.0.0.0"),
            import_port=env_int("IMPORT_APP_PORT", 8000),
            query_port=env_int("QUERY_APP_PORT", 8001),
            cors_origins=tuple(
                item.strip() for item in env_str("CORS_ORIGINS", "*").split(",") if item.strip()
            ),
        ),
        evolution=EvolutionSettings(
            enabled=env_bool("EVOLUTION_ENABLED", False),
            collection=env_str("EVOLUTION_COLLECTION", "kb_evolution_items"),
            rrf_weight=env_float("EVOLUTION_RRF_WEIGHT", 1.0),
            observe_window_days=env_int("EVOLUTION_OBSERVE_WINDOW_DAYS", 7),
            groundedness_min=env_float("EVOLUTION_GROUNDEDNESS_MIN", 0.85),
            gray_enabled=env_bool("EVOLUTION_GRAY_ENABLED", False),
            scheduler_enabled=env_bool("EVOLUTION_SCHEDULE_ENABLED", True),
            schedule_interval_minutes=env_int("EVOLUTION_SCHEDULE_INTERVAL_MINUTES", 30),
            scan_batch=env_int("EVOLUTION_SCAN_BATCH", 50),
            gen_context_enabled=env_bool("EVOLUTION_GEN_CONTEXT_ENABLED", True),
            gap_weight_user=env_float("EVOLUTION_GAP_WEIGHT_USER", 0.4),
            gap_weight_retrieval=env_float("EVOLUTION_GAP_WEIGHT_RETRIEVAL", 0.3),
            gap_weight_generation=env_float("EVOLUTION_GAP_WEIGHT_GENERATION", 0.3),
            gap_strong_threshold=env_float("EVOLUTION_GAP_STRONG_THRESHOLD", 0.65),
            gap_weak_threshold=env_float("EVOLUTION_GAP_WEAK_THRESHOLD", 0.45),
            attain_rate_min=env_float("EVOLUTION_ATTAIN_RATE_MIN", 0.60),
            reject_rate_max=env_float("EVOLUTION_REJECT_RATE_MAX", 0.30),
            alarm_adopt_rate_drop=env_float("EVOLUTION_ALARM_ADOPT_DROP", 0.10),
            recall_limit=env_int("EVOLUTION_RECALL_LIMIT", 10),
            metric_interval_minutes=env_int("EVOLUTION_METRIC_INTERVAL_MINUTES", 60),
            backtest_enabled=env_bool("EVOLUTION_BACKTEST_ENABLED", True),
            backtest_interval_hours=env_int("EVOLUTION_BACKTEST_INTERVAL_HOURS", 24),
            # 回测下架门槛：命中次数不足时只观察，避免单条差评误杀人工审批过的知识
            backtest_min_hits=env_int("EVOLUTION_BACKTEST_MIN_HITS", 3),
        ),
        runtime=RuntimeSettings(
            task_state_ttl_seconds=env_int("TASK_STATE_TTL_SECONDS", 6 * 3600),
        ),
    )


settings = _build_settings()
