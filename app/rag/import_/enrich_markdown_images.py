"""Markdown 图片增强服务：扫描图片 → 视觉模型生成说明 → 上传 MinIO → 替换图片链接。"""
from __future__ import annotations

import base64
import re
from mimetypes import guess_type
from pathlib import Path

from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser

from app.process.import_.agent.state import ImportGraphState
from app.rag.config import SUPPORTED_IMAGE_EXTENSIONS
from app.shared.clients.minio_gateway import minio_gateway
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger, step_log
from app.shared.runtime.prompts import load_prompt
from app.shared.utils.require import require_state_str

# 图片上下文窗口（字符数）：取图片引用前后的字符片段作为视觉模型的语义线索
CONTEXT_WINDOW = 200
# Markdown 图片引用模板：`![说明](地址)`，中间插入 re.escape 后的图片名以定位单张图。
# 注意转义括号 `\[` `\]` `\(` `\)` 不可写作 `$`（`$` 是行尾锚点，会导致永不匹配）。
_IMAGE_REF_TEMPLATE = r"\!\[.*?\]\(.*?%s.*?\)"


def _image_ref_pattern(image_name: str) -> re.Pattern[str]:
    """构造定位单张图片引用的正则（图片名为字面量，需转义）。"""
    return re.compile(_IMAGE_REF_TEMPLATE % re.escape(image_name))


def _extract_context(md_content: str, anchor: int, window: int, *, pre: bool) -> str:
    """取 ``anchor`` 位置前/后 ``window`` 字符的上下文，并剥离首尾空白。

    不做句界对齐：图片通常独占一行，其紧邻的换行本身就是「最近句界」，
    按句界收缩会得到空串，使上下文完全失效。此处按窗口硬截断即可——
    提示词中已明确告知这是「上文/下文」片段，无需保证句子完整。
    """
    if pre:
        return md_content[max(0, anchor - window):anchor].strip()
    return md_content[anchor:min(len(md_content), anchor + window)].strip()

@step_log("validate_markdown_source")
def validate_markdown_source(state: ImportGraphState) -> tuple[str, Path, Path]:
    """校验 ``md_path`` 并读取内容，返回 ``(md_content, images_dir, md_path)``。"""
    md_path = require_state_str(state, "md_path")
    md_path_obj = Path(md_path)
    if not md_path_obj.exists():
        raise FileNotFoundError(f"md_path 指向的文件不存在：{md_path}")

    md_content = md_path_obj.read_text(encoding="utf-8")
    if not md_content:
        raise ValueError(f"md_path 内容为空：{md_path}")

    state["md_content"] = md_content
    return md_content, md_path_obj.parent / "images", md_path_obj


@step_log("scan_images")
def scan_images(images_dir: Path, md_content: str) -> list[tuple[str, str, tuple[str, str]]]:
    """扫描被 Markdown 引用的图片，返回 ``[(图片名, 路径, (上文, 下文)), ...]``。"""
    image_context: list[tuple[str, str, tuple[str, str]]] = []
    for image_obj in images_dir.iterdir():
        image_name = image_obj.name
        if image_obj.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
            continue
        match = _image_ref_pattern(image_name).search(md_content)
        if not match:
            logger.debug(f"{image_name} 未被 Markdown 引用，跳过")
            continue
        start, end = match.span()
        pre_context = _extract_context(md_content, start, CONTEXT_WINDOW, pre=True)
        post_context = _extract_context(md_content, end, CONTEXT_WINDOW, pre=False)
        image_context.append((image_name, str(image_obj), (pre_context, post_context)))
    return image_context


@step_log("summarize_images")
def summarize_images(
    image_content: list[tuple[str, str, tuple[str, str]]],
    stem: str,
) -> dict[str, str]:
    """调用视觉模型为每张图片生成说明，返回 ``{图片名: 说明}``。"""
    vision_client = llm_providers.vision_chat()
    chain = vision_client | StrOutputParser()
    summaries: dict[str, str] = {}
    for image_name, image_path_str, image_context in image_content:
        prompt_text = load_prompt("image_summary", root_folder=stem, image_content=image_context)
        image_base64 = base64.b64encode(Path(image_path_str).read_bytes()).decode(encoding="utf-8")
        message = HumanMessage(content=[
            {"type": "text", "text": prompt_text},
            {"type": "image_url", "image_url": {"url": f"data:{guess_type(image_name)[0]};base64,{image_base64}"}},
        ])
        summary = chain.invoke([message])
        summaries[image_name] = summary
        logger.info(f"图片识别完成：{image_name}")
    return summaries


@step_log("upload_images_and_replace")
def upload_images_and_replace(
    md_content: str,
    summaries_images: dict[str, str],
    image_content: list[tuple[str, str, tuple[str, str]]],
    stem: str,
) -> str:
    """上传图片到 MinIO 并把 Markdown 中的图片引用替换为带说明的公网地址。"""
    # 1. 先清理该文档目录下的旧图片（重复导入时避免残留）
    minio_gateway.remove_images(stem)

    # 2. 逐张上传；单张失败不影响整体
    image_urls: dict[str, str] = {}
    for image_name, image_path_str, _ in image_content:
        try:
            image_urls[image_name] = minio_gateway.upload_image(
                stem, image_name, image_path_str, guess_type(image_name)[0]
            )
        except Exception as exc:  # noqa: BLE001 - 单张图片失败继续处理其余图片
            logger.warning(f"{image_name} 上传 MinIO 失败（跳过）：{exc}")

    if not image_urls:
        logger.warning("图片全部上传失败，保留原始 Markdown")
        return md_content

    # 3. 替换 ![](xxx) 为 ![图片说明](公网地址)
    for image_name, image_url in image_urls.items():
        summary = summaries_images.get(image_name) or ""
        pattern = _image_ref_pattern(image_name)
        md_content = pattern.sub(lambda _: f"![{summary}]({image_url})", md_content)
    return md_content


@step_log("backup_new_md_content")
def backup_new_md_content(md_content_new: str, md_path_obj: Path) -> str:
    """把替换后的 Markdown 另存为 ``<原名>_new.md`` 并返回新路径。"""
    new_path_obj = md_path_obj.with_name(f"{md_path_obj.stem}_new{md_path_obj.suffix}")
    new_path_obj.write_text(md_content_new, encoding="utf-8")
    logger.info(f"已备份图片增强后的 Markdown：{new_path_obj}")
    return str(new_path_obj)


@step_log("enrich_markdown_images")
def enrich_markdown_images(state: ImportGraphState) -> ImportGraphState:
    """图片增强服务入口（无图片时直接原样返回）。"""
    md_content, images_dir, md_path_obj = validate_markdown_source(state)
    if not images_dir.exists() or images_dir.is_file() or not any(images_dir.iterdir()):
        logger.info(f"{md_path_obj} 没有待处理图片，跳过图片增强")
        return state

    image_content = scan_images(images_dir, md_content)
    if not image_content:
        logger.info(f"{md_path_obj} 的图片均未被引用，跳过图片增强")
        return state

    image_summaries = summarize_images(image_content, md_path_obj.stem)
    md_content_new = upload_images_and_replace(md_content, image_summaries, image_content, md_path_obj.stem)
    state["md_content"] = md_content_new
    state["md_path"] = backup_new_md_content(md_content_new, md_path_obj)
    return state
