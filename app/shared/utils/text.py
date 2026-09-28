"""
文本归一与主体名等价判定（导入端 / 查询端 / 自进化共用一份实现）。

历史问题：主体名等价判定曾在三个地方各写一份（``item_name/catalog.py``、
``item_name/match.py``、``evolution/retrieval.py``），口径不一致会导致
「导入时归并到 A，提问时确认到 B，进化条目召回不到」。此处收敛为唯一实现。
"""
from __future__ import annotations

import re
import unicodedata

# 后缀包含判定的最短长度：过短（如 "1"、"180"）易误合并
MIN_SUFFIX_LEN = 4

# 多主体分隔符（写入端用逗号连接多个主体名）
_SUBJECT_SEPARATOR = re.compile(r"[,，]")


def normalize_item_name(name: object) -> str:
    """归一化主体名用于比较：全角转半角、去除所有空白、统一小写。"""
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", str(name))
    return "".join(text.split()).lower()


def is_same_entity(name_a: object, name_b: object) -> bool:
    """判断两个主体名是否指向同一实体。

    判据：归一化相等，或「短名是长名的后缀且紧邻字符非数字」。
    后者对应品牌前缀差异（``brotherhak180烫金机`` 以 ``hak180烫金机`` 结尾）。
    刻意**不**把「短名是长名的前缀」视为同一实体，以保留父/子型号歧义
    （``HAK 180`` vs ``HAK 180 烫金机``）必须反问的既有契约。
    """
    key_a, key_b = normalize_item_name(name_a), normalize_item_name(name_b)
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True
    short, long = (key_a, key_b) if len(key_a) <= len(key_b) else (key_b, key_a)
    if len(short) < MIN_SUFFIX_LEN or not long.endswith(short):
        return False
    # 紧邻字符若是数字，说明是不同型号（如 hak180 vs hak1800），不合并
    return not long[len(long) - len(short) - 1].isdigit()


def name_tokens(name: object) -> list[str]:
    """主体名切分：NFKC 归一 + 小写 + 按空白切 token（保留 token 边界）。"""
    return unicodedata.normalize("NFKC", str(name or "")).lower().split()


def token_prefix_match(a: list[str], b: list[str]) -> bool:
    """较短 token 序列是较长者的前缀即视为等价。

    兼容 ``HAK 180`` 与 ``HAK 180 烫金机`` 这类「短名是长名前缀」的写法，
    但 ``HAK 180`` 与 ``HAK 1800`` 不构成前缀，避免尾号误判。
    """
    if not a or not b:
        return False
    m = min(len(a), len(b))
    return a[:m] == b[:m]


def split_subjects(text: object) -> list[str]:
    """把 ``a,b`` 形式的多主体字符串拆成单个主体（忽略空项）。"""
    return [item.strip() for item in _SUBJECT_SEPARATOR.split(str(text or "")) if item.strip()]


def name_equivalent(stored: object, query_names: list[str], *, global_item: str) -> bool:
    """判断「库内某条目的主体名」是否与查询主体列表等价（供进化条目召回）。

    - ``global_item`` 占位符（未打商品标签的条目）视为全局可召回；
    - 否则逐主体与查询名做 token 前缀等价判定。
    """
    text = str(stored or "").strip()
    if text == global_item:
        return True
    query_tokens = [name_tokens(name) for name in query_names]
    for subject in split_subjects(text):
        subject_tokens = name_tokens(subject)
        if any(token_prefix_match(subject_tokens, qt) for qt in query_tokens):
            return True
    return False


def join_subjects(names: list[str]) -> str:
    """去重保序后用逗号连接多个主体名（写入端口径）。"""
    resolved: list[str] = []
    for raw in names or []:
        name = str(raw or "").strip()
        if name and name not in resolved:
            resolved.append(name)
    return ",".join(resolved)
