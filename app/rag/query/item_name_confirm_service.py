from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import JsonOutputParser

from app.process.query.agent.state import QueryGraphState
from app.shared.runtime.load_prompt import load_prompt
from app.shared.runtime.logger import logger, step_log
from app.infra.persistence.history_repository import history_repository
from app.infra.llm.providers import llm_providers
from app.infra.vector_store.milvus_gateway import milvus_gateway
from app.rag.query.config import (
    ITEM_NAME_CONFIRM_MARGIN,
    ITEM_NAME_CONFIRM_MIN_SCORE,
    ITEM_NAME_DENSE_METRIC,
    ITEM_NAME_OPTION_MIN_SCORE,
    ITEM_NAME_SEARCH_LIMIT,
)
from app.rag.query.history_utils import build_history_context
from app.rag.query.item_name_catalog import match_catalog_exact, normalize_item_name


@step_log("get_data_and_validates")
def get_data_and_validates(state:QueryGraphState) -> tuple[str,str]:
    """
    参数校验
    :param state:
    :return:
    """
    # 1. 获取数据
    session_id = state.get("session_id")
    original_query = state.get("original_query")
    # 2. 进行数据校验
    if not session_id or not original_query:
        logger.error(f"session_id或者original_query未空,业务无法继续进行,提前终止!")
        raise ValueError(f"session_id或者original_query未空,业务无法继续进行,提前终止!")
    # 3. 返回结果
    return session_id,original_query

@step_log("get_history_messages_and_context")
def get_history_messages_and_context(session_id:str) -> str:
    """
    获取近期有效的历史聊天记录并拼接成上下文提示词片段。
    """
    return build_history_context(session_id, limit=10)

@step_log("call_llm_item_name_and_rewritten")
def call_llm_item_name_and_rewritten(history_text:str, original_query:str)->dict:
    """
    调用模型进行识别
    :param history_text: 
    :param original_query: 
    :return: 
    """
    #1. 加载模型对象
    json_llm_client = llm_providers.chat(json_mode=True)
    #2. 加载提示词
    # 提示词修改
    # 提示词 1. history靠上! 影响了模型对规则读取 历史对话向下挪
    #       2. 不是每次提问都是延续的! 上一次 烫金机  本次  苹果手机
    history_prompt_text = load_prompt("rewritten_query_and_itemnames",query=original_query,history_text=history_text)
    #3. 包装提示词对象
    history_prompt_messages = [
        HumanMessage(
            content=history_prompt_text
        )
    ]
    #4. 创建调用链
    chains = json_llm_client | JsonOutputParser()
    #5. 调用获取结果
    result_dict = chains.invoke(history_prompt_messages)
    #6. 参数校验赋予默认值
    if "item_names" not in result_dict:
        result_dict["item_names"] = []
    if "rewritten_query" not in result_dict:
        result_dict["rewritten_query"] = original_query
    #7. 返回结果
    return result_dict

@step_log("search_by_item_names")
def search_by_item_names(item_names:list[str]) -> dict[str,list[dict]]:
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
    for index in range(0,len(item_names)):
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
        results=  milvus_gateway.hybrid_search(
            collection_name=milvus_gateway.item_name_collection_name,
            reqs=reqs, # [1,2]
            ranker_weights=(0.5,0.5),
            norm_score=True,
            limit=ITEM_NAME_SEARCH_LIMIT,  # 显式取 top-N：相对判定需要 top2（不同主体）才能算间距
            output_fields=['item_name']
        )
        # results = [[{id:主键,distance:0.9,entity:{item_name:具体的name}},{id:主键,distance:0.9,entity:{item_name:具体的name}},{id:主键,distance:0.9,entity:{item_name:具体的name}}]]  保证对称性 单列检索和混合检索的返回结果一致
        # 混合检索失败（集合不存在/metric 不匹配等）时网关返回 None：跳过该项目，避免 NoneType 下标抛 500
        if not results:
            final_result[item_name] = []
            continue
        # 4.处理混合检索的结果
        item_name_milvus_list =[]
        if len(results[0]) > 0 :
            for item in results[0]:
                # {id:主键,distance:0.9,entity:{item_name:具体的name}}
                item_name_milvus_list.append({
                    "item_name":item.get('entity').get('item_name') ,"score":item.get('distance')
                })
            # {item_name:分,item_name:分...5个}
        # 5.循环完以后得结果最终返回即可
        final_result[item_name] = item_name_milvus_list
    return final_result

