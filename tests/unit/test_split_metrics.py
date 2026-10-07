"""切分质量指标测试（纯函数）。"""
from __future__ import annotations

from app.rag.import_.section_splitter import Block
from app.rag_eval.split_metrics import (
    body_of,
    evaluate_split_quality,
    fragment_rate,
    line_coverage,
    parent_child_integrity,
    page_provenance_rate,
    table_integrity,
)


def chunk(title: str, body: str, **overrides) -> dict:
    item = {
        "chunk_id": f"id-{title}-{len(body)}",
        "title": title,
        "content": f"{title}\n{body}",
        "heading_path": title,
        "page": 1,
        "parent_id": f"p-{title}",
    }
    item.update(overrides)
    return item


def test_body_of_strips_title_prefix():
    assert body_of(chunk("标题", "正文内容")) == "正文内容"


def test_line_coverage_detects_lost_content():
    blocks = [Block("heading", "标题"), Block("text", "第一段"), Block("text", "第二段")]
    complete = [chunk("标题", "第一段\n\n第二段")]
    assert line_coverage(blocks, complete) == 1.0

    # 第二段丢失：3 个源块（标题 + 两段）只覆盖了 2 个
    incomplete = [chunk("标题", "第一段")]
    assert line_coverage(blocks, incomplete) == round(2 / 3, 4)


def test_line_coverage_accepts_heading_in_breadcrumb():
    """无正文的章节标题只作为父块存在，其文本会出现在后代切片的面包屑里。"""
    blocks = [Block("heading", "第3章 网络配置")]
    children = [chunk("3.1 接口配置", "正文", heading_path="第3章 网络配置 / 3.1 接口配置")]
    assert line_coverage(blocks, children) == 1.0


def test_table_integrity_requires_header_in_same_chunk():
    table = "| 参数 | 默认值 |\n| --- | --- |\n| 温度 | 180 |\n| 压力 | 3 |"
    blocks = [Block("table", table)]

    with_header = [chunk("参数表", f"{table}\n\n补充说明")]
    assert table_integrity(blocks, with_header) == 1.0

    # 历史缺陷形态：数据行被切到没有表头的分块里
    headerless = [chunk("参数表", "| 参数 | 默认值 |\n| --- | --- |\n| 温度 | 180 |"), chunk("参数表", "| 压力 | 3 |")]
    assert table_integrity(blocks, headerless) == 0.0


def test_table_integrity_ok_when_no_table():
    assert table_integrity([Block("text", "没有表格")], [chunk("t", "b")]) == 1.0


def test_fragment_rate_reports_both_ratios():
    children = [chunk("短", "短内容"), chunk("长", "内" * 500)]
    rate = fragment_rate(children, min_chars=400)
    assert rate["chunk_ratio"] == 0.5
    # 内容占比远低于数量占比——这正是结构感知切分下的正确口径
    assert 0 < rate["content_ratio"] < 0.05


def test_fragment_rate_handles_empty_input():
    assert fragment_rate([])["chunk_ratio"] == 0.0


def test_page_provenance_rate():
    children = [chunk("a", "x", page=3), chunk("b", "y", page=0), chunk("c", "z", page=7)]
    assert page_provenance_rate(children) == round(2 / 3, 4)


def test_parent_child_integrity_flags_orphans():
    parents = [{"parent_id": "p1"}]
    children = [chunk("a", "x", parent_id="p1"), chunk("b", "y", parent_id="missing")]
    integrity = parent_child_integrity(parents, children)
    assert integrity["resolvable_ratio"] == 0.5
    assert integrity["parent_count"] == 1
    assert integrity["child_count"] == 2


def test_evaluate_split_quality_summarises_everything():
    blocks = [Block("heading", "标题"), Block("text", "正文")]
    children = [chunk("标题", "正文")]
    quality = evaluate_split_quality(children, [{"parent_id": "p-标题"}], blocks)
    assert quality["子块数"] == 1
    assert quality["父块数"] == 1
    assert quality["源内容覆盖率"] == 1.0
    assert quality["表格完整率"] == 1.0
    assert quality["章节面包屑覆盖率"] == 1.0
    assert quality["父子关联"]["resolvable_ratio"] == 1.0
