"""
基础设施配置聚合模块。

各类配置（Milvus/向量、LLM 模型、MCP、解析、存储）分散在 app.shared.config
下的单例模块中。此处通过 InfraConfig 聚合对象统一暴露，业务模块只需导入
infra_config 即可访问全部配置。
"""
from dataclasses import dataclass, field

from app.shared.config.bailian_mcp_config import mcp_config, McpConfig
from app.shared.config.embedding_config import embedding_config, EmbeddingConfig
from app.shared.config.lm_config import lm_config, LLMConfig
from app.shared.config.milvus_config import milvus_config, MilvusConfig
from app.shared.config.mineru_config import mineru_config, MinerUConfig
from app.shared.config.minio_config import minio_config, MinIOConfig
from app.shared.config.reranker_config import reranker_config, RerankerConfig
from app.shared.config.settings_config import settings, AppSettings


@dataclass
class InfraConfig:
    """聚合各配置单例，业务层通过 infra_config.xxx 统一访问。"""

    embedding_config: EmbeddingConfig = field(default_factory=lambda: embedding_config)
    lm_config: LLMConfig = field(default_factory=lambda: lm_config)
    mcp_config: McpConfig = field(default_factory=lambda: mcp_config)
    milvus_config: MilvusConfig = field(default_factory=lambda: milvus_config)
    mineru_config: MinerUConfig = field(default_factory=lambda: mineru_config)
    minio_config: MinIOConfig = field(default_factory=lambda: minio_config)
    reranker_config: RerankerConfig = field(default_factory=lambda: reranker_config)
    settings: AppSettings = field(default_factory=lambda: settings)


infra_config = InfraConfig()