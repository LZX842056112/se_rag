from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser

from app.process.query.agent.state import QueryGraphState
from app.rag.query.config import RERANK_MAX_INPUT_TOKENS, RERANK_SUMMARY_CHAR_RATIO, RERANK_MIN_SUMMARY_CHARS, \
    RERANK_MAX_TOPK, RERANK_MIN_TOPK, RERANK_GAP_ABS, RERANK_GAP_RATIO, get_rerank_topk
from app.shared.runtime.load_prompt import load_prompt
from app.shared.runtime.logger import logger, step_log
from app.infra.llm.providers import llm_providers

#   1.获取并且校验参数(state) rewritten_query  rrf_chunks  web_search_docs
#            2.数据格式化处理   rrf_chunks {chunk_id,title,parent_title,part,file_title,content,item_name,score,type,url}
#                             web_search_docs {snippt , title,url}
#                             -> 一种格式 -> 给模型了
#                没有    / chunk_id -> 数据库的有的标识
#                snippet / content -> text : 回答参考内容
#                title  / title   -> title : 标题
#                没有    / score   -> score : web 0  milvus rrf的分 -> reranker打分
#                没有    / type    -> mcp web  数据库 milvus
#                url    /  没用    -> url [图片地址]
#                reranker_list
#            3.组装问题和答案的列表(rewritten_query,reranker_list) -> question_answer_pair_list [[],[],[]]
#                获取问题
#                判断问题token的长度
#                循环reranker_list获取答案 text
#                   判断text的长度
#                     超
#                        模型压缩 -> 调用..
#                   装数据pair
#                返回结果
#            4. reranker模型打分+排序
#                reranker.compute_score([question_answer_pair_list [问题,答案]]) -> [scores]
#                [scores] -> reranker_list { score : x }  -> zip
#                sort排序
#                reranker_list {text 没有压缩} -> 分数 + 排序
#            5. 动态topk截取数据
#                reranker_list  min  max  topk
#                  1 2 3 4 5 6 7 8
#                    断崖值 0.3
@step_log("_require_rerank_inputs")
def _require_rerank_inputs(state):
    #1.获取参数
    rewritten_query = state.get("rewritten_query")
    rrf_chunks = state.get("rrf_chunks",[])
    web_search_docs = state.get("web_search_docs",[])
    #2.非空判断：rewritten_query / rrf_chunks 为硬依赖；网络检索结果允许为空（联网失败时降级为纯本地召回）
    if not rewritten_query or len(rrf_chunks) == 0:
        logger.error(f"rewritten_query或者rrf_chunks为空,业务无法继续进行,提前终止!")
        raise ValueError(f"rewritten_query或者rrf_chunks为空,业务无法继续进行,提前终止!")
    if len(web_search_docs) == 0:
        logger.warning("web_search_docs为空,本次仅使用本地召回结果参与重排")
    return rewritten_query,rrf_chunks,web_search_docs

@step_log("deal_rrf_and_web_result")
def deal_rrf_and_web_result(rrf_chunks, web_search_docs):
    # 1. 定义一个列表
    reranker_docs = []
    # 2. 先循环rrf
    for chunk in rrf_chunks:
        reranker_docs.append({
            "chunk_id":chunk.get("chunk_id"),
            "text":chunk.get("content"),
            "title":chunk.get("title"),
            "score":0, # rrf分 -> reranker打的分
            "type":"milvus",
            "source":chunk.get("source"), # 保留来源标志(milvus/evolution)供自进化引用回填
            "url":None
        })
    # 3. 再循环web_search
    for doc in web_search_docs:
        reranker_docs.append({
            "chunk_id": None,
            "text": doc.get("snippet"),
            "title": doc.get("title"),
            "score": 0,  # rrf分 -> reranker打的分
            "type": "web",
            "url": doc.get("url")
        })

    return reranker_docs

@step_log("create_question_answer_list")
def create_question_answer_list(rewritten_query, reranker_docs):
    question_answer_pair_list = []
    # 1. 获取rewritten_query并且计算token数量
    reranker_model =  llm_providers.reranker_model()
    tokenizer =  reranker_model.tokenizer
    # 算的时候,只需要算我这个字符占有token列表,不用考虑前后的特殊标识
    rewritten_query_tokens_list =  tokenizer.encode(rewritten_query,add_special_tokens=False)
    rewritten_query_token_len = len(rewritten_query_tokens_list)
    # 2. 循环reranker_docs获取每个text答案
    for doc in reranker_docs:
        # 3. 答案的长度判读
        answer = doc.get("text") # 答案
        answer_token_len = len(tokenizer.encode(answer,add_special_tokens=False))
        # 4. 超长了调用模型进行压缩
        # reranker固定4个分割符号
        if rewritten_query_token_len + answer_token_len + 4 > RERANK_MAX_INPUT_TOKENS:
            # 调用模型进行压缩
            # limit = 答案的token / 1.3 -> int -> 50 max
            limit = max(
                RERANK_MIN_SUMMARY_CHARS,
                int((RERANK_MAX_INPUT_TOKENS - 4 - rewritten_query_token_len) / RERANK_SUMMARY_CHAR_RATIO))
            # 加载提示词
            rerank_text_refine_str =  load_prompt("rerank_text_refine",question=rewritten_query,answer=answer,limit=limit)
            # 封装message
            messages = [
                HumanMessage(
                    content=rerank_text_refine_str
                )
            ]
            # 封装调用链
            chains = llm_providers.chat() | StrOutputParser()
            # 执行获取结果
            answer = chains.invoke(messages)
        # 5. 答案一定处理过了
        # question_answer_pair_list answer -> 可能被压缩 -> 只用于打分
        question_answer_pair_list.append([rewritten_query,answer])
    # 6. 返回结果
    return question_answer_pair_list


