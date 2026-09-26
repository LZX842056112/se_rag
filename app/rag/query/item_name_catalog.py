"""主体名目录：从 Milvus ``kb_item_names`` 拉取全量主体名，支撑「归一化精确直通」判定。

背景：主体名向量检索的绝对分数量级依赖 Milvus 版本与 ``norm_score`` 语义
（代码旧注释假设满分≈0.875，实测"提问与库内主体名完全相同"也仅 0.662~0.664），
因此判定不能只依赖绝对分数。本模块提供"库内标准名"目录与归一化比较能力，
让"用户明确报出产品名"这一最常见场景可以确定性命中。

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
from app.rag.query.config import ITEM_NAME_CATALOG_TTL_SECONDS
from app.shared.runtime.logger import logger

# 目录缓存：names 为去重后的库内标准名列表；ts 为最近一次加载（含失败）时间
_CACHE: dict[str, object] = {"names": [], "ts": 0.0}

# 单次拉取上限：主体名属低频小集合，给足以避免分页遗漏
_QUERY_LIMIT = 16384


def normalize_item_name(name: object) -> str:
    """归一化主体名用于比较：全角转半角、去除所有空白、统一小写。"""
    if not name:
        return ""
    text = unicodedata.normalize("NFKC", str(name))
    return "".join(text.split()).lower()


def load_item_names(force: bool = False) -> list[str]:
    """加载库内全量主体名（带 TTL 缓存）。任何异常返回空列表。"""
    now = time.time()
    if not force and _CACHE["names"] and (now - float(_CACHE["ts"])) < ITEM_NAME_CATALOG_TTL_SECONDS:
        return list(_CACHE["names"])  # type: ignore[arg-type]

    names: list[str] = []
    try:
        client = milvus_gateway.milvus_client
        collection = milvus_gateway.item_name_collection_name
        if not client.has_collection(collection_name=collection):
            logger.warning(f"主体名目录集合不存在:{collection},精确直通降级为不可用")
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
                name = str((row or {}).get("item_name") or "").strip()
                key = normalize_item_name(name)
                if key and key not in seen:
                    seen.add(key)
                    names.append(name)
            logger.info(f"主体名目录已刷新:{len(names)} 条")
    except Exception as e:  # noqa: BLE001 - 目录属增强能力，异常必须降级而非中断提问
        logger.warning(f"主体名目录加载失败,精确直通降级为不可用:{e}")
        names = []

    _CACHE["names"] = names
    _CACHE["ts"] = now
    return list(names)


def match_catalog_exact(name: object) -> str | None:
    """归一化后在目录中查等价值；命中返回库内标准名，未命中返回 None。"""
    key = normalize_item_name(name)
    if not key:
        return None
    for candidate in load_item_names():
        if normalize_item_name(candidate) == key:
            return candidate
    return None
