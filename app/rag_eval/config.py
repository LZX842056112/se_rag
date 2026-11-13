"""评测运行参数。

评测通过 HTTP 调用查询服务，不直接依赖 Milvus/Mongo；judge 模型复用
``settings.evaluation``，与回答模型解耦，避免自评自答。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# 默认检索指标截断位置
DEFAULT_KS: tuple[int, ...] = (3, 5, 10)


@dataclass
class EvalConfig:
    """一次评测运行的完整参数。"""

    dataset_path: Path
    output_dir: Path
    base_url: str = "http://127.0.0.1:8001"
    ks: tuple[int, ...] = DEFAULT_KS
    concurrency: int = 2  # 并发请求数：过高会压垮本地模型
    timeout_seconds: float = 180.0  # 单题超时（秒）
    retries: int = 1  # 网络/5xx 重试次数
    run_ragas: bool = True  # 是否执行 RAGAS 生成指标
    max_drop: float = 0.02  # 回归阈值：指标下降超过该值视为回归
    tags: tuple[str, ...] = field(default_factory=tuple)  # 只跑指定标签（空表示全部）

    def selected(self, tags: list[str] | None) -> bool:
        """按标签过滤；未配置标签时全部选中。"""
        if not self.tags:
            return True
        return bool(set(self.tags) & set(tags or []))