@step_log("use_reranker_deal_score")
def use_reranker_deal_score(question_answer_pair_list, reranker_docs):
    # 1. 调用reranker打分
    reranker_model = llm_providers.reranker_model()
    # normalize=True 归一化 避免负分  0 - 1分之间
    scores_list =  reranker_model.compute_score(question_answer_pair_list,normalize=True)
    # scores_list == question_answer_pair_list == reranker_docs {score}
    # 2. 同步遍历 分 -> reranker_docs
    for score , doc in zip(scores_list,reranker_docs):
        doc["score"] = score
    # 3. 倒序排序
    reranker_docs.sort(key=lambda x : x.get("score",0),reverse=True)

@step_log("dyn_limit_reranker_docs")
def dyn_limit_reranker_docs(reranker_docs):
    """
      动态结果截取! topk个
         RERANK_MAX_TOPK: int = 5  -> 最多10个
         RERANK_MIN_TOPK: int = 2  -> 最少2个
         RERANK_GAP_RATIO: float = 20% -> 断崖百分比  ->  1 - 2 / 1     0.3 0.2 -> (0.3 - 0.2) / 0.3 = 33%
         RERANK_GAP_ABS: float = 0.2   -> 断崖分差值  ->  0.8  ->  0.5  跳过  大 多了 影响准确了  小  少了 召回率
      topk -> ???
    :param reranker_docs:
    :return:
    """
    # 累计断崖：以重排峰值(第 1 名)作基准，逐项累计与其的相对下跌，达到阈值即截断。
    # 相比仅“相邻两指针对比”，它能捕捉相邻分差细小、但相对头部已明显下滑的慢坡，
    # 避免把头尾质量接近的低分长尾一并保留进作答上下文。
    top_max: int = min(get_rerank_topk(), len(reranker_docs))
    top_min: int = RERANK_MIN_TOPK
    gap_abs: float = RERANK_GAP_ABS      # 0.2 绝对分差阈值
    gap_ratio: float = RERANK_GAP_RATIO  # 0.2 相对比例阈值

    # 缺省截断数取满 top_max；前 top_min 个作为召回下限无条件保留
    topk: int = top_max

    # 已见到的最高分（累计基准）。重排后按分数倒序，故初始通常即为 docs[0]
    running_max = reranker_docs[0].get("score", 0.0)

    # 从 top_min 起向后累计判断：分数未创新高且相对峰值累计下跌达到阈值 → 在此截断
    for i in range(top_min, top_max):
        score = reranker_docs[i].get("score", 0.0)
        if score > running_max:            # 分数回升，刷新峰值基准
            running_max = score
            continue
        abs_score = running_max - score    # 相对峰值的累计下跌
        ratio = abs_score / running_max if running_max else 0.0
        if abs_score > gap_abs or ratio > gap_ratio:
            topk = i
            break
    # 截取前 topk 个
    return reranker_docs[:topk]


@step_log("rerank_documents")
def rerank_documents(state: QueryGraphState) -> QueryGraphState:
    """
    重排序服务：
    1. 合并 RRF 和 Web Search 的文档
    2. 使用 BGE Reranker 模型计算相关性得分
    3. 根据得分动态截断，智能截取 TopK
    4. 回写 reranked_docs
    """
    # 1.获取并且校验参数(state) rewritten_query  rrf_chunks  web_search_docs
    rewritten_query,rrf_chunks,web_search_docs = _require_rerank_inputs(state)
    # 2. 数据格式化处理
    reranker_docs = deal_rrf_and_web_result(rrf_chunks,web_search_docs)
    # 3. 组装问题和答案的列表(rewritten_query,reranker_list) -> question_answer_pair_list [[],[],[]]
    question_answer_pair_list:list[list[str]] = create_question_answer_list(rewritten_query,reranker_docs)
    # 4. 打分+排序
    logger.info(f"排序和打分之前的数据:{reranker_docs}")
    use_reranker_deal_score(question_answer_pair_list,reranker_docs)
    logger.info(f"排序和打分之后的数据:{reranker_docs}")
    # 5. 动态截取数据
    reranker_docs = dyn_limit_reranker_docs(reranker_docs)
    # 6. 更新state
    state["reranked_docs"] = reranker_docs
    return state