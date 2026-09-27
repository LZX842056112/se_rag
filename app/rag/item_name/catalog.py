"""主体名目录：从 Milvus ``kb_item_names`` 拉取全量主体名，支撑"库内标准名命中"判定。

背景：主体名向量检索的绝对分数量级依赖 Milvus 版本与 ``norm_score`` 语义
（旧注释假设满分≈0.875，实测"提问与库内主体名完全相同"也仅 0.662~0.664），
因此判定不能只依赖绝对分数。本模块提供"库内标准名"目录与归一化 / 同一实体判定能力，
让"用户明确报出产品名"这一最常见场景可以确定性命中。

同一实体的多种写法不靠人工维护名单，而是：
- 写入端：导入识别后归并到库内已有同一实体（见 ``app/rag/import_/item_name_service.py``），
  从源头避免近重复主体名；
- 读取端：``is_same_entity`` 按"短名是长名后缀"判同一实体（品牌前缀差异），
  用于间距判定与召回扩展，兜底存量数据。

设计要点：
- 进程内短 TTL 缓存（默认 60s），避免每次提问都打 Milvus；
- 任何异常（集合不存在 / 连接失败 / 字段缺失 / 查询不支持）一律回退为空目录并告警，
  **绝不抛出、绝不阻断主链路**；失败后同样计入 TTL，避免异常期高频重试；
- 归一化仅用于"比较"：NFKC（全角→半角）+ 去除所有空白 + 统一小写，
  以吸收大模型抽取主体名时的空格/大小写/全半角抖动。
"""
from __future__ import annotations

import time
import unicodedata

from app.infra.vector_store.milvus_gateway import milvus_gateway
from app.rag.item_name.config import ITEM_NAME_CATALOG_TTL_SECONDS
from app.shared.runtime.logger import logger

# 目录缓存：names 为去重后的库内标准名；raws 为库内全部原始写法（用于同一实体召回扩展）；
# ts 为最近一次加载（含失败）时间
_CACHE: dict[str, object] = {"names": [], "raws": [], "ts": 0.0}

# 单次拉取上限：主体名属低频小集合，给足以避免分页遗漏
_QUERY_LIMIT = 16384

# 后缀包含判定的最短长度：过短（如 "1"、"180"）易误合并
_MIN_SUFFIX_LEN = 4


def normalize_item_name(name: object) -> str:
    """归一化主体名用于比较：全角转半角、去除所有空白、统一小写。"""
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", str(name))
    return "".join(text.split()).lower()


def is_same_entity(name_a: object, name_b: object) -> bool:
    """判断两个主体名是否指向同一实体（用于收敛近重复名、排除其对间距判定的干扰）。

    判据：归一化相等，或"短名是长名的后缀且紧邻字符非数字"。
    后者对应"品牌前缀差异"（`brotherhak180烫金机` 以 `hak180烫金机` 结尾）。
    刻意**不**把"短名是长名的前缀"视为同一实体，以保留父/子型号歧义
    （`HAK 180` vs `HAK 180 烫金机`）必须反问的既有契约。
    """
    key_a, key_b = normalize_item_name(name_a), normalize_item_name(name_b)
    if not key_a or not key_b:
        return False
    if key_a == key_b:
        return True
    short, long = (key_a, key_b) if len(key_a) <= len(key_b) else (key_b, key_a)
    if len(short) < _MIN_SUFFIX_LEN or not long.endswith(short):
        return False
    # 紧邻字符若是数字，说明是不同型号（如 hak180 vs hak1800），不合并
    return not long[len(long) - len(short) - 1].isdigit()


def _ensure_loaded(force: bool) -> None:
    now = time.time()
    if not force and _CACHE["names"] and (now - float(_CACHE["ts"])) < ITEM_NAME_CATALOG_TTL_SECONDS:
        return

    names: list[str] = []
    raws: list[str] = []
    try:
        client = milvus_gateway.milvus_client
        collection = milvus_gateway.item_name_collection_name
        if not client.has_collection(collection_name=collection):
            logger.warning(f"主体名目录集合不存在:{collection},目录命中降级为不可用")
        else:
            # 集合可能尚未 load，先尝试加载（失败不影响后续 query）
            try:
                client.load_collection(collection_name=collection)
            except Exception as e:  # noqa: BLE001 - 加载失败不影响查询尝试
                logger.warning(f"主体名目录集合加载失败(继续尝试查询):{e}")
            rows = client.query(
                collection_name=collection,
                filter="",
                output_fields=["item_name"],
                limit=_QUERY_LIMIT,
            )
            seen: set[str] = set()
            for row in rows or []:
                raw = str((row or {}).get("item_name") or "").strip()
                key = normalize_item_name(raw)
                if not key:
                    continue
                if raw not in raws:
                    raws.append(raw)
                # 归一化后同一写法（仅空格/大小写差异）只保留一条
                if key in seen:
                    continue
                seen.add(key)
                names.append(raw)
            logger.info(f"主体名目录已刷新:{len(names)} 条(去重后)")
    except Exception as e:  # noqa: BLE001 - 目录属增强能力，异常必须降级而非中断提问
        logger.warning(f"主体名目录加载失败,目录命中降级为不可用:{e}")
        names, raws = [], []

    _CACHE["names"] = names
    _CACHE["raws"] = raws
    _CACHE["ts"] = now


def load_item_names(force: bool = False) -> list[str]:
    """加载库内全量主体名（去重、带 TTL 缓存）。任何异常返回空列表。"""
    _ensure_loaded(force)
    return list(_CACHE["names"])  # type: ignore[arg-type]


def equivalent_item_names(name: object) -> list[str]:
    """返回与 name 同一实体的全部已知写法（库内实际写法），用于召回扩展。

    库内不存在同一实体写法时，返回 name 自身，保证过滤条件不丢主体。
    """
    _ensure_loaded(False)
    if not normalize_item_name(name):
        return []
    out: list[str] = []
    for raw in list(_CACHE["raws"]):  # type: ignore[arg-type]
        if raw and raw not in out and is_same_entity(raw, name):
            out.append(raw)
    if not out:
        text = str(name or "").strip()
        if text:
            out.append(text)
    return out


def expand_item_names(names: list[str]) -> list[str]:
    """把主体名列表扩展为"同一实体的全部写法"。

    避免只按其中一种写法过滤 Milvus，导致另一种写法下的分片被漏召回。
    """
    out: list[str] = []
    for name in names:
        for raw in equivalent_item_names(name):
            if raw and raw not in out:
                out.append(raw)
    return out


def match_catalog_exact(name: object) -> str | None:
    """归一化精确同名时返回库内标准名；否则 None。"""
    key = normalize_item_name(name)
    if not key:
        return None
    for candidate in load_item_names():
        if normalize_item_name(candidate) == key:
            return candidate
    return None


def match_catalog_name(name: object) -> tuple[str, str] | None:
    """在库内主体名目录中匹配 name，返回 ``(库内标准名, 命中方式)``；未命中返回 None。

    先归一化精确同名，再退化为"同一实体"（品牌前缀等写法差异）。
    命中方式 ∈ {"catalog_exact", "catalog_entity"}，便于日志区分"真同名"与"同实体"，
    也便于事后抽查是否发生了误归并。
    """
    exact = match_catalog_exact(name)
    if exact:
        return exact, "catalog_exact"
    if not normalize_item_name(name):
        return None
    for candidate in load_item_names():
        if is_same_entity(candidate, name):
            return candidate, "catalog_entity"
    return None
