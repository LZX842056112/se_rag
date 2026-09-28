"""导入链路参数：MinerU 交互与文本切分策略。"""
from __future__ import annotations

# MinerU 模型版本（pipeline / vlm 等）
MINERU_MODEL_VERSION = "vlm"
# MinerU 任务轮询最长等待时间（秒），超过判定失败
MINERU_POLL_TIMEOUT_SECONDS = 600
# MinerU 任务轮询间隔（秒）
MINERU_POLL_INTERVAL_SECONDS = 3
# 解析结果 zip 下载超时（秒）
MINERU_DOWNLOAD_TIMEOUT_SECONDS = 60

# local_dir 缺省值所对应的输出目录名
PDF_PARSE_SERVICE_LOCAL_DIR = "output"

# ==================== 文本切分策略 ====================
# 单块最大长度：超过则递归细切，防止过长导致向量语义失真
CHUNK_MAX_SIZE = 1000
# 单块目标长度
CHUNK_SIZE = 600
# 相邻块重叠长度，保证语义不被切断
CHUNK_OVERLAP = 50
# 短碎片阈值：低于该长度尝试与同标题邻块合并
CHUNK_MIN = 400
# 主体名识别时截取前 N 个切片作为上下文
CHUNKS_SPLIT_TOP_NUMBER = 10
# 向量化批次大小：每批处理 N 条切片，避免显存溢出
EMBEDDING_BATCH_SIZE = 6
