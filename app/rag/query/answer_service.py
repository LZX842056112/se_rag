import re

from langchain_core.messages import HumanMessage

from app.infra.persistence.history_repository import history_repository
from app.process.query.agent.state import QueryGraphState
from app.rag.query.config import SUPPORTED_IMAGE_EXTENSIONS
from app.rag.query.history_utils import build_history_context
from app.evolution.config import evolution_config
from app.evolution.online_eval.grounding import compute_groundedness
from app.evolution.schema import CitationModel
from app.shared.runtime.load_prompt import load_prompt
from app.shared.utils.sse_utils import SSEEvent, push_to_session
from app.shared.runtime.logger import logger
from app.infra.llm.providers import llm_providers


def state_exists_answer(state) -> bool:
    # 1. 判断是否存在
    answer = state.get("answer")
    if not answer:
        # 没有
        # item_name 确定,正常流程
        logger.info(f"answer内容为空,业务正常进行查询的!跳入回答环节!")
        return False
    # 2. 存在 判定是不是流式
    is_stream = state.get("is_stream",False)
    if is_stream:
        # 是流式 现在就要结果给推送队列中
        push_to_session(state.get("session_id"),SSEEvent.DELTA,{"delta":answer})

    logger.info(f"answer内容不为空,前期没有识别出item_name,提前给与回答!")
    return True


def load_answer_prompt(state : QueryGraphState) -> str:
    """
      加载提示词! 用于模型润色answer回答
    :param state:
    :return:
    """
    # question
    question = state.get("rewritten_query")
    # context
    # reranked_docs = [  { chunk_id , title , text , score = reranker模型 , type , url  }  ]
    reranked_docs = state.get("reranked_docs",[])
    context = ""
    #  第1部分,标题:xx,来源:网络或者向量库,置信度: xxx,内容: text \n
    for index,doc in enumerate(reranked_docs,start=1):
        context += (f"第{index}部分,标题:{doc.get('title')},来源:{ '网络搜索' if doc.get('type') == 'web' else '向量库'} ,"
                    f"置信度: {doc.get('score')},内容:{doc.get('text')}\n")
    # item_names
    item_names = f"{','.join(state.get('item_names',[]))}"
    # history（复用统一的历史上下文构建，取 6 条有效记录）
    history_text = build_history_context(state.get("session_id"), limit=6)
    # 加载提示词
    answer_out_str = load_prompt("answer_out",question=question,context=context,item_names=item_names,history=history_text)
    return answer_out_str


def call_llm_deal_answer(state, answer_prompt_text):
    """
      处理answer
    :param state:
    :param answer_prompt_text:
    :return:
    """
    is_stream = state.get("is_stream",False)
    answer = ""
    # 准备模型
    llm_client = llm_providers.chat()
    messages = [HumanMessage(
            content=answer_prompt_text
        )]
    # 流式和非流式
    if is_stream:
        # 流式
        stream = llm_client.stream(messages)
        for chunk in stream:
            # 增量数据 delta -> 队列中
            push_to_session(state.get("session_id"),SSEEvent.DELTA,{"delta":chunk.content})
            answer += chunk.content
    else:
        # 非流
        response =  llm_client.invoke(messages) # AIMessage(content = 结果)
        answer = response.content
    state["answer"] = answer


def extract_text_image_url(state):
    """
    处理图片
       text
       url
    :param state:
    :return:
    """
    # 获取reranked_docs  text url
    reranked_docs = state.get("reranked_docs",[])
    # 定义正则规则
    image_re =  re.compile(r"\!\[.*?\]\((.*?)\)")  # findall
    image_urls = []
    # 循环出来每个doc url | text
    for doc in reranked_docs:
        url:str  = doc.get("url")
        text:str = doc.get("text")
        if url and url.endswith(SUPPORTED_IMAGE_EXTENSIONS):
            image_urls.append(url)
        url_list = image_re.findall(text)
        if url_list and len(url_list) >0:
            image_urls.extend(url_list)
    # image_urls
    state['image_urls'] = image_urls


def save_answer_message_history(state):

    history_repository.save_message(
        session_id=state.get("session_id"),
        role="assistant",
        text=state.get("answer"),
        rewritten_query=state.get("rewritten_query"),
        item_names=state.get("item_names",[]),
        image_urls=state.get("image_urls",[]),
        citations=state.get("citations",[]),
        groundedness=state.get("groundedness",0.0)
    )


def backfill_evolution_outputs(state: QueryGraphState) -> QueryGraphState:
    """回填引用 / 检索信号 / 接地性；仅在自进化开启时计算 groundedness。"""
    reranked_docs = state.get("reranked_docs", [])
    cited: list = []
    evo_ids: list = []
    for doc in reranked_docs:
        cid = doc.get("chunk_id")
        if cid is not None and doc.get("type") != "web":
            cited.append(cid)
        if doc.get("source") == "evolution":
            evo_ids.append(cid)
    state["cited_chunk_ids"] = cited
    state["faq_evo_ids"] = evo_ids
    # 对外引用列表（落库 + 历史回显 + 实时 SSE 共用同一份，保证两路一致）
    evo_set = {str(i) for i in evo_ids}
    state["citations"] = [
        CitationModel(
            faq_id=str(cid),
            source="evolution" if str(cid) in evo_set else "kb",
        ).model_dump()
        for cid in cited
    ]
    state["retrieval_signals"] = {
        "zero_hit": len(reranked_docs) == 0,
        "no_retrieval": not state.get("embedding_chunks") and not state.get("hyde_embedding_chunks"),
        "evolution_hit": bool(evo_ids),
    }
    if getattr(evolution_config, "enabled", False) and reranked_docs:
        evidence = [str(d.get("text") or "") for d in reranked_docs]
        state["groundedness"] = compute_groundedness(state.get("answer", ""), evidence)
    else:
        state["groundedness"] = 0.0
    return state


def generate_answer(state: QueryGraphState) -> QueryGraphState:
    # 1. state中是否存在answer -> 可以返回字符串了
    has_answer:bool = state_exists_answer(state)
    # 2. 判断不存在...
    if not has_answer:
        # 没有answer 模型润色answer 提取image_urls
        # answer_prompt_text =  声明一个提示词拼接的方法(state) 拼接
        answer_prompt_text:str = load_answer_prompt(state)
        # 调用llm模型处理answer字符串的问题
        call_llm_deal_answer(state,answer_prompt_text)
        # 使用正则或者图片url匹配获取image_urls
        extract_text_image_url(state)
    # 3.回填自进化输出（引用 / 信号 / 接地性）
    backfill_evolution_outputs(state)
    # 4.历史聊天记录记录（须在回填之后，保证 citations/groundedness 可持久化）
    save_answer_message_history(state)
    return state