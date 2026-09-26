"""
自进化配置模块：集中管理阈值 / 窗口 / 开关 / 集合名。
初值即改造方案 §10.1 的工程初值，编码不再另定参数；仅读，不负责写。
"""
from dataclasses import dataclass, field
from typing import Any

from app.shared.config.common import env_str, env_bool, env_float


@dataclass
class EvolutionConfig:
    # 全局开关（默认关闭，灰度平滑）
    enabled: bool = field(default_factory=lambda: env_bool("EVOLUTION_ENABLED", False))
    # Milvus 进化条目集合
    evolution_collection: str = field(
        default_factory=lambda: env_str("EVOLUTION_COLLECTION", "kb_evolution_items")
    )
    # 进化召回并入 RRF 的权重
    rrf_weight: float = field(default_factory=lambda: env_float("EVOLUTION_RRF_WEIGHT", 1.0))
    # 回测观察窗（天）
    observe_window_days: int = field(
        default_factory=lambda: int(env_str("EVOLUTION_OBSERVE_WINDOW_DAYS", "7"))
    )
    # 在线 groundedness 门禁（初值）
    groundedness_min: float = field(default_factory=lambda: env_float("EVOLUTION_GROUNDEDNESS_MIN", 0.85))
    # 灰度开关：候选条目是否以低召回权重参与观察
    gray_enabled: bool = field(default_factory=lambda: env_bool("EVOLUTION_GRAY_ENABLED", False))

    # ---- 自动调度 ----
    # 调度器开关（受 enabled 总开关双重约束）
    scheduler_enabled: bool = field(default_factory=lambda: env_bool("EVOLUTION_SCHEDULE_ENABLED", True))
    # 调度间隔（分钟）
    schedule_interval_minutes: int = field(
        default_factory=lambda: int(env_str("EVOLUTION_SCHEDULE_INTERVAL_MINUTES", "30"))
    )
    # 单轮扫描的未解决反馈批次
    scan_batch: int = field(default_factory=lambda: int(env_str("EVOLUTION_SCAN_BATCH", "50")))
    # 候选生成是否注入检索上下文（关闭则恒传空由 LLM 据问题提炼）
    gen_context_enabled: bool = field(default_factory=lambda: env_bool("EVOLUTION_GEN_CONTEXT_ENABLED", True))

    # ---- 缺口分级 ----
    gap_weight_user: float = field(default_factory=lambda: env_float("EVOLUTION_GAP_WEIGHT_USER", 0.4))
    gap_weight_retrieval: float = field(default_factory=lambda: env_float("EVOLUTION_GAP_WEIGHT_RETRIEVAL", 0.3))
    gap_weight_generation: float = field(default_factory=lambda: env_float("EVOLUTION_GAP_WEIGHT_GENERATION", 0.3))
    gap_strong_threshold: float = field(default_factory=lambda: env_float("EVOLUTION_GAP_STRONG_THRESHOLD", 0.65))
    gap_weak_threshold: float = field(default_factory=lambda: env_float("EVOLUTION_GAP_WEAK_THRESHOLD", 0.45))

    # ---- 回测止损 ----
    attain_rate_min: float = field(default_factory=lambda: env_float("EVOLUTION_ATTAIN_RATE_MIN", 0.60))
    reject_rate_max: float = field(default_factory=lambda: env_float("EVOLUTION_REJECT_RATE_MAX", 0.30))
    # 采纳率相对前一天骤降幅度触发告警
    alarm_adopt_rate_drop: float = field(default_factory=lambda: env_float("EVOLUTION_ALARM_ADOPT_DROP", 0.10))

    # ---- 进化召回 ----
    evolution_recall_limit: int = field(
        default_factory=lambda: int(env_str("EVOLUTION_RECALL_LIMIT", "10"))
    )

    # ---- MongoDB 集合 ----
    fb_events_collection: str = field(default_factory=lambda: env_str("EVOLUTION_FB_EVENTS_COLLECTION", "fb_events"))
    k_gaps_collection: str = field(default_factory=lambda: env_str("EVOLUTION_K_GAPS_COLLECTION", "k_gaps"))
    k_candidates_collection: str = field(default_factory=lambda: env_str("EVOLUTION_K_CANDIDATES_COLLECTION", "k_candidates"))
    k_metrics_collection: str = field(default_factory=lambda: env_str("EVOLUTION_K_METRICS_COLLECTION", "k_metrics"))
    param_registry_collection: str = field(default_factory=lambda: env_str("EVOLUTION_PARAM_REGISTRY_COLLECTION", "param_registry"))


evolution_config = EvolutionConfig()