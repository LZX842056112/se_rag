"""主体名检索与判定原语（导入端 / 查询端共用）。

- ``search_by_item_names``：对主体名做 BGE-M3 混合检索，取 top-N 候选；
- ``select_item_names``：按「top1 下限 + top1/top2 间距」判定"确认 / 可选 / 丢弃"；
- ``resolve_existing_item_name``：写入端入口 —— 新识别的主体名若与库内某实体同一，
  则复用库内标准名，从源头避免近重复主体名（读取端的 ``expand_item_names`` 只兜底存量）。
"""
from __future__ import annotations

from app.rag.item_name.catalog import find_names_mentioned_in, find_similar_names, load_item_names, match_catalog_name
from app.rag.item_name.config import (
    ITEM_NAME_CONFIRM_MARGIN,
    ITEM_NAME_CONFIRM_MIN_SCORE,
    ITEM_NAME_DENSE_METRIC,
    ITEM_NAME_OPTION_MIN_SCORE,
    ITEM_NAME_SEARCH_LIMIT,
)
from app.shared.clients.milvus_gateway import milvus_gateway
from app.shared.models import llm_providers
from app.shared.runtime.logger import logger, step_log
from app.shared.utils.text import is_same_entity, normalize_item_name

# 相似兜底最多给几个选项（用点选而不是让用户重新组织语言）
SIMILAR_OPTION_LIMIT = 3
# 目录条目很少时（小知识库），直接把目录作为选项列出
CATALOG_SUGGEST_MAX = 5


def _similar_fallback(item_name: str, ranked: list[dict]) -> list[dict]:
    """没到可选阈值时的相似主体兜底（目录相似 + 低分向量候选）。

    事故背景：用户输入 ``hak180``（型号前缀）时，主体向量分只有 0.409，低于可选阈值 0.60，
    旧实现直接丢弃候选、只回一句「请您明确主体再提问」；其实库里只有一个
    ``Brother HAK 180 烫金机``，应当直接列出来让用户点选。
    """
    options: list[dict] = []
    seen_keys: set[str] = set()

    def _add(name: str, score: float | None, matched_by: str) -> None:
        key = normalize_item_name(name)
        if not key or key in seen_keys:
            return
        seen_keys.add(key)
        options.append({"item_name": name, "score": score, "matched_by": matched_by})

    for name, how in find_similar_names(item_name, limit=SIMILAR_OPTION_LIMIT):
        _add(name, None, how)

    for hit in ranked or []:
        if len(options) >= SIMILAR_OPTION_LIMIT:
            break
        _add(str(hit.get("item_name") or ""), hit.get("score"), "vector_low_score")

    # 小知识库：连相似都算不上时，直接把目录列出来，避免只给一句“请明确主体”
    if not options:
        catalog = load_item_names()
        if 0 < len(catalog) <= CATALOG_SUGGEST_MAX:
            for name in catalog:
                _add(name, None, "catalog_all")

    return options[:SIMILAR_OPTION_LIMIT]


def similar_from_query(*texts: str) -> list[dict]:
    """模型没抽出主体时，**用问句本身**在目录里找相似主体供点选。

    事故背景：用户问「烫金机怎么安装」时模型返回空 ``item_names``，链路因此完全跳过目录匹配，
    只回一句「也没有找到相似主体」；而同一界面上问 ``hak180`` 却能给出选项——
    对用户来说就是「有的显示有的不显示」。

    兜底顺序：主体名相似（子串 / token 前缀）→ 问句里出现库内关键词 → 小知识库直接列目录。
    """
    options: list[dict] = []
    seen: set[str] = set()

    def _add(name: str, matched_by: str) -> None:
        key = normalize_item_name(name)
        if not key or key in seen:
            return
        seen.add(key)
        options.append({"item_name": name, "score": None, "matched_by": matched_by})

    wanted = [str(text or "").strip() for text in texts]
    for text in wanted:
        if not text:
            continue
        for name, how in find_similar_names(text, limit=SIMILAR_OPTION_LIMIT):
            _add(name, how)
        if options:
            return options[:SIMILAR_OPTION_LIMIT]

    for text in wanted:
        if not text:
            continue
        for name, how in find_names_mentioned_in(text, limit=SIMILAR_OPTION_LIMIT):
            _add(name, how)
        if options:
            return options[:SIMILAR_OPTION_LIMIT]

    catalog = load_item_names()
    if 0 < len(catalog) <= CATALOG_SUGGEST_MAX:
        for name in catalog:
            _add(name, "catalog_all")
    return options[:SIMILAR_OPTION_LIMIT]


