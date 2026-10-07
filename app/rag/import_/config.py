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
# 长度口径统一说明：以下三个阈值一律按**正文口径**比较，即不含切片前置标题的那部分文本。
# 历史实现用「含标题前缀」的长度判断短块合并，导致该步骤永远不可达（死代码）。
#
# CHUNK_SIZE 软目标：决定**多个**原子块（段落/表格/代码/图片）如何打包；
# CHUNK_MAX_SIZE 硬上限：只有**单个**原子块超过它才从中间切开，合并结果也不得超过它。
# 单个原子块落在 [CHUNK_SIZE, CHUNK_MAX_SIZE] 之间时不切——它本身已是语义完整的单元，
# 按字符切开只会制造纯重叠的碎片尾巴。
CHUNK_MAX_SIZE = 1000
# 单块目标长度
CHUNK_SIZE = 600
# 相邻块重叠长度，仅在「单个原子块必须从中间切开」时生效
CHUNK_OVERLAP = 50
# 短碎片阈值：同章节内正文低于该长度的分块尝试向后合并（合并上限 CHUNK_MAX_SIZE）
CHUNK_MIN = 400
# 主体名识别时截取前 N 个切片作为上下文
CHUNKS_SPLIT_TOP_NUMBER = 10
# 向量化批次大小：每批处理 N 条切片，避免显存溢出
EMBEDDING_BATCH_SIZE = 6
