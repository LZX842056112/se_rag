import asyncio
import json

from agents.mcp import MCPServerStreamableHttp

from app.process.query.agent.state import QueryGraphState
from app.shared.runtime.logger import logger
from app.infra.config.providers import infra_config


def _require_rewritten_query(state) -> str:
    rewritten_query = state.get("rewritten_query")
    if not rewritten_query:
        logger.error(f"重写的问题为空,业务无法继续,提前终止!")
        raise ValueError(f"重写的问题为空,业务无法继续,提前终止!")
    return rewritten_query

async def open_ai_mcp(rewritten_query):

    # 1. 链接mcp服务
    mcp_server = MCPServerStreamableHttp(
        name="Streamable HTTP Python Server",
        params={
            "url": infra_config.mcp_config.mcp_base_url,
            "headers": {"Authorization": f"Bearer {infra_config.mcp_config.api_key}"},
            "timeout": 50,
        },
        cache_tools_list=True,
        max_retry_attempts=3,
    )
    try:
        # 2. mcp服务链接
        await mcp_server.connect()
        # 3. mcp工具调用
        mcp_result = await mcp_server.call_tool(
            tool_name="bailian_web_search",
            arguments={
                "query":rewritten_query,
                "count":5
            }
        )
        return mcp_result
    except Exception as e:
        logger.exception(f"mcp调用发生问题,问题:{str(e)}")
        return None
    finally:
        # 4. 清空链接
        await mcp_server.cleanup()

def search_by_web(state: QueryGraphState) -> QueryGraphState:
    """
    网络搜索服务：
    1. 通过 MCP 协议异步调用百炼联网搜索接口
    2. 将用户的查询转化为实时的、结构化的网络搜索结果
    3. 包含标题、链接和摘要
    4. 回写 web_search_docs
    """
    #3. 获取并校验参数(state) -> rewritten_query
    rewritten_query = _require_rewritten_query(state)
    # 4. async 使用openai提供mcp方式进行调用(rewritten_query) -> 查询结果
    mcp_result = asyncio.run(open_ai_mcp(rewritten_query))
    # 5. 联网失败降级：不抛异常，返回空列表，交由重排阶段仅用本地召回结果
    if mcp_result is None or not mcp_result.content:
        logger.warning("联网搜索未返回有效结果,本次仅使用本地召回结果")
        return []
    # 6. 结果解析：注意 mcp 返回对象的字段是属性，不是字典
    text = mcp_result.content[0].text
    # {pages:[{snippet,title,url},{},{}]}
    text_dict = json.loads(text)
    web_search_docs = text_dict.get("pages",[])
    #7. 返回列表即可
    return web_search_docs