@step_log("search_by_item_names")
def search_by_item_names(item_names: list[str]) -> dict[str, list[dict]]:
    """
       进行向量数据库搜索
    :param item_names:
    :return:
    """
    # 准备一个最终的字典
    final_result = {}
    # 1.循环llm查询到item_names的列表 -> item_name
    # item_names_vector = {dense:[[],[]],sparse:[{},{}]}
    # 先批量生成向量!
    item_names_vector = llm_providers.generate_embeddings(item_names)
    if not item_names_vector:
        # 嵌入生成失败（LLM 服务异常返回 None）：降级为空检索，避免 'NoneType' 下标抛 500
        return final_result
    for index in range(0, len(item_names)):
        # 2.获取稠密和稀疏向量
        item_name = item_names[index]
        item_name_dense = item_names_vector['dense'][index]
        item_name_sparse = item_names_vector['sparse'][index]
        # 3.稀疏和稠密向量进行混合检索
        # 3.1 创建annSearchRequest -> 2 -> []
        # dense 检索的 metric_type 必须与 kb_item_names 实际索引一致（该集合为 HNSW/COSINE），
        # 否则 Milvus 会报 "metric type not match" 并让检索静默失败（网关返回 None）。
        # sparse 保持 IP（与集合 sparse 索引一致）。
        reqs = milvus_gateway.create_requests(
            item_name_dense,
            item_name_sparse,
            dense_params={"metric_type": ITEM_NAME_DENSE_METRIC},
            limit=max(ITEM_NAME_SEARCH_LIMIT * 2, 10),
        )
        # 3.2 创建WeightReranker排序器
        # 3.3 进行混合检索
        #  稠密 满分 1
        #  稀疏 满分 0.6
        #  0.5 0.5  = 0.75 - 0.8      0.73 -> 0.7   0.78  0.75
        results = milvus_gateway.hybrid_search(
            collection_name=milvus_gateway.item_name_collection_name,
            reqs=reqs,  # [1,2]
            ranker_weights=(0.5, 0.5),
            norm_score=True,
            limit=ITEM_NAME_SEARCH_LIMIT,  # 显式取 top-N：相对判定需要 top2（不同主体）才能算间距
            output_fields=['item_name']
        )
        # results = [[{id:主键,distance:0.9,entity:{item_name:具体的name}},{...}]]  保证对称性 单列检索和混合检索的返回结果一致
        # 混合检索失败（集合不存在/metric 不匹配等）时网关返回 None：跳过该项目，避免 NoneType 下标抛 500
        if not results:
            final_result[item_name] = []
            continue
        # 4.处理混合检索的结果
        item_name_milvus_list = []
        if len(results[0]) > 0:
            for item in results[0]:
                # {id:主键,distance:0.9,entity:{item_name:具体的name}}
                item_name_milvus_list.append({
                    "item_name": item.get('entity').get('item_name'), "score": item.get('distance')
                })
            # {item_name:分,item_name:分...5个}
        # 5.循环完以后得结果最终返回即可
        final_result[item_name] = item_name_milvus_list
    return final_result


