"""RAG 离线评测：HTTP 调用查询服务，计算检索与 RAGAS 生成指标。

快速开始：
    python -m app.rag_eval.cli run --dataset app/rag_eval/datasets/sample_cases.json

设计约束：
- 只通过 HTTP 访问查询服务，不直接连 Milvus / Mongo；
- RAGAS 是可选依赖，未安装时可用 ``--no-ragas`` 只跑检索指标；
- judge 模型必须由 ``EVAL_LLM_MODEL`` 明确配置，避免回答模型自评。
"""

from app.rag_eval.config import EvalConfig
from app.rag_eval.dataset import EvalCase, load_cases
from app.rag_eval.runner import run_evaluation, timed_run

__all__ = ["EvalConfig", "EvalCase", "load_cases", "run_evaluation", "timed_run"]
