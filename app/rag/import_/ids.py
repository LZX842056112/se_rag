"""确定性切片标识：文档 / 章节 / 子块三级哈希 id。

设计要点：

1. **id 只编码结构，不编码内容**——``doc_id#heading_path#occurrence#part``。因此文档内容
   微调、或同一文档重复导入时 id 保持稳定，绑在 id 上的标注、反馈、引用不会失效。
   内容是否变化由独立的 ``content_hash`` 字段感知。
2. **``occurrence`` 用于消歧重名标题**——硬件手册中 ``## 设备``、``## 电源线`` 会在不同父
   章节下重复出现，仅靠 ``heading_path`` 无法区分，必须叠加「该路径在本文件内第几次出现」。
3. **选 40 位十六进制字符串而非整数主键**——``milvus_gateway.in_expr`` 只产出字符串字面量，
   字符串主键可直接复用于父块批量回取，无需新增整型表达式构造器；十六进制不含引号与换行，
   天然免转义。
"""
from __future__ import annotations

import hashlib

# 哈希输出长度（sha1 十六进制）
_HASH_LEN = 40


def _sha1(text: str) -> str:
    """返回文本的 sha1 十六进制摘要。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def make_doc_id(file_title: str) -> str:
    """由文件标题生成文档级稳定标识（大小写与首尾空白不敏感）。"""
    return _sha1((file_title or "").strip().lower())


def section_key(heading_path: str, occurrence: int) -> str:
    """章节键：标题面包屑 + 该面包屑在本文件内的出现序号。"""
    return f"{heading_path or ''}#{int(occurrence)}"


def make_parent_id(doc_key: str, heading_path: str, occurrence: int) -> str:
    """父块（章节级）标识。"""
    return _sha1(f"{doc_key}#{section_key(heading_path, occurrence)}")


def make_chunk_id(doc_key: str, heading_path: str, occurrence: int, part: int) -> str:
    """子块（检索单元）标识。"""
    return _sha1(f"{doc_key}#{section_key(heading_path, occurrence)}#{int(part)}")


def make_content_hash(content: str) -> str:
    """内容指纹，用于感知「结构未变但内容变了」的情况。"""
    return _sha1(content or "")


def truncate_utf8(text: str, max_bytes: int) -> str:
    """按 UTF-8 字节数截断文本，且不切断多字节字符。

    Milvus 的 ``VARCHAR`` 长度按**字节**计，超限会导致插入失败，故写入前统一走此函数。
    """
    if not text:
        return text or ""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore")