def best_other_score(ranked: list[dict], anchor_name: str) -> float | None:
    """取"非锚点主体"中的最高分，用于计算 top1 与次优主体（不同 item_name）的间距。

    关键：对"同一实体的不同写法"视为同一主体，不参与"次优主体"计分。判据见
    ``catalog.is_same_entity``：归一化相等，或"短名是长名的后缀"（品牌前缀差异，
    如 `brotherhak180烫金机` ⊃ `hak180烫金机`）。
    否则这类词条的向量分几乎相同，间距恒为 0，导致永远无法自动确认、用户手动确认也消解不了。
    注意：父/子型号（`HAK 180` vs `HAK 180 烫金机`）仍算不同主体，歧义必须反问。
    """
    best: float | None = None
    for hit in ranked:
        if is_same_entity(hit.get("item_name"), anchor_name):
            # 与锚点同一实体（含近重复写法）：视为同一主体，跳过
            continue
        score = float(hit.get("score") or 0.0)
        if best is None or score > best:
            best = score
    return best


@step_log("select_item_names")
def select_item_names(milvus_result: dict[str, list[dict]]) -> dict[str, list]:
    """
      根据分数,明确确定和可选的item_name列表
    :param milvus_result:
    :return:
    """
    confirmed_list = []
    option_list = []
    similar_list: list[dict] = []

    # 思路: item_name -> [{item_name:"向量数据库中的item_name",score:0.8},...]
    # 循环处理
    for item_name, mivlus_result_list_dict in milvus_result.items():
        # mivlus_result_list_dict = [{item_name:"向量数据库中的item_name",score:0.9 }, 0.85 0.8 ..]
        # 苹果手机和华为手机哪个好用?
        # 苹果手机 : [{},{},{},{},{},{}]  确认 -> 条件筛选 -> 确认1个 -> 分最高的  可选 -> 可能是.. 0.8 - 0.6 都要 topk 2
        # 华为手机 : [{},{},{},{},{},{}]  确认 -> 条件筛选 -> 确认1个 -> 分最高的  可选 -> 可能是.. 0.8 - 0.6 都要 topk 2
        # [{item_name:"向量数据库中的item_name",score:0.8},..] 数据是已经排好顺序的! 分高 前面!
        # confirmed_list = [] -> 啥样的算确认  [ 稠密向量满分 1 * 0.5 + 稀疏向量满分 0.75  0.5 ] = 0.5 + 0.375 = 0.875 -> 0.8 + 确认
        # option_list = [] -> 算可选的 -> 0.6 - 0.8 -> 可选
        # hits 已按分数降序（网关保证），这里再排一次以消除上游顺序假设
        ranked = sorted(
            [hit for hit in (mivlus_result_list_dict or []) if hit.get("item_name")],
            key=lambda hit: float(hit.get("score") or 0.0),
            reverse=True,
        )
        if not ranked:
            # 向量无任何命中：仅当目录命中时直通（集合/索引异常时的兜底）
            hit = match_catalog_name(item_name)
            if hit:
                confirmed_list.append({"item_name": hit[0], "score": None, "matched_by": hit[1]})
                logger.info(f"模型识别item_name:{item_name},向量无命中但目录命中:{hit[0]},直接确认")
            else:
                # 零命中 + 目录未命中：同样要给用户可点选的相似主体，而不是直接丢弃
                fallback = _similar_fallback(item_name, ranked)
                if fallback:
                    similar_list.extend(fallback)
                    logger.info(
                        f"模型识别item_name:{item_name},向量零命中,"
                        f"给出相似主体供点选:{[h.get('item_name') for h in fallback]}"
                    )
                else:
                    logger.info(f"模型识别item_name:{item_name},向量零命中且目录无相似主体")
            continue

        # 锚点主体：库内标准名（归一化精确同名 / 同一实体）优先于向量排名结果
        hit = match_catalog_name(item_name)
        exact_name = hit[0] if hit else None
        anchor = next((hit_row for hit_row in ranked if hit_row.get("item_name") == exact_name), None) if exact_name else None
        anchor_is_exact = anchor is not None
        if anchor is None:
            anchor = ranked[0]
        anchor_name = anchor.get("item_name")
        anchor_score = float(anchor.get("score") or 0.0)
        other_score = best_other_score(ranked, anchor_name)
        # 只有一个候选主体时视为无歧义
        margin = float("inf") if other_score is None else round(anchor_score - other_score, 4)

        # 1) 目录命中：名字完全一致本身即强证据（摆脱分数量级依赖），
        #    但仍要求间距达标，以避开"父型号/子型号"歧义（如 HAK 180 vs HAK 180 烫金机）
        if anchor_is_exact and margin >= ITEM_NAME_CONFIRM_MARGIN:
            confirmed_list.append({**anchor, "matched_by": hit[1]})
            logger.info(f"模型识别item_name:{item_name},目录命中:{anchor_name},间距={margin},直接确认")
            continue

        # 2) 相对判定：top1 达下限 且 与次优主体间距达标
        if anchor_score >= ITEM_NAME_CONFIRM_MIN_SCORE and margin >= ITEM_NAME_CONFIRM_MARGIN:
            confirmed_list.append({**anchor, "matched_by": "relative"})
            logger.info(f"模型识别item_name:{item_name},相对判定确认:{anchor_name},分={anchor_score},间距={margin}")
            continue

        # 3) 可选区间：不自动确认，交给用户二次确认（反问）
        #    按归一化名去重，避免把"仅空格差异"的近重复名同时列出，让用户无法区分、形成死循环
        option_hits = []
        seen_key: set[str] = set()
        for hit in ranked:
            if float(hit.get("score") or 0.0) < ITEM_NAME_OPTION_MIN_SCORE:
                break  # 已按分数降序，后续都不达标
            key = normalize_item_name(hit.get("item_name"))
            if key in seen_key:
                continue
            seen_key.add(key)
            option_hits.append(hit)
        if option_hits:
            option_list.extend(option_hits[:2])
            logger.info(
                f"模型识别item_name:{item_name},未确认(分={anchor_score},间距={margin}),"
                f"但是有可选的:{','.join([str(hit.get('item_name')) for hit in option_hits[:2]])}")
            continue

        # 4) 相似兜底：给不出确认/可选时，列出相似主体让用户点选（不再直接丢弃）
        fallback = _similar_fallback(item_name, ranked)
        if fallback:
            similar_list.extend(fallback)
            logger.info(
                f"模型识别item_name:{item_name},未达阈值(分={anchor_score}),"
                f"给出相似主体供点选:{[h.get('item_name') for h in fallback]}"
            )
            continue
        logger.info(f"模型识别item_name:{item_name},无任何候选(分={anchor_score},间距={margin}),丢弃")

    return {
        "confirmed_list": confirmed_list,
        "option_list": option_list,
        "similar_list": similar_list,
    }


def resolve_existing_item_name(raw_name: str) -> tuple[str, dict | None]:
    """写入端主体归并：把新识别的主体名对齐到库内已存在的同一实体。

    命中则返回库内标准名与判定证据；未命中返回原名与 None。
    与读取端共用同一套阈值/间距口径，保证"写入怎么归并"与"读取怎么确认"一致。
    """
    name = str(raw_name or "").strip()
    if not name:
        return raw_name, None
    # 1. 目录命中（归一化精确同名或同一实体）：直接复用，省一次向量检索
    hit = match_catalog_name(name)
    if hit:
        return hit[0], {"item_name": hit[0], "score": None, "matched_by": hit[1]}
    # 2. 向量检索 + 相对判定
    confirmed = select_item_names(search_by_item_names([name])).get("confirmed_list") or []
    if not confirmed:
        return name, None
    resolved = confirmed[0]
    return str(resolved.get("item_name") or name), resolved
