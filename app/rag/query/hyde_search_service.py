from langchain_core.messages import HumanMessage
from langchain_core.output_parsers import StrOutputParser

from app.process.query.agent.state import QueryGraphState
from app.shared.runtime.load_prompt import load_prompt
from app.infra.llm.providers import llm_providers
from app.rag.query.chunk_search import (
    require_query_and_items,
    search_chunks,
    to_chunks,
)


def call_llm_answer(rewritten_query) -> str:
    # 1. 获取模型客户端对象
    llm_client = llm_providers.chat()
    # 2. 加载提示词字符串
    hyde_prompt_text = load_prompt("hyde_prompt", rewritten_query=rewritten_query)
    # 3. 封装提示词Message
    messages = [HumanMessage(content=hyde_prompt_text)]
    # 4. 封装调用链
    chains = llm_client | StrOutputParser()
    # 5. 调用获取结果
    return chains.invoke(messages)


def search_by_hyde(state: QueryGraphState) -> QueryGraphState:
    """
    向量检索服务：
    1. 根据改写后的问题和限定的商品范围
    2. 利用 BGEM3 混合检索（稠密+稀疏）技术
    3. 从 Milvus 向量数据库中召回 Top-K 最相关的知识切片
    4. 回写 embedding_chunks
    """
    # 1. 获取并校验参数(state) -> item_names rewritten_query
    item_names, rewritten_query = require_query_and_items(state)
    # 2. 模型生成假设性答案
    llm_answer = call_llm_answer(rewritten_query)
    # 3. 进行向量的混合+条件检索
    milvus_list = search_chunks(item_names, f"{rewritten_query}:{llm_answer}")
    # 4. 进行结果的统一格式化处理
    return to_chunks(milvus_list)
