"""
评估样本定义模块。

维护评测主体、联网占位结果及题库文件读写。
真实切分样本由 split_dataset 生成；本模块不再手写切片或题库。
"""

import json
from pathlib import Path


# 当前演示样本对应的主体名称与文件标题。
TEST_ITEM_NAME = "HAK 180"
TEST_FILE_TITLE = "HAK 180"

# 评估过程中生成的题库文件和报告统一放到包内 artifacts 目录。
ARTIFACTS_DIR = Path(__file__).resolve().parent / "artifacts"
GENERATED_BATCH_CASES_FILE = ARTIFACTS_DIR / "hak180_eval_cases.json"


def build_web_search_docs() -> list[dict]:
    """
    构造联网占位结果。

    返回值：
    - list[dict]：模拟联网检索结果的列表

    字段说明：
    - title：网页标题
    - snippet：网页摘要
    - url：网页地址

    为什么只放一条固定数据：
    - 当前项目 rerank 阶段要求同时存在本地候选和联网候选；
    - 评测重点是本地召回链路，所以这里用稳定占位数据避免联网波动干扰结果。
    """
    return [
        {
            "title": "联网占位结果",
            "snippet": "这是一条联网占位结果，用于满足 rerank 入参，不提供关键答案。",
            "url": "https://example.com/hak180-placeholder",
        }
    ]


def write_batch_eval_cases(case_list: list[dict]) -> None:
    """
    将题库写入磁盘。

    参数：
    - case_list：要保存的题库列表

    这样可以把“准备测试数据”和“执行评测”拆成两步。
    """
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_BATCH_CASES_FILE.write_text(
        json.dumps(case_list, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_batch_eval_cases() -> list[dict]:
    """
    读取批量评测题库。

    返回值：
    - list[dict]：题库列表；如果文件不存在则返回空列表

    题库文件不存在时，通常说明还没先执行测试数据入库。
    """
    if not GENERATED_BATCH_CASES_FILE.exists():
        return []
    return json.loads(GENERATED_BATCH_CASES_FILE.read_text(encoding="utf-8"))
