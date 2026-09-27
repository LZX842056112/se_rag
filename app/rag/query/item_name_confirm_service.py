from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import JsonOutputParser

from app.process.query.agent.state import QueryGraphState
from app.shared.runtime.load_prompt import load_prompt
from app.shared.runtime.logger import logger, step_log
from app.infra.persistence.history_repository import history_repository
from app.infra.llm.providers import llm_providers
from app.rag.item_name.match import search_by_item_names, select_item_names
from app.rag.query.history_utils import build_history_context


@step_log("_require_session_and_query")
def _require_session_and_query(state:QueryGraphState) -> tuple[str,str]:
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
    5. 同步历史记录到 MongoDB
    """
    # 1
    session_id, original_query = _require_session_and_query(state)
    # 2 获取历史(有效)信息和拼接上下文
    history_text = get_history_messages_and_context(session_id)
    # 3. 调用模型识别item_names和重写的问题
    result:dict =call_llm_item_name_and_rewritten(history_text,original_query)
    list_dict = {}
    if len(result.get("item_names",[])) > 0:
        # 4. 进行向量数据库的搜索 llm - 分析 -> item_names -> milvus中进行查询 -> 打分
        milvus_result:dict[str,list[dict]] = search_by_item_names(result.get("item_names",[]))
        # 5. 根据打分确定两个列表 确认列表 可选列表
        list_dict:dict[str,list] =select_item_names(milvus_result)
    #6. 确定和可选列表修改state answer item_names rewritten_query
    apply_item_name_result(state,list_dict,result.get("rewritten_query"))
    #7. 记录提问的聊天记录
    save_history_message(state)
    return state
