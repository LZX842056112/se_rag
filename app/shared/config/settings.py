"""
全局配置聚合模块（全项目唯一配置出口）。

设计约定：
1. 只有 ``common`` + 本模块读取环境变量，业务模块统一使用 ``settings``；
2. 每个领域一个子配置 dataclass，避免同一环境变量在多处重复解析
   （历史问题：``EVOLUTION_COLLECTION`` 曾被 Milvus 配置与自进化配置各读一次）；
3. 字段命名统一 snake_case，解析集中在本文件，便于比对与审查。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.shared.config.common import env_bool, env_float, env_int, env_str


@dataclass
class LLMSettings:
    """对话 / 视觉模型（OpenAI 兼容协议）配置。"""

    base_url: str  # OpenAI 兼容服务地址（OPENAI_BASE_URL）
    api_key: str  # API 密钥（OPENAI_API_KEY）
    llm_model: str  # 默认对话模型名（LLM_DEFAULT_MODEL）
    vl_model: str  # 视觉多模态模型名（VL_MODEL），用于图片理解
    temperature: float  # 采样温度，越大输出越随机（LLM_DEFAULT_TEMPERATURE）


@dataclass
class EmbeddingSettings:
    """BGE-M3 稠密+稀疏向量模型配置。"""

    model_path: str  # 模型权重目录（BGE_M3_PATH）
    model_name: str  # 模型标识（BGE_M3）
    device: str  # 推理设备，如 cpu/cuda（BGE_DEVICE）
    fp16: bool  # 是否用 fp16 半精度以省显存/加速（BGE_FP16）

@dataclass
class EvaluationSettings:
    """离线答案评测。模型名必须明确配置；接口和密钥可复用回答服务。"""

    model: str  # 评测所用模型名（EVAL_LLM_MODEL）
    base_url: str  # 评测模型服务地址（EVAL_LLM_BASE_URL）
    api_key: str  # 评测 API 密钥（EVAL_LLM_API_KEY）
    timeout_seconds: float = 120  # 单次评测超时（秒），默认 120

@dataclass
class RerankerSettings:
    """BGE-Reranker 重排模型配置。"""

    model_path: str  # 权重目录（BGE_RERANKER_LARGE）
    device: str  # 推理设备（BGE_RERANKER_DEVICE）
    fp16: bool  # 是否 fp16 半精度（BGE_RERANKER_FP16）

@dataclass
class MilvusSettings:
    """Milvus 连接与集合名（集合名属于数据契约，改名需同步迁移存量数据）。"""

    url: str  # Milvus 连接地址（MILVUS_URL）
    chunks_collection: str  # 向量检索主集合（CHUNKS_COLLECTION）
    item_name_collection: str  # 知识条目标题/名称向量集合（ITEM_NAME_COLLECTION）
    evolution_collection: str  # 自进化候选条目集合（EVOLUTION_COLLECTION，默认 kb_evolution_items）
    # 连接与单次 RPC 超时（秒）。pymilvus 的构造参数只覆盖建连等待，
    # hybrid_search/query 等业务 RPC 必须各自传 timeout，否则网络挂起时永久阻塞。
    timeout_seconds: int


@dataclass
class MinioSettings:
    """MinIO 对象存储配置。"""

    endpoint: str  # MinIO 访问地址（MINIO_ENDPOINT）
    access_key: str  # Access Key（MINIO_ACCESS_KEY）
    secret_key: str  # Secret Key（MINIO_SECRET_KEY）
    bucket_name: str  # 桶名（MINIO_BUCKET_NAME）
    image_dir: str  # 图片存放目录前缀（MINIO_IMG_DIR）
    secure: bool  # 是否启用 HTTPS（MINIO_SECURE）


@dataclass
class MineruSettings:
    """MinerU（PDF → Markdown）服务配置。"""

    base_url: str  # MinerU 服务地址（MINERU_BASE_URL）
    api_key: str  # MinerU API 令牌（MINERU_API_TOKEN）


@dataclass
class McpSettings:
    """百炼联网搜索 MCP 配置。"""

    base_url: str  # MCP 服务地址（MCP_DASHSCOPE_BASE_URL）
    api_key: str  # 复用 OPENAI_API_KEY
    timeout_seconds: int  # 调用超时（秒），默认 30（MCP_TIMEOUT_SECONDS）


@dataclass
class MongoSettings:
    """MongoDB 连接与集合名（集合名属于数据契约）。"""

    url: str  # MongoDB 连接串（MONGO_URL）
    db_name: str  # 数据库名（MONGO_DB_NAME）
    chat_message_collection: str  # 聊天消息集合（MONGO_CHAT_COLLECTION）
    fb_events_collection: str  # 反馈事件集合（EVOLUTION_FB_EVENTS_COLLECTION）
    k_gaps_collection: str  # 知识缺口集合（EVOLUTION_K_GAPS_COLLECTION）
    k_candidates_collection: str  # 知识候选集合（EVOLUTION_K_CANDIDATES_COLLECTION）
    k_metrics_collection: str  # 知识指标集合（EVOLUTION_K_METRICS_COLLECTION）
    param_registry_collection: str  # 自进化参数注册表（EVOLUTION_PARAM_REGISTRY_COLLECTION）
    # 章节级父块：只按 parent_id 批量取回、从不参与向量检索，故存 Mongo 而非 Milvus，
    # 同时避开 Milvus VARCHAR(65535) 字节上限，长章节可完整存储
    parent_chunks_collection: str
    query_sessions_collection: str = "query_sessions"  # 查询会话集合
    query_runs_collection: str = "query_runs"  # 查询执行记录集合
    query_feedback_collection: str = "query_feedback"  # 查询反馈集合
    query_issues_collection: str = "query_issues"  # 查询问题集合


@dataclass
class ApiSettings:
    """两个 HTTP 服务的基础配置。"""

    import_app_name: str  # 导入服务名称（IMPORT_APP_NAME）
    query_app_name: str  # 问答服务名称（QUERY_APP_NAME）
    app_env: str  # 运行环境 dev/prod（APP_ENV）
    host: str  # 监听地址（APP_HOST）
    import_port: int  # 导入服务端口（IMPORT_APP_PORT，默认 8000）
    query_port: int  # 问答服务端口（QUERY_APP_PORT，默认 8001）
    cors_origins: tuple[str, ...]  # 跨域白名单，逗号分隔（CORS_ORIGINS，默认 *）
    session_ownership_enabled: bool = True  # 是否启用会话属主校验（SESSION_OWNERSHIP_ENABLED）


@dataclass
class EvolutionSettings:
    """自进化闭环的全部开关、阈值与集合名。"""

    enabled: bool  # 总开关（EVOLUTION_ENABLED）
    collection: str  # 自进化条目集合（EVOLUTION_COLLECTION）
    rrf_weight: float  # RRF 融合权重（EVOLUTION_RRF_WEIGHT）
    observe_window_days: int  # 观测窗口天数（EVOLUTION_OBSERVE_WINDOW_DAYS）
    groundedness_min: float  # 可信度下限（EVOLUTION_GROUNDEDNESS_MIN）
    gray_enabled: bool  # 灰度发布开关（EVOLUTION_GRAY_ENABLED）
    scheduler_enabled: bool  # 定时调度开关（EVOLUTION_SCHEDULE_ENABLED）
    schedule_interval_minutes: int  # 调度周期（分钟，EVOLUTION_SCHEDULE_INTERVAL_MINUTES）
    scan_batch: int  # 每批扫描数（EVOLUTION_SCAN_BATCH）
    gen_context_enabled: bool  # 是否生成上下文（EVOLUTION_GEN_CONTEXT_ENABLED）
    gap_weight_user: float  # 用户反馈缺口权重（EVOLUTION_GAP_WEIGHT_USER）
    gap_weight_retrieval: float  # 检索缺口权重（EVOLUTION_GAP_WEIGHT_RETRIEVAL）
    gap_weight_generation: float  # 生成缺口权重（EVOLUTION_GAP_WEIGHT_GENERATION）
    gap_strong_threshold: float  # 强缺口阈值（EVOLUTION_GAP_STRONG_THRESHOLD）
    gap_weak_threshold: float  # 弱缺口阈值（EVOLUTION_GAP_WEAK_THRESHOLD）
    attain_rate_min: float  # 达成率下限（EVOLUTION_ATTAIN_RATE_MIN）
    reject_rate_max: float  # 拒绝率上限（EVOLUTION_REJECT_RATE_MAX）
    alarm_adopt_rate_drop: float  # 采纳率下跌告警阈值（EVOLUTION_ALARM_ADOPT_DROP）
    recall_limit: int  # 召回条数限制（EVOLUTION_RECALL_LIMIT）
    # 闭环运行周期：指标快照/自调与回测的触发间隔（0 表示每轮都跑）
    metric_interval_minutes: int  # 指标快照/自调触发间隔（分钟，0 表示每轮都跑）
    backtest_enabled: bool  # 回测开关（EVOLUTION_BACKTEST_ENABLED）
    backtest_interval_hours: int  # 回测周期（小时，EVOLUTION_BACKTEST_INTERVAL_HOURS）
    backtest_min_hits: int  # 回测下架门槛（EVOLUTION_BACKTEST_MIN_HITS）
    auto_tune_enabled: bool = False  # 参数自动调优开关（EVOLUTION_AUTO_TUNE_ENABLED）
    recall_enabled: bool | None = None  # 已审批条目召回开关，None 表示跟随 enabled（EVOLUTION_RECALL_ENABLED）
    write_enabled: bool | None = None  # 知识写入开关，None 表示跟随 enabled（EVOLUTION_WRITE_ENABLED）


@dataclass
class AgenticSettings:
    """Agentic RAG 检索编排的运行时参数。"""

    mode: str = 'legacy'  # 检索模式：legacy / agentic（RAG_MODE）
    policy_version: str = 'agentic-v1'  # Agentic 策略版本（RAG_POLICY_VERSION）
    gray_percent: int = 0  # 灰度放量比例 0-100（RAG_GRAY_PERCENT）
    decision_model: str = ''  # 决策/路由所用模型名，空则复用对话模型（AGENT_LLM_MODEL）
    max_tool_calls: int = 4  # 单轮最大工具调用次数（AGENT_MAX_TOOL_CALLS）
    max_web_calls: int = 1  # 联网检索工具最大调用数（AGENT_MAX_WEB_CALLS）
    max_repairs: int = 1  # 检索失败后最大修复重试轮数（0/1，AGENT_MAX_REPAIRS）
    max_context_tokens: int = 8000  # 上下文窗口上限（token，AGENT_MAX_CONTEXT_TOKENS）
    timeout_seconds: float = 60  # 单次 Agentic 执行超时（秒，AGENT_TIMEOUT_SECONDS）
    keyword_scan_limit: int = 500  # 关键词扫描命中条数上限（AGENT_KEYWORD_SCAN_LIMIT）
    neighbor_window: int = 1  # 邻近切片扩窗数（AGENT_NEIGHBOR_WINDOW）
    tokenizer_path: str = ''  # 分词器路径，用于 token 预算裁剪（AGENT_TOKENIZER_PATH）


@dataclass
class RuntimeSettings:
    """进程内运行期参数（内存占用、缓存过期等）。"""

    task_state_ttl_seconds: int  # 任务状态缓存过期时间（秒，TASK_STATE_TTL_SECONDS）
    model_prices_json: str = "{}"  # 模型单价 JSON（LLM_MODEL_PRICES_JSON）
    intent_timeout_seconds: float = 2.0  # 意图识别超时（秒，INTENT_TIMEOUT_SECONDS）
    default_issue_owner: str = ""  # 问题默认负责人（DEFAULT_ISSUE_OWNER）


@dataclass
class ImportSettings:
    """导入链路的鉴权、限额、重试与写入参数。

    属性名用 ``import_``（``import`` 是 Python 关键字），业务侧访问 ``settings.import_``。
    """

    # 上传/状态查询鉴权（请求头 X-Internal-Token）；为空时 fail-closed（503）
    admin_token: str
    # 单文件上传大小上限（MB）
    max_upload_mb: int
    # Milvus 单次写入分片大小（条）
    milvus_insert_batch_size: int
    # 图片视觉摘要并发上限
    image_summary_max_workers: int
    # 对话/视觉模型单次调用超时（秒）与 SDK 内建重试次数
    llm_timeout_seconds: int
    llm_max_retries: int
    # 导入任务的有界重试次数与退避基准（秒）
    max_retry_attempts: int
    retry_backoff_seconds: float
    # 进程内同时执行的导入任务上限，以及等待名额的最长时间（秒）
    max_concurrent_tasks: int
    acquire_timeout_seconds: int
    # 导入完成后的跨存储对账（只记日志，不阻断）
    reconcile_enabled: bool
    # 复用既有 MinerU 解析产物（断点续传的轻量实现）
    checkpoint_enabled: bool


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
    import_: ImportSettings
    evolution: EvolutionSettings
    runtime: RuntimeSettings
    evaluation: EvaluationSettings
    agentic: AgenticSettings = field(default_factory=AgenticSettings)


def _build_settings() -> Settings:
    """集中构造配置；所有环境变量解析都在此完成。"""
    return Settings(
        evaluation=EvaluationSettings(
            model=env_str("EVAL_LLM_MODEL"),  # 评测模型名
            base_url=env_str("EVAL_LLM_BASE_URL"),  # 评测模型服务地址
            api_key=env_str("EVAL_LLM_API_KEY"),  # 评测 API 密钥
            timeout_seconds=env_float("EVAL_LLM_TIMEOUT_SECONDS", 120),  # 评测超时（秒），默认 120
        ),
        llm=LLMSettings(
            base_url=env_str("OPENAI_BASE_URL"),  # OpenAI 兼容服务地址
            api_key=env_str("OPENAI_API_KEY"),  # API 密钥（也可复用为 MCP 密钥）
            llm_model=env_str("LLM_DEFAULT_MODEL"),  # 默认对话模型名
            vl_model=env_str("VL_MODEL"),  # 视觉多模态模型名
            temperature=env_float("LLM_DEFAULT_TEMPERATURE"),  # 采样温度
        ),
        embedding=EmbeddingSettings(
            model_path=env_str("BGE_M3_PATH"),  # BGE-M3 权重目录
            model_name=env_str("BGE_M3"),  # 模型标识
            device=env_str("BGE_DEVICE"),  # 推理设备 cpu/cuda
            fp16=env_bool("BGE_FP16"),  # 是否 fp16 半精度
        ),
        reranker=RerankerSettings(
            model_path=env_str("BGE_RERANKER_LARGE"),  # 重排模型权重目录
            device=env_str("BGE_RERANKER_DEVICE"),  # 推理设备
            fp16=env_bool("BGE_RERANKER_FP16"),  # 是否 fp16 半精度
        ),
        milvus=MilvusSettings(
            url=env_str("MILVUS_URL"),  # Milvus 连接地址
            chunks_collection=env_str("CHUNKS_COLLECTION"),  # 向量主集合
            item_name_collection=env_str("ITEM_NAME_COLLECTION"),  # 条目名称向量集合
            evolution_collection=env_str("EVOLUTION_COLLECTION", "kb_evolution_items"),  # 自进化集合，默认 kb_evolution_items
            timeout_seconds=env_int("MILVUS_TIMEOUT_SECONDS", 15),  # 连接/超时（秒），默认 15
        ),
        minio=MinioSettings(
            endpoint=env_str("MINIO_ENDPOINT"),  # MinIO 访问地址
            access_key=env_str("MINIO_ACCESS_KEY"),  # Access Key
            secret_key=env_str("MINIO_SECRET_KEY"),  # Secret Key
            bucket_name=env_str("MINIO_BUCKET_NAME"),  # 桶名
            image_dir=env_str("MINIO_IMG_DIR"),  # 图片目录前缀
            secure=env_bool("MINIO_SECURE"),  # 是否 HTTPS
        ),
        mineru=MineruSettings(
            base_url=env_str("MINERU_BASE_URL"),  # MinerU 服务地址
            api_key=env_str("MINERU_API_TOKEN"),  # MinerU 令牌
        ),
        mcp=McpSettings(
            base_url=env_str("MCP_DASHSCOPE_BASE_URL"),  # 百炼 MCP 服务地址
            api_key=env_str("OPENAI_API_KEY"),  # 复用对话 API 密钥
            timeout_seconds=env_int("MCP_TIMEOUT_SECONDS", 30),  # 调用超时（秒），默认 30
        ),
        mongo=MongoSettings(
            url=env_str("MONGO_URL"),  # MongoDB 连接串
            db_name=env_str("MONGO_DB_NAME"),  # 数据库名
            chat_message_collection=env_str("MONGO_CHAT_COLLECTION", "chat_message"),  # 聊天消息集合，默认 chat_message
            fb_events_collection=env_str("EVOLUTION_FB_EVENTS_COLLECTION", "fb_events"),  # 反馈事件集合，默认 fb_events
            k_gaps_collection=env_str("EVOLUTION_K_GAPS_COLLECTION", "k_gaps"),  # 知识缺口集合，默认 k_gaps
            k_candidates_collection=env_str("EVOLUTION_K_CANDIDATES_COLLECTION", "k_candidates"),  # 知识候选集合，默认 k_candidates
            k_metrics_collection=env_str("EVOLUTION_K_METRICS_COLLECTION", "k_metrics"),  # 知识指标集合，默认 k_metrics
            param_registry_collection=env_str("EVOLUTION_PARAM_REGISTRY_COLLECTION", "param_registry"),  # 参数注册表，默认 param_registry
            parent_chunks_collection=env_str("PARENT_CHUNKS_COLLECTION", "kb_parent_chunks"),  # 章节父块集合，默认 kb_parent_chunks
        ),
        api=ApiSettings(
            session_ownership_enabled=env_bool("SESSION_OWNERSHIP_ENABLED", True),  # 会话属主校验，默认开启
            import_app_name=env_str("IMPORT_APP_NAME", "Enterprise RAG Import Service"),  # 导入服务名，默认 "Enterprise RAG Import Service"
            query_app_name=env_str("QUERY_APP_NAME", "Enterprise RAG Query Service"),  # 问答服务名，默认 "Enterprise RAG Query Service"
            app_env=env_str("APP_ENV", "dev"),  # 运行环境，默认 dev
            host=env_str("APP_HOST", "0.0.0.0"),  # 监听地址，默认 0.0.0.0
            import_port=env_int("IMPORT_APP_PORT", 8000),  # 导入端口，默认 8000
            query_port=env_int("QUERY_APP_PORT", 8001),  # 问答端口，默认 8001
            cors_origins=tuple(
                item.strip() for item in env_str("CORS_ORIGINS", "*").split(",") if item.strip()
            ),  # 跨域白名单，逗号分隔，默认 *
        ),
        import_=ImportSettings(
            admin_token=env_str("IMPORT_ADMIN_TOKEN", "evoadmin_15946edbb1ed4289ae8296dbaafa57b5aad9bf25bd72a51c"),  # 导入鉴权令牌（空则 fail-closed）
            max_upload_mb=env_int("IMPORT_MAX_UPLOAD_MB", 50),  # 单文件上传上限（MB），默认 50
            milvus_insert_batch_size=env_int("MILVUS_INSERT_BATCH_SIZE", 500),  # Milvus 单次写入条数，默认 500
            image_summary_max_workers=env_int("IMAGE_SUMMARY_MAX_WORKERS", 4),  # 图片摘要并发数，默认 4
            llm_timeout_seconds=env_int("LLM_TIMEOUT_SECONDS", 60),  # 模型调用超时（秒），默认 60
            llm_max_retries=env_int("LLM_MAX_RETRIES", 2),  # SDK 内建重试次数，默认 2
            max_retry_attempts=env_int("IMPORT_MAX_RETRY_ATTEMPTS", 3),  # 导入任务重试次数，默认 3
            retry_backoff_seconds=env_float("IMPORT_RETRY_BACKOFF_SECONDS", 1.0),  # 退避基准（秒），默认 1.0
            max_concurrent_tasks=env_int("IMPORT_MAX_CONCURRENT_TASKS", 2),  # 并发导入任务数，默认 2
            acquire_timeout_seconds=env_int("IMPORT_ACQUIRE_TIMEOUT_SECONDS", 600),  # 等待名额最长时间（秒），默认 600
            reconcile_enabled=env_bool("IMPORT_RECONCILE_ENABLED", True),  # 跨存储对账，默认开启
            checkpoint_enabled=env_bool("IMPORT_CHECKPOINT_ENABLED", True),  # 断点续传，默认开启
        ),
        evolution=EvolutionSettings(
            recall_enabled=env_bool('EVOLUTION_RECALL_ENABLED') if env_str('EVOLUTION_RECALL_ENABLED') else None,  # 召回开关，未设置则跟随 enabled
            write_enabled=env_bool('EVOLUTION_WRITE_ENABLED') if env_str('EVOLUTION_WRITE_ENABLED') else None,  # 写入开关，未设置则跟随 enabled
            enabled=env_bool("EVOLUTION_ENABLED", False),  # 自进化总开关，默认关闭
            auto_tune_enabled=env_bool("EVOLUTION_AUTO_TUNE_ENABLED", False),  # 参数自动调优，默认关闭
            collection=env_str("EVOLUTION_COLLECTION", "kb_evolution_items"),  # 自进化集合，默认 kb_evolution_items
            rrf_weight=env_float("EVOLUTION_RRF_WEIGHT", 1.0),  # RRF 融合权重，默认 1.0
            observe_window_days=env_int("EVOLUTION_OBSERVE_WINDOW_DAYS", 7),  # 观测窗口天数，默认 7
            groundedness_min=env_float("EVOLUTION_GROUNDEDNESS_MIN", 0.85),  # 可信度下限，默认 0.85
            gray_enabled=env_bool("EVOLUTION_GRAY_ENABLED", False),  # 灰度开关，默认关闭
            scheduler_enabled=env_bool("EVOLUTION_SCHEDULE_ENABLED", True),  # 定时调度，默认开启
            schedule_interval_minutes=env_int("EVOLUTION_SCHEDULE_INTERVAL_MINUTES", 30),  # 调度周期（分钟），默认 30
            scan_batch=env_int("EVOLUTION_SCAN_BATCH", 50),  # 每批扫描数，默认 50
            gen_context_enabled=env_bool("EVOLUTION_GEN_CONTEXT_ENABLED", True),  # 生成上下文，默认开启
            gap_weight_user=env_float("EVOLUTION_GAP_WEIGHT_USER", 0.4),  # 用户反馈缺口权重，默认 0.4
            gap_weight_retrieval=env_float("EVOLUTION_GAP_WEIGHT_RETRIEVAL", 0.3),  # 检索缺口权重，默认 0.3
            gap_weight_generation=env_float("EVOLUTION_GAP_WEIGHT_GENERATION", 0.3),  # 生成缺口权重，默认 0.3
            gap_strong_threshold=env_float("EVOLUTION_GAP_STRONG_THRESHOLD", 0.65),  # 强缺口阈值，默认 0.65
            gap_weak_threshold=env_float("EVOLUTION_GAP_WEAK_THRESHOLD", 0.45),  # 弱缺口阈值，默认 0.45
            attain_rate_min=env_float("EVOLUTION_ATTAIN_RATE_MIN", 0.60),  # 达成率下限，默认 0.60
            reject_rate_max=env_float("EVOLUTION_REJECT_RATE_MAX", 0.30),  # 拒绝率上限，默认 0.30
            alarm_adopt_rate_drop=env_float("EVOLUTION_ALARM_ADOPT_DROP", 0.10),  # 采纳率下跌告警阈值，默认 0.10
            recall_limit=env_int("EVOLUTION_RECALL_LIMIT", 10),  # 召回条数上限，默认 10
            metric_interval_minutes=env_int("EVOLUTION_METRIC_INTERVAL_MINUTES", 60),  # 指标/自调间隔（分钟），默认 60
            backtest_enabled=env_bool("EVOLUTION_BACKTEST_ENABLED", True),  # 回测开关，默认开启
            backtest_interval_hours=env_int("EVOLUTION_BACKTEST_INTERVAL_HOURS", 24),  # 回测周期（小时），默认 24
            # 回测下架门槛：命中次数不足时只观察，避免单条差评误杀人工审批过的知识
            backtest_min_hits=env_int("EVOLUTION_BACKTEST_MIN_HITS", 3),  # 回测最低命中数，默认 3
        ),
        runtime=RuntimeSettings(
            task_state_ttl_seconds=env_int("TASK_STATE_TTL_SECONDS", 6 * 3600),  # 任务状态缓存 TTL（秒），默认 6 小时
            model_prices_json=env_str("LLM_MODEL_PRICES_JSON", "{}"),  # 模型单价 JSON，默认空
            intent_timeout_seconds=max(0.05, min(2.0, env_float("INTENT_TIMEOUT_SECONDS", 2.0))),  # 意图识别超时（秒），默认 2.0
            default_issue_owner=env_str("DEFAULT_ISSUE_OWNER", ""),  # 问题默认负责人，默认空
        ),
        agentic=AgenticSettings(
            mode=env_str('RAG_MODE', 'legacy'),  # 检索模式，默认 legacy
            policy_version=env_str('RAG_POLICY_VERSION', 'agentic-v1'),  # 策略版本，默认 agentic-v1
            gray_percent=max(0, min(100, env_int('RAG_GRAY_PERCENT', 0))),  # 灰度放量比例 0-100，默认 0
            decision_model=env_str('AGENT_LLM_MODEL', ''),  # 决策模型名，默认空（复用对话模型）
            max_tool_calls=max(1, env_int('AGENT_MAX_TOOL_CALLS', 4)),  # 最大工具调用次数，默认 4
            max_web_calls=max(0, env_int('AGENT_MAX_WEB_CALLS', 1)),  # 联网检索最大调用数，默认 1
            max_repairs=max(0, min(1, env_int('AGENT_MAX_REPAIRS', 1))),  # 修复重试轮数，默认 1
            max_context_tokens=max(1, env_int('AGENT_MAX_CONTEXT_TOKENS', 8000)),  # 上下文上限 token，默认 8000
            timeout_seconds=max(.05, env_float('AGENT_TIMEOUT_SECONDS', 60)),  # 执行超时（秒），默认 60
            keyword_scan_limit=max(1, env_int('AGENT_KEYWORD_SCAN_LIMIT', 500)),  # 关键词扫描上限，默认 500
            neighbor_window=max(0, min(2, env_int('AGENT_NEIGHBOR_WINDOW', 1))),  # 邻近扩窗数，默认 1
            tokenizer_path=env_str('AGENT_TOKENIZER_PATH', ''),  # 分词器路径，默认空
        ),
    )


settings = _build_settings()