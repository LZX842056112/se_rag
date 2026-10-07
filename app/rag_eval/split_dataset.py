"""评估样本：把评测知识组织成文档，经**真实切分器**得到黄金切片。

历史做法（``dataset.build_import_chunks``）直接手写 8 条切片送进导入链路，切分器完全不在评估
链路内——切分策略怎么改都不会被发现；gold 又按**位置**切分（``gold_chunk_ids[:4]`` /
``[2:7]``），切片数量一变标注立刻失效。

现在改为三步：

1. 用 ``build_eval_document()`` 把评测知识组织成 Markdown 文档（标题层级 + 表格 + 代码块）；
2. 调 ``section_splitter.split_blocks``（与线上导入**同一套**切分逻辑）得到切片与确定性
   ``chunk_id``——因此 gold 可以在本地算出来，不再需要「先入库再回查自增主键」；
3. gold 由**内容关键词**推导，不依赖切片序号。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.rag.import_.content_list import build_image_ref_map, find_content_list, normalize_content_list
from app.rag.import_.ids import make_doc_id
from app.rag.import_.markdown_blocks import normalize_markdown
from app.rag.import_.section_splitter import Block, split_blocks
from app.rag_eval.dataset import TEST_FILE_TITLE, TEST_ITEM_NAME

# 真实解析产物所在目录名（用于切分质量基线；跨日期/uuid 目录用 glob 查找）
ARTIFACT_DOC_NAME = "hak180产品安全手册"

# 评测知识文档：内容与既有评测题保持一致，只是改为「文档 + 标题」形态，好让真实切分器介入
EVAL_DOCUMENT_MD = """# HAK 180 烫金机

## 局部烫印模式

### 局部烫印模式说明

HAK 180 的局部烫印模式用于只在纸张指定区域进行烫金转印，常用于局部图案、标题区域或指定装饰区域的加工。

### 局部烫印范围设置

HAK 180 在局部烫印模式下，若想只在纸张顶部 50mm 到 170mm 的区域转印烫金膜，应将起始位置设置为 50mm，结束位置设置为 170mm，然后保存参数。

### 局部烫印操作步骤

进入 HAK 180 操作面板的局部烫印菜单，选择定位设置，依次输入 50mm 起点和 170mm 终点，确认后执行试印。

### 局部烫印参数保存

HAK 180 在完成局部烫印的起点和终点设置后，需要先保存当前参数，再进行下一步试印，否则修改后的区间不会生效。

### 局部烫印注意事项

设置 HAK 180 局部烫印范围前，需要确认纸张原点、膜带张力和压印位置正常，否则会出现转印区域偏移。

### 局部烫印试印确认

HAK 180 完成局部烫印参数设置后，应先执行试印，观察 50mm 到 170mm 的转印区间是否准确，再进入正式生产。

### 局部烫印常见偏移原因

HAK 180 局部烫印出现转印区域偏移时，常见原因包括纸张原点错误、膜带张力异常、压印位置不准以及定位参数未重新保存。

### 局部烫印异常处理

如果 HAK 180 局部烫印试印后发现位置偏差，应重新检查纸张原点、膜带张力、压印位置，并再次确认起点终点参数是否正确。

## 局部烫印参数速查

| 参数项 | 取值范围 | 默认值 | 说明 |
| --- | --- | --- | --- |
| 起始位置 | 0mm-200mm | 0mm | 局部烫印转印区间起点 |
| 结束位置 | 0mm-200mm | 200mm | 局部烫印转印区间终点 |
| 膜带张力 | 1-5 | 3 | 张力等级，过高易断膜 |

## 参数下发示例

