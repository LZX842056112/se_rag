"""切分缺陷回归用例。

每条用例对应一个已在真实语料上复现过的历史缺陷：改造前失败、改造后必须通过。
"""
from __future__ import annotations

from app.rag.import_.config import CHUNK_MAX_SIZE
from app.rag.import_.ids import make_doc_id
from app.rag.import_.markdown_blocks import normalize_markdown
from app.rag.import_.section_splitter import build_sections, split_section_children


def run_split(md: str, file_title: str = "doc"):
    doc_key = make_doc_id(file_title)
    parents, sections = build_sections(normalize_markdown(md), doc_key, file_title)
    children = []
    for section in sections:
        children.extend(split_section_children(section, seq_start=len(children)))
    return parents, children


def body_of(chunk: dict) -> str:
    return chunk["content"][len(chunk["title"]) + 1:]


def test_p0_1_preamble_is_not_truncated():
    """P0-1：历史实现用 ``content[len(title)+1:]`` 定位正文，误以为标题在首行，
    导致首个标题之前的正文被截去等长前缀（实测「本手册适用于 H3C…」→「C MSR…」）。"""
    preamble = "本手册适用于 H3C MSR 系列路由器，阅读前请确认设备型号与软件版本。" * 20
    md = f"{preamble}\n\n# 1 产品介绍\n\n" + "设备支持多种组网方式与安全特性。" * 40
    _, children = run_split(md)
    joined = "\n".join(body_of(c) for c in children)
    assert "本手册适用于 H3C MSR 系列路由器" in joined


def test_p0_2_heading_without_body_is_preserved():
    """P0-2：历史实现要求标题下至少 2 行才结算，导致无正文的章节标题整段丢失。"""
    md = "# 第3章 网络配置\n\n## 3.1 接口配置\n\n以太网接口默认工作在二层模式。\n"
    parents, _ = run_split(md)
    assert "第3章 网络配置" in [p["title"] for p in parents]


def test_p0_3_code_block_keeps_indentation_and_blank_lines():
    """P0-3：历史实现对所有入块行做 strip 并跳过空行，代码块缩进与空行被抹平。"""
    md = (
        "# 4 配置命令\n\n请在设备上执行以下命令：\n\n"
        "```python\ndef apply():\n    if enabled:\n        return True\n\n    return False\n```\n\n"
        "命令执行完成后重启设备。\n"
    )
    _, children = run_split(md)
    code_chunks = [c for c in children if "```" in c["content"]]
    assert code_chunks
    code_lines = [
        line
        for chunk in code_chunks
        for line in body_of(chunk).split("\n")
    ]
    # 缩进层级必须与原文一致（逐行精确比较，避免子串误判）
    assert "def apply():" in code_lines
    assert "    if enabled:" in code_lines
    assert "        return True" in code_lines
    assert "    return False" in code_lines
    # 代码块内的空行必须保留
    assert "" in code_lines
    # 围栏成对
    assert sum(1 for line in code_lines if line.strip().startswith("```")) % 2 == 0


def test_p0_4_long_table_keeps_header_in_every_part():
    """P0-4：历史实现把表格当普通行参与字符切割，实测 1411 字符参数表切成 3 块，
    第 2、3 块成为无表头的碎片。"""
    rows = "\n".join(f"| 参数{i} | 取值范围 0-{i}00 | 默认值 {i} | 说明文字若干描述 |" for i in range(1, 60))
    md = "# 2 参数表\n\n| 参数 | 取值范围 | 默认值 | 说明 |\n| --- | --- | --- | --- |\n" + rows + "\n"
    _, children = run_split(md)
    table_chunks = [c for c in children if "| --- |" in c["content"]]
    assert len(table_chunks) > 1
    assert all("| 参数 | 取值范围 | 默认值 | 说明 |" in c["content"] for c in table_chunks)


def test_p0_4_real_world_html_table_stays_atomic():
    """真实语料中 MinerU 直接输出 HTML 表格，整表应保持为一个原子块。"""
    rows = "".join(f"<tr><td>部件{i}</td><td>×</td><td>○</td></tr>" for i in range(1, 20))
    html = f"<table><tr><td>部件名称</td><td>铅</td><td>汞</td></tr>{rows}</table>"
    md = f"# 产品中有害物质的名称及含量\n\n{html}\n\n本表格依据相关规定编制。\n"
    _, children = run_split(md)
    assert len(children) == 1
    assert "| 部件名称 | 铅 | 汞 |" in children[0]["content"]
    assert "部件19" in children[0]["content"]


def test_p1_1_short_sibling_parts_are_merged():
    """P1-1：历史实现的合并判定用「含标题前缀」的长度，而细切按剩余额度贪婪填满，
    导致除末块外每块恒接近上限、合并永远不触发（死代码）。"""
    md = "# 标题\n\n" + ("甲" * 300) + "\n\n" + ("乙" * 500) + "\n"
    _, children = run_split(md)
    assert len(children) == 1
    assert "甲" in body_of(children[0]) and "乙" in body_of(children[0])


def test_p1_1_trailing_short_part_is_merged_back():
    """尾段过短时也应并回前段，而不是留下一个碎片块。"""
    md = "# 标题\n\n" + ("甲" * 600) + "\n\n" + ("乙" * 80) + "\n"
    _, children = run_split(md)
    assert len(children) == 1
    assert "乙" in body_of(children[0])


def test_p1_2_splitting_does_not_break_identifiers_or_decimals():
    """P1-2：分隔符含裸 ``.`` 会在 config.py / 3.5 / 10.1.1.1 处误切。"""
    body = "配置项 config.py 的默认值为 3.5，网关地址为 10.1.1.1，" * 40
    _, children = run_split("# 配置说明\n\n" + body)
    joined = "\n".join(body_of(c) for c in children)
    assert "config.py" in joined
    assert "3.5" in joined
    assert "10.1.1.1" in joined


def test_p1_2_chinese_punctuation_is_used_as_boundary():
    """无句末标点的中文长句必须能在「，」处断开，而不是退化为按字符硬切。"""
    body = "检查" + "，".join(f"第{i}项参数是否正常" for i in range(1, 200))
    _, children = run_split("# 故障排查\n\n" + body)
    assert len(children) > 1
    # 每个切点都必须落在逗号上（逗号归属于前段或后段均可），不得切在词中间
    for previous, current in zip(children, children[1:]):
        assert body_of(current).startswith("，") or body_of(previous).rstrip().endswith("，")


def test_p1_3_paragraph_boundary_is_preserved():
    """P1-3：历史实现丢弃空行，段落边界信息消失。"""
    md = "# 标题\n\n段落一内容。\n\n段落二内容。\n"
    _, children = run_split(md)
    assert len(children) == 1
    assert "段落一内容。\n\n段落二内容。" in body_of(children[0])


def test_p1_5_slight_overflow_does_not_create_fragment():
    """P1-5：历史实现下正文 601 字符会产出 [600, 56] 两块，尾块是纯重叠碎片。"""
    for length in (598, 600, 601, 640):
        body = ("说明文字" * ((length // 4) + 1))[:length]
        _, children = run_split("# 概述\n\n" + body)
        assert len(children) == 1, f"正文 {length} 字符不应产生碎片块"
        assert len(body_of(children[0])) == length


def test_no_chunk_exceeds_hard_cap():
    """任何子块正文都不得超过硬上限。"""
    md = "# 长章节\n\n" + "。".join(["内容片段" * 40 for _ in range(60)]) + "\n"
    _, children = run_split(md)
    assert all(len(body_of(c)) <= CHUNK_MAX_SIZE for c in children)
