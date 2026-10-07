"""文档切分服务：结构感知解析 → 章节识别 → 两级切片（父块 + 子块）。

流程：

1. ``load_markdown_content``——读取并清洗 Markdown（图片增强后的 ``_new.md``）；
2. ``resolve_blocks``——优先消费 MinerU 的结构化产物 ``content_list.json``（可拿到 block 类型、
   标题层级、页码、表格 HTML），缺失时回退 Markdown 解析；两条路径产出同构的 Block 流；
3. ``build_sections``——按标题层级切出章节，每个章节产出父块（整棵子树文本，供命中后回填背景）；
4. ``split_section_children``——把章节自身的原子块切成子块（检索单元）。

历史实现的三段式（标题正则粗切 → 递归字符细切 → 同标题短块合并）已整体替换：其前言截断、
无正文章节标题丢失、代码块缩进抹平、长表格丢表头、短块合并不可达等问题见
``tests/unit/test_split_regressions.py``。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.process.import_.agent.state import ImportGraphState
from app.rag.import_.content_list import build_image_ref_map, find_content_list, normalize_content_list
from app.rag.import_.ids import make_doc_id
from app.rag.import_.markdown_blocks import normalize_markdown
from app.rag.import_.section_splitter import Block, split_blocks
from app.shared.runtime.logger import logger, step_log
from app.shared.utils.require import fail, require_state_str


@step_log("load_markdown_content")
def load_markdown_content(state: ImportGraphState) -> tuple[str, str]:
    """读取并清洗 Markdown 内容：``md_content`` 为空时回退读取 ``md_path``，统一换行符。"""
    md_content = state.get("md_content")
    file_title = state.get("file_title")
    md_path = state.get("md_path")

    if not md_content and md_path and Path(md_path).exists():
        logger.warning(f"md_content 为空，回退从 md_path 读取：{md_path}")
        md_content = Path(md_path).read_text(encoding="utf-8")
    if not md_content:
        fail("md_content", "为空且无法从 md_path 读取")

    fallback_title = Path(md_path).stem if md_path and Path(md_path).exists() else "default"
    file_title = require_state_str(state, "file_title", default=fallback_title)

    # 数据清洗：统一换行符为 \n
    md_content = md_content.replace("\r\n", "\n").replace("\r", "\n")
    state["md_content"] = md_content
    state["file_title"] = file_title
    return md_content, file_title


@step_log("resolve_blocks")
def resolve_blocks(md_content: str, md_path: str | None) -> tuple[list[Block], str]:
    """产出归一化 Block 流，返回 ``(blocks, 来源标识)``。

    结构化产物缺失或不可用时**回退** Markdown 解析，绝不中断导入——存量文档尚未重跑
    MinerU 时仍可正常入库。
    """
    content_list_path = find_content_list(md_path)
    if content_list_path is not None:
        raw_blocks: Any = None
        try:
            raw_blocks = json.loads(content_list_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 产物损坏时回退，不阻断导入
            logger.warning(f"结构化产物读取失败（回退 Markdown 解析）：{content_list_path}：{exc}")

        if isinstance(raw_blocks, list) and raw_blocks:
            blocks = normalize_content_list(raw_blocks, build_image_ref_map(md_content))
            if blocks:
                logger.info(
                    f"使用结构化产物切分：{content_list_path.name}，"
                    f"原始块={len(raw_blocks)} 归一化块={len(blocks)}"
                )
                return blocks, "content_list"
            logger.warning(f"结构化产物归一化后无有效块，回退 Markdown 解析：{content_list_path}")
        elif raw_blocks is not None:
            logger.warning(f"结构化产物结构异常（非空列表校验未通过），回退 Markdown 解析：{content_list_path}")
    else:
        logger.info(f"未找到结构化产物，使用 Markdown 解析：{md_path}")

    return normalize_markdown(md_content), "markdown"


@step_log("backup_chunks_json")
def backup_chunks_json(chunks_list: list[dict[str, Any]], md_path: str) -> None:
    """把子块备份到 Markdown 同目录的 ``<stem>.json``（供 ``load_chunks`` 回退读取）。"""
    json_path_obj: Path = Path(md_path).with_name(f"{Path(md_path).stem}.json")
    json_path_obj.write_text(json.dumps(chunks_list, indent=4, ensure_ascii=False), encoding="utf-8")
    logger.info(f"已经将切片数据,备份到{json_path_obj}位置!!")


@step_log("backup_parents_json")
def backup_parents_json(parents_list: list[dict[str, Any]], md_path: str) -> None:
    """把父块备份到 Markdown 同目录的 ``<stem>_parents.json``。"""
    json_path_obj: Path = Path(md_path).with_name(f"{Path(md_path).stem}_parents.json")
    json_path_obj.write_text(json.dumps(parents_list, indent=4, ensure_ascii=False), encoding="utf-8")
    logger.info(f"已经将父块数据,备份到{json_path_obj}位置!!")


@step_log("split_document")
def split_document(state: ImportGraphState) -> ImportGraphState:
    """文档切分服务入口：产出 ``chunks``（子块）与 ``parent_chunks``（父块）。"""
    md_content, file_title = load_markdown_content(state)
    blocks, source = resolve_blocks(md_content, state.get("md_path"))

    doc_key = make_doc_id(file_title)
    parents, children = split_blocks(blocks, doc_key, file_title)

    # 空产出必须显式失败：否则下游 load_chunks 会静默回退到上一次导入的陈旧备份，
    # 用旧内容覆盖新内容且无告警。
    if not children:
        fail("chunks", "切分后为空")

    state["chunks"] = children
    state["parent_chunks"] = parents
    logger.info(
        f"两级切片完成（来源={source}）：父块={len(parents)} 子块={len(children)}，"
        f"子块正文长度中位数={_median_body_len(children)}"
    )

    backup_chunks_json(children, state["md_path"])
    backup_parents_json(parents, state["md_path"])
    return state


def _median_body_len(children: list[dict[str, Any]]) -> int:
    """子块正文字长中位数（不含标题前缀），用于日志观测切分粒度。"""
    lengths = sorted(len(c["content"]) - len(c["title"]) - 1 for c in children)
    if not lengths:
        return 0
    return lengths[len(lengths) // 2]