```json
{"mode": "local", "start_mm": 50, "end_mm": 170, "save": true}
```
"""

# 评测题库规格：gold 由内容关键词推导，与切片序号无关
_CASE_SPECS: list[dict[str, Any]] = [
    {
        "case_id": "hak180_local_region_eval_001",
        "question": "HAK 180 在局部烫印时怎么设置 50mm 到 170mm 区域，设置后还要做什么？",
        "gold_keywords": ["50mm", "170mm", "起始位置", "结束位置", "保存"],
        "must_hit_keywords": ["50mm", "170mm"],
        "tags": ["售后", "参数设置", "操作步骤", "局部烫印"],
    },
    {
        "case_id": "hak180_local_region_eval_002",
        "question": "HAK 180 局部烫印前后要注意什么，出现位置偏移一般该检查哪些项目？",
        "gold_keywords": ["纸张原点", "膜带张力", "压印位置", "偏移", "重新检查", "注意事项"],
        "must_hit_keywords": ["纸张原点", "膜带张力"],
        "tags": ["售后", "注意事项", "异常排查", "局部烫印"],
    },
]


def build_eval_document() -> str:
    """返回评测知识文档（Markdown）。"""
    return EVAL_DOCUMENT_MD


def build_web_search_docs() -> list[dict]:
    """联网占位结果：满足 rerank 入参要求，不提供关键答案。"""
    return [
        {
            "title": "联网占位结果",
            "snippet": "这是一条联网占位结果，用于满足 rerank 入参，不提供关键答案。",
            "url": "https://example.com/hak180-placeholder",
        }
    ]


def split_eval_document(md_content: str | None = None) -> dict[str, Any]:
    """对评测知识文档跑真实切分器，返回 ``{children, parents, blocks, doc_key}``。"""
    blocks = normalize_markdown(md_content if md_content is not None else EVAL_DOCUMENT_MD)
    doc_key = make_doc_id(TEST_FILE_TITLE)
    parents, children = split_blocks(blocks, doc_key, TEST_FILE_TITLE)
    return {"children": children, "parents": parents, "blocks": blocks, "doc_key": doc_key}


def find_artifact_dir(project_root: Path) -> Path | None:
    """在 ``output/*/*/<文档名>`` 中查找已缓存的 MinerU 解析产物目录。"""
    for candidate in sorted(project_root.glob(f"output/*/*/{ARTIFACT_DOC_NAME}")):
        if candidate.is_dir() and find_content_list(candidate / f"{ARTIFACT_DOC_NAME}.md"):
            return candidate
    return None


def split_real_artifact(project_root: Path) -> dict[str, Any] | None:
    """对已缓存的真实解析产物跑真实切分器（用于切分质量基线）。

    未找到产物时返回 None——切分质量段降级跳过，不影响检索评测。
    """
    artifact_dir = find_artifact_dir(project_root)
    if artifact_dir is None:
        return None

    md_candidates = sorted(artifact_dir.glob("*_new.md")) or sorted(artifact_dir.glob("*.md"))
    if not md_candidates:
        return None
    md_path = md_candidates[0]
    md_content = md_path.read_text(encoding="utf-8")

    content_list_path = find_content_list(md_path)
    blocks: list[Block]
    if content_list_path is not None:
        raw = json.loads(content_list_path.read_text(encoding="utf-8"))
        blocks = normalize_content_list(raw, build_image_ref_map(md_content))
    else:
        blocks = normalize_markdown(md_content)

    doc_key = make_doc_id(ARTIFACT_DOC_NAME)
    parents, children = split_blocks(blocks, doc_key, ARTIFACT_DOC_NAME)
    return {"children": children, "parents": parents, "blocks": blocks, "doc_key": doc_key}


def _match_chunk_ids(children: list[dict], keywords: list[str], *, require_all: bool) -> list[str]:
    """按关键词在切片正文中的命中情况取 chunk_id。"""
    matched: list[str] = []
    for child in children:
        body = child.get("content") or ""
        hit = all(keyword in body for keyword in keywords) if require_all else any(
            keyword in body for keyword in keywords
        )
        if hit:
            matched.append(child["chunk_id"])
    return matched


def build_cases_from_split(split: dict[str, Any], expected_item_names: list[str] | None = None) -> list[dict]:
    """由真实切片 + 内容关键词推导题库。

    ``gold_chunk_ids``：包含任一相关关键词的切片（相关答案范围）；
    ``must_hit_chunk_ids``：同时包含全部关键事实的切片（绝不能漏的那一条）。

    :param expected_item_names: 期望主体名。应传入**实际写入的主体名**——主体识别会把新名归并
        到库内标准名（如 ``HAK 180`` → ``HAK 180 烫金机``），硬编码常量会让检索过滤失配。
    """
    children = split["children"]
    item_names = expected_item_names or [TEST_ITEM_NAME]
    cases: list[dict] = []
    for spec in _CASE_SPECS:
        gold = _match_chunk_ids(children, spec["gold_keywords"], require_all=False)
        must_hit = _match_chunk_ids(children, spec["must_hit_keywords"], require_all=True)
        if not gold:
            raise RuntimeError(
                f"评测样本标注失败：{spec['case_id']} 的关键词在切片中均未命中，"
                f"说明评测文档与题库已不一致，请同步维护 _CASE_SPECS"
            )
        cases.append(
            {
                "case_id": spec["case_id"],
                "question": spec["question"],
                "expected_item_names": list(item_names),
                "gold_chunk_ids": gold,
                "must_hit_chunk_ids": must_hit or gold[:1],
                "tags": list(spec["tags"]),
            }
        )
    return cases
