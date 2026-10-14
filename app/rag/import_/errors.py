"""导入任务的错误分类：把异常映射成稳定的机器码。

背景：历史实现把所有异常统一归为 ``failed``，前端与排障无法区分「文件损坏」
「服务不可用」「模型超时」。此处按异常类型与消息关键字做一次归类。

约定：错误码沿用项目既有的 snake_case 风格；消息保持原始异常文本（含中文上下文）。
"""
from __future__ import annotations

# 可重试的错误码：网络/服务侧的瞬时故障。
# 确定性错误（切分为空、文档类型不支持、对账不符）刻意不列入，避免无意义重跑。
RETRYABLE_CODES: frozenset[str] = frozenset({
    "mineru_timeout",
    "mineru_failed",
    "storage_unavailable",
    "internal_error",
})

# 关键字规则：按顺序匹配，**先具体后笼统**（例如 mineru_timeout 必须早于 mineru_failed）
_KEYWORD_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("unsupported_type", ("不支持的文档类型", "仅支持 md / pdf", "类型无法解析")),
    ("import_busy", ("导入并发已满", "等待导入名额超时")),
    ("mineru_timeout", ("mineru 解析超时", "mineru 任务轮询", "mineru 申请解析")),
    ("parse_failed", ("没有 markdown 文件", "解析结果解压后")),
    ("split_empty", ("切分后为空", "chunks为空", "chunks 为空")),
    ("index_count_mismatch", ("对账失败",)),
    ("storage_unavailable", ("不可用", "未配置")),
    ("embedding_failed", ("向量生成", "向量化", "texts")),
    ("item_name_failed", ("主体名", "主体识别")),
    ("mineru_failed", ("mineru",)),
    ("index_failed", ("写入", "insert", "upsert", "milvus", "mongo", "minio")),
)


def classify_error(exc: BaseException) -> tuple[str, str]:
    """把异常映射为 ``(错误码, 中文提示)``。

    无法归类时返回 ``("internal_error", 原始消息)``。
    """
    message = str(exc) or exc.__class__.__name__

    # 超时优先按类型判定：requests/内建超时都是 OSError 子类，靠消息无法可靠区分
    if isinstance(exc, TimeoutError):
        return "mineru_timeout", message

    lowered = message.lower()
    for code, keywords in _KEYWORD_RULES:
        for keyword in keywords:
            if keyword.lower() in lowered:
                return code, message
    return "internal_error", message


def is_retryable(code: str) -> bool:
    """该错误码是否值得重试。"""
    return code in RETRYABLE_CODES