def _best_other_score(ranked: list[dict], anchor_name: str) -> float | None:
    """取"非锚点主体"中的最高分，用于计算 top1 与次优主体（不同 item_name）的间距。

    关键：对"归一化后等价"的近重复名（如 `HAK 180 烫金机` 与 `HAK 180烫金机`，
    仅空格差异）视为同一主体，不参与"次优主体"计分。
    否则这类词条的向量分几乎相同，间距恒为 0，导致永远无法自动确认，用户手动确认也消解不了（死循环）。
    """
    anchor_key = normalize_item_name(anchor_name)
    best: float | None = None
    for hit in ranked:
        if normalize_item_name(hit.get("item_name")) == anchor_key:
            # 与锚点等价（含近重复词条）：视为同一主体，跳过
            continue
        score = float(hit.get("score") or 0.0)
        if best is None or score > best:
            best = score
    return best


@step_log("select_item_names")
def select_item_names(milvus_result:dict[str,list[dict]]) -> dict[str,list]:
    """
      根据分数,明确确定和可选的item_name列表
    :param milvus_result:
    :return:
    """
    confirmed_list = []
    option_list = []

    # 思路: item_name -> [{item_name:"向量数据库中的item_name",score:0.8},{item_name:"向量数据库中的item_name",score:0.8},{item_name:"向量数据库中的item_name",score:0.8}]
    # 循环处理
    for item_name, mivlus_result_list_dict  in  milvus_result.items():
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
            # 向量无任何命中：仅当目录精确命中时直通（集合/索引异常时的兜底）
            exact_name = match_catalog_exact(item_name)
            if exact_name:
                confirmed_list.append({"item_name": exact_name, "score": None, "matched_by": "catalog_exact"})
                logger.info(f"模型识别item_name:{item_name},向量无命中但目录精确命中:{exact_name},直接确认")
            continue

        # 锚点主体：库内标准名（归一化精确同名）优先于向量排名结果
        exact_name = match_catalog_exact(item_name)
        anchor = next((hit for hit in ranked if hit.get("item_name") == exact_name), None) if exact_name else None
        anchor_is_exact = anchor is not None
        if anchor is None:
            anchor = ranked[0]
        anchor_name = anchor.get("item_name")
        anchor_score = float(anchor.get("score") or 0.0)
        other_score = _best_other_score(ranked, anchor_name)
        # 只有一个候选主体时视为无歧义
        margin = float("inf") if other_score is None else round(anchor_score - other_score, 4)

        # 1) 目录精确直通：名字完全一致本身即强证据（摆脱分数量级依赖），
        #    但仍要求间距达标，以避开"父型号/子型号"歧义（如 HAK 180 vs HAK 180 烫金机）
        if anchor_is_exact and margin >= ITEM_NAME_CONFIRM_MARGIN:
            confirmed_list.append({**anchor, "matched_by": "catalog_exact"})
            logger.info(f"模型识别item_name:{item_name},目录精确命中:{anchor_name},间距={margin},直接确认")
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

        # 4) 其余丢弃
        logger.info(f"模型识别item_name:{item_name},无任何候选(分={anchor_score},间距={margin}),丢弃")

    return {
        "confirmed_list":confirmed_list,
        "option_list":option_list
    }

