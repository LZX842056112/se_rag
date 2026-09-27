"""
应用基础配置模块，负责读取导入服务与查询服务的启动配置。
"""
from dataclasses import dataclass

from app.shared.config.common import env_bool, env_str


@dataclass
class AppSettings:
    import_app_name: str = env_str("IMPORT_APP_NAME", "Enterprise RAG Import Service")
    query_app_name: str = env_str("QUERY_APP_NAME", "Enterprise RAG Query Service")
    app_env: str = env_str("APP_ENV", "dev")
    app_host: str = env_str("APP_HOST", "0.0.0.0")
    import_app_port: int = int(env_str("IMPORT_APP_PORT", "8000"))
    query_app_port: int = int(env_str("QUERY_APP_PORT", "8001"))
    cors_origins: tuple[str, ...] = tuple(
        item.strip() for item in env_str("CORS_ORIGINS", "*").split(",") if item.strip()
    )
    # 自进化旁路全局开关（默认关闭，灰度平滑）
    evolution_enabled: bool = env_bool("EVOLUTION_ENABLED", False)


settings = AppSettings()
