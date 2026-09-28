"""主体名目录：从 Milvus ``kb_item_names`` 拉取全量主体名，支撑「库内标准名命中」判定。

背景：主体名向量检索的绝对分数量级依赖 Milvus 版本与 ``norm_score`` 语义
（实测「提问与库内主体名完全相同」也仅 0.662~0.664），因此判定不能只看绝对分数。
本模块提供「库内标准名」目录，以及同一实体判定能力，让「用户明确报出产品名」这一
最常见场景可以确定性命中。

同一实体的多种写法不靠人工维护名单，而是：
- 写入端：导入识别后归并到库内已有同一实体（见 ``item_name_service``），从源头避免近重复；
- 读取端：``is_same_entity`` 按「短名是长名后缀」判同一实体（品牌前缀差异），
  用于间距判定与召回扩展，兜底存量数据。

设计要点：
- 进程内短 TTL 缓存（默认 60s），避免每次提问都打 Milvus；
- 任何异常（集合不存在 / 连接失败 / 字段缺失）一律回退为空目录并告警，
  **绝不抛出、绝不阻断主链路**；失败同样计入 TTL，避免异常期高频重试。
"""
from __future__ import annotations

import time

from app.rag.item_name.config import ITEM_NAME_CATALOG_TTL_SECONDS
from app.shared.clients.milvus_gateway import milvus_gateway
from app.shared.runtime.logger import logger
from app.shared.utils.text import is_same_entity, name_tokens, normalize_item_name, token_prefix_match

# 目录缓存：names 为去重后的库内标准名；raws 为库内全部原始写法（用于同一实体召回扩展）
_CACHE: dict[str, object] = {"names": [], "raws": [], "ts": 0.0}

# 单次拉取上限：主体名属低频小集合，给足以避免分页遗漏
_QUERY_LIMIT = 16384


def _ensure_loaded(force: bool) -> None:
    """按 TTL 加载/刷新目录（异常降级为空目录）。"""
    now = time.time()
    if not force and _CACHE["names"] and (now - float(_CACHE["ts"])) < ITEM_NAME_CATALOG_TTL_SECONDS:
        return

    names: list[str] = []
    raws: list[str] = []
    try:
        client = milvus_gateway.milvus_client
        collection = milvus_gateway.item_name_collection_name
        if client is None:
            logger.warning("主体名目录加载跳过：Milvus 客户端不可用")
        elif not client.has_collection(collection_name=collection):
            logger.warning(f"主体名目录集合不存在：{collection}，目录命中降级为不可用")
        else:
            try:
                client.load_collection(collection_name=collection)
            except Exception as exc:  # noqa: BLE001 - 加载失败不影响后续查询尝试
                logger.warning(f"主体名目录集合加载失败（继续尝试查询）：{exc}")
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
                if key in seen:  # 归一化后同一写法（仅空格/大小写差异）只保留一条
                    continue
                seen.add(key)
                names.append(raw)
            logger.info(f"主体名目录已刷新：{len(names)} 条（去重后）")
    except Exception as exc:  # noqa: BLE001 - 目录属增强能力，异常必须降级而非中断提问
        logger.warning(f"主体名目录加载失败，目录命中降级为不可用：{exc}")
        names, raws = [], []

    _CACHE["names"] = names
    _CACHE["raws"] = raws
    _CACHE["ts"] = now


def load_item_names(force: bool = False) -> list[str]:
    """加载库内全量主体名（去重、带 TTL 缓存）；任何异常返回空列表。"""
    _ensure_loaded(force)
    return list(_CACHE["names"])


def equivalent_item_names(name: object) -> list[str]:
    """返回与 ``name`` 同一实体的全部已知写法（库内实际写法），用于召回扩展。

    库内不存在同一实体写法时，返回 ``name`` 自身，保证过滤条件不丢主体。
    """
    _ensure_loaded(False)
    if not normalize_item_name(name):
        return []
    out: list[str] = []
    for raw in list(_CACHE["raws"]):
        if raw and raw not in out and is_same_entity(raw, name):
            out.append(raw)
    if not out:
        text = str(name or "").strip()
        if text:
            out.append(text)
    return out


def expand_item_names(names: list[str]) -> list[str]:
    """把主体名列表扩展为「同一实体的全部写法」，避免按单一写法过滤导致漏召回。"""
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
    """在库内主体名目录中匹配 ``name``，返回 ``(库内标准名, 命中方式)``。

    先归一化精确同名，再退化为「同一实体」。命中方式 ∈
    ``{"catalog_exact", "catalog_entity"}``，便于日志区分并事后抽查误归并。
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


# 子串相似的最短长度：过短（如 "机"、"180"）会匹配到一切，必须设下限
_MIN_CONTAINS_LEN = 3


def find_similar_names(name: object, *, limit: int = 3) -> list[tuple[str, str]]:
    """在库内主体名目录中找「相似主体」，用于没确认到主体时让用户点选。

    与 ``match_catalog_name`` 的区别：那里只认「同名 / 同一实体」，命中就自动确认；
    这里更宽松，专门处理「型号前缀」这类不足以自动确认、但明显指向同一主体的写法：

    - ``catalog_contains``：归一化后互为子串（``hak180`` ⊂ ``brotherhak180烫金机``），
      要求较短的一方至少 3 个字符，避免 ``机`` / ``180`` 这类噪声；
    - ``catalog_token_prefix``：token 前缀等价（``HAK 180`` vs ``HAK 180 烫金机``）。

    :return: ``[(库内标准名, 命中方式), ...]``，按相似强度排序（子串 > token 前缀）
    """
    key = normalize_item_name(name)
    if not key:
        return []

    matched: list[tuple[int, str, str]] = []
    seen: set[str] = set()
    for candidate in load_item_names():
        candidate_key = normalize_item_name(candidate)
        if not candidate_key or candidate_key in seen:
            continue
        seen.add(candidate_key)
        shorter = min(len(key), len(candidate_key))
        if shorter >= _MIN_CONTAINS_LEN and (key in candidate_key or candidate_key in key):
            matched.append((2, candidate, "catalog_contains"))
            continue
        if token_prefix_match(name_tokens(candidate), name_tokens(name)):
            matched.append((1, candidate, "catalog_token_prefix"))

    matched.sort(key=lambda item: (-item[0], len(item[1])))
    return [(candidate, how) for _, candidate, how in matched[:limit]]