@step_log("apply_item_name_result")
def apply_item_name_result(state, list_dict:dict[str,list], rewritten_query:str):
    """
      本次就是为了state
         confirmed_list ->  有数据
           item_names = []
           rewritten_query = ""
           一定不能给 answer del ...
           return
         option_list ->confirmed_list没有,  有数据
           item_names = 候选主体列表（P1 修复：写入本轮 state -> 落库历史，供下一轮消解）
           rewritten_query = LLM 改写结果
           answer = 本次提问没有确认主体,但是有相似可选的: 1,2,3,4 请您再次确认!
           return
         confirmed_list,option_list -> 都没有数据
           item_names 不赋值
           rewritten_query 也无需赋值
           answer = 本次问题没有关联到任何主体,有没有相似可选的主体! 请您明确主体再提问!
           return
    :param state:
    :param list_dict:
    :param rewritten_query:
    :return:
    """
    # [{item_name:xx,score:xxx},]
    confirmed_list = list_dict.get("confirmed_list",[])
    option_list = list_dict.get("option_list",[])

    if len(confirmed_list) > 0:
        state['item_names'] = [item.get('item_name') for item in confirmed_list ]
        state['rewritten_query'] = rewritten_query
        if "answer" in state:
            state["answer"] = None
        return

    if len(option_list) >0:
        # 没有确认,但是有可选的：进入反问。
        # P1 修复：把候选主体与改写后的问题一并写入本轮 state，save_history_message 随之落库，
        # 用户下一轮回答"是X"时才能被消解。原实现只写 answer 不写 item_names，而本轮消息会在
        # get_history_messages_and_context 中被"主体非空"条件过滤掉 -> 历史恒为空 -> 多轮死循环。
        # 注意：路由只判 state["answer"] 是否为空，写入 item_names 不改变本轮回问路由。
        candidate_names: list[str] = []
        for item in option_list:
            name = item.get("item_name")
            if name and name not in candidate_names:
                candidate_names.append(name)
        if candidate_names:
            state["item_names"] = candidate_names
        if rewritten_query:
            state["rewritten_query"] = rewritten_query
        state["answer"] = f"本次提问没有确认主体,但是有相似可选的: {','.join(candidate_names)},请您再次确认!"
        return

    state['answer'] = "本次问题没有关联到任何主体,有没有相似可选的主体! 请您明确主体再提问!"

@step_log("save_history_message")
def save_history_message(state:QueryGraphState):
    history_repository.save_message(
        session_id=state.get("session_id"),
        role="user",  # 用户提问
        text=state.get("original_query"),
        rewritten_query=state.get("rewritten_query",""),
        item_names=state.get("item_names",[]),
        image_urls=[]
    )

@step_log("confirm_item_name")
def confirm_item_name(state: QueryGraphState) -> QueryGraphState:
    """
    意图确认服务：
    1. 结合历史对话提取商品名
    2. 将模糊问题改写为完整独立的精准问题
    3. 在 Milvus 向量库中进行混合搜索
    4. 根据评分高低自动对齐标准型号，或生成反问让用户手动确认
    5. 同步历史记录到 MongoDB  HAK 180 烫金机
    """
    # 1
    session_id, original_query = get_data_and_validates(state)
    # 2 获取历史(有效)信息和拼接上下文
    history_text = get_history_messages_and_context(session_id)
    # 3. 调用模型识别item_names和重写的问题
    result:dict =call_llm_item_name_and_rewritten(history_text,original_query)
    list_dict = {}
    if len(result.get("item_names",[])) > 0:
        # 4. 进行向量数据库的搜索 llm - 分析 -> item_names -> milvus中进行查询 -> 打分
        # [1 -> 关联item_name,2 -> item_name,3 ...,4]
        milvus_result:dict[str,list[dict]] = search_by_item_names(result.get("item_names",[]))
        # 5. 根据打分确定两个列表 确认列表 可选列表
        # 确认列表
        # 可选列表
        # dict{"confirmed_list":[],option_list:[]}
        #   "confirmed_list":confirmed_list,
        #   "option_list":option_list
        list_dict:dict[str,list] =select_item_names(milvus_result)
    #6. 确定和可选列表修改state answer item_names rewritten_query
    # 修改 state answer item_names rewritten_query list_dict
    apply_item_name_result(state,list_dict,result.get("rewritten_query"))
    #7. 记录提问的聊天记录
    save_history_message(state)
    return state