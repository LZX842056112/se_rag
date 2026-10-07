"""结构感知解析测试：content_list 归一化与 HTML 表格转换。"""
from __future__ import annotations

from app.rag.import_.content_list import (
    build_image_ref_map,
    html_table_to_markdown,
    normalize_content_list,
)
from app.rag.import_.markdown_blocks import normalize_markdown

# 与真实 MinerU content_list 结构一致的样本（含页眉/页脚/页码/表格 colspan/图片）
SAMPLE_BLOCKS = [
    {"type": "image", "img_path": "images/abc123.jpg", "image_caption": [], "image_footnote": ["D01WD7001-00"],
     "bbox": [122, 29, 246, 65], "page_idx": 0},
    {"type": "text", "text": "HAK 180 烫金机", "text_level": 2, "bbox": [24, 104, 145, 129], "page_idx": 0},
    {"type": "text", "text": "感谢您购买 HAK 180 烫金机。", "bbox": [20, 165, 124, 179], "page_idx": 0},
    {"type": "header", "text": "brother", "bbox": [20, 29, 113, 64], "page_idx": 0},
    {"type": "footer", "text": "© 2021 Brother Industries, Ltd. 保留所有权利。", "bbox": [745, 952, 903, 967],
     "page_idx": 0},
    {"type": "page_number", "text": "1", "bbox": [969, 954, 976, 966], "page_idx": 0},
    {"type": "table", "table_body": '<table><tr><td>型号</td><td colspan="6">有害物质</td></tr>'
                                    '<tr><td>HAK180</td><td>铅</td><td>汞</td><td>镉</td><td>六价铬</td>'
                                    '<td>多溴联苯</td><td>多溴二苯醚</td></tr></table>',
     "table_caption": [], "table_footnote": [], "bbox": [747, 426, 975, 752], "page_idx": 0},
]


def test_page_furniture_is_dropped():
    """页眉/页脚/页码是页面装饰，不得作为正文入库。"""
    blocks = normalize_content_list(SAMPLE_BLOCKS, {})
    texts = " ".join(b.text for b in blocks)
    assert "brother" not in texts
    assert "Brother Industries" not in texts
    assert not any(b.text == "1" for b in blocks)


def test_text_level_becomes_heading():
    blocks = normalize_content_list(SAMPLE_BLOCKS, {})
    headings = [b for b in blocks if b.type == "heading"]
    assert [b.text for b in headings] == ["HAK 180 烫金机"]
    assert headings[0].level == 2


def test_page_index_is_one_based():
    blocks = normalize_content_list(SAMPLE_BLOCKS, {})
    assert all(b.page == 1 for b in blocks)


def test_table_body_becomes_atomic_markdown_table():
    blocks = normalize_content_list(SAMPLE_BLOCKS, {})
    tables = [b for b in blocks if b.type == "table"]
    assert len(tables) == 1
    lines = tables[0].text.split("\n")
    assert lines[0].startswith("| 型号 |")
    assert lines[1] == "| --- | --- | --- | --- | --- | --- | --- |"
    # colspan=6 的「有害物质」展开为 6 列，与后续数据行等宽
    assert lines[0].count("|") == 8


def test_image_block_uses_visual_summary_from_enriched_markdown():
    md = "![设备外观示意图，含型号标识](http://minio.local/upload-images/doc/abc123.jpg)\n"
    refs = build_image_ref_map(md)
    assert refs == {"abc123.jpg": "![设备外观示意图，含型号标识](http://minio.local/upload-images/doc/abc123.jpg)"}

    blocks = normalize_content_list(SAMPLE_BLOCKS, refs)
    images = [b for b in blocks if b.type == "image"]
    assert len(images) == 1
    assert "设备外观示意图" in images[0].text
    assert "abc123.jpg" in images[0].text


def test_image_block_falls_back_to_caption_then_raw_path():
    with_caption = [{"type": "image", "img_path": "images/x.jpg", "image_caption": ["图 1 面板"], "page_idx": 0}]
    assert normalize_content_list(with_caption, {})[0].text == "图 1 面板"

    without_anything = [{"type": "image", "img_path": "images/x.jpg", "page_idx": 0}]
    assert normalize_content_list(without_anything, {})[0].text == "![](images/x.jpg)"


def test_html_table_to_markdown_handles_rowspan():
    html = ('<table><tr><td rowspan="2">框架</td><td>铅</td></tr>'
            '<tr><td>汞</td></tr></table>')
    lines = html_table_to_markdown(html).split("\n")
    assert lines[0] == "| 框架 | 铅 |"
    assert lines[2] == "| 框架 | 汞 |"


def test_html_table_to_markdown_returns_empty_without_table():
    assert html_table_to_markdown("") == ""
    assert html_table_to_markdown("<p>普通段落</p>") == ""


def test_markdown_pipe_table_is_recognized_as_table():
    """手写 Markdown 的管道表也必须原子化，否则超长时会丢失表头。"""
    md = "# 参数表\n\n| 参数 | 默认值 |\n| --- | --- |\n| 温度 | 180 |\n"
    tables = [b for b in normalize_markdown(md) if b.type == "table"]
    assert len(tables) == 1
    assert tables[0].text.split("\n")[0] == "| 参数 | 默认值 |"


def test_pipe_lines_without_divider_stay_text():
    md = "# 标题\n\n| 这不是表格\n| 只是普通文本\n"
    assert not [b for b in normalize_markdown(md) if b.type == "table"]
