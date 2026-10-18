# query模块所有json相关的类型
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints

from app.evolution.schema import CitationModel
from app.rag.query.contracts import Intent, HistoryIntent


# 健康检查的响应json
class HealthResponseSchema(BaseModel):
    code:int=200
    message:str=None
    # 当前静态资源内容指纹：前端据此判断「页面里的脚本是否已过期」，提示用户刷新
    asset_version:str=""

# 查询接口的请求参数json
class QueryRequestSchema(BaseModel):
    # strip_whitespace 先于长度校验：纯空白 query 会被去空后触发 min_length 校验而拒绝
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
    session_id: str | None = Field(default=None, max_length=128)
    is_stream: bool = False
    # 点选主体时的意图原句（用户点选前问过的那句话）。
    # 前端把「主体名」放在 query、把「意图」放在这里，服务端拼出完整问句，
    # 避免模型把意图改写成历史对话里的话题。
    rewrite_hint: Annotated[
        str | None, StringConstraints(strip_whitespace=True, max_length=4096)
    ] = None

# 查询模块流式响应结果
class QueryStreamResponseSchema(BaseModel):
    message:str
    session_id:str
    turn_id: str = ""

# 进行中查询的进度快照（页面刷新后据此重建「阶段进度」并重新接管 SSE）
class QueryTaskStatusResponseSchema(BaseModel):
    turn_id: str = ""
    code:int=200
    session_id:str
    # pending / processing / completed / failed；空串表示该会话没有在册任务
    status:str=""
    done_list:list[str]=Field(default_factory=list)
    running_list:list[str]=Field(default_factory=list)
    # 本轮提问原文：刷新后用户消息可能还没落库，前端据此补出提问气泡
    query:str=""
    error_code:str | None=None
    message:str | None=None

class QueryNotStreamResponseSchema(BaseModel):
    message:str
    session_id:str
    answer:str
    done_list:list[str]
    image_urls:list[str]
    # 已识别主体：前端点踩时原样回传，保证 反馈→缺口→候选 的 item_names 贯通
    item_names:list[str] = Field(default_factory=list)
    # 自进化输出
    citations:list[CitationModel] = Field(default_factory=list)
    # 接地性：None 表示「未评估」（证据为空或评估失败），前端显示为「未评估」而非 0%
    groundedness:float | None = None
    retrieval_signals:dict = Field(default_factory=dict)
    # 没确认到主体时的相似主体选项（前端渲染成可点选按钮）
    item_name_options:list[dict] = Field(default_factory=list)
    # 本轮意图："rag"=知识库问答 / "chitchat"=闲聊（前端据此渲染「闲聊」徽标）
    intent: Intent = "rag"
    turn_id: str = ""
    answer_id: str = ""
    business_tag: str = ""
    issue_id: str = ""
    handoff_required: bool = False
    metrics: dict = Field(default_factory=dict)

# 会话记录
class HistoryClearResponseSchema(BaseModel):
    message:str
    deleted_count:int

class HistoryItemResponseSchema(BaseModel):
    business_tag: str = ""
    issue_id: str = ""
    handoff_required: bool = False
    id:str
    session_id:str
    role:str
    text:str
    rewritten_query:str = None
    item_names:list[str]=Field(description="关联的item_name", default_factory=list)
    image_urls:list[str]=Field(description="关联的图片地址", default_factory=list)
    citations:list[CitationModel]=Field(description="引用来源", default_factory=list)
    groundedness:float | None=None
    # 本轮意图标记："chitchat"=闲聊（前端渲染「闲聊」徽标）；空串为闲聊功能上线前的历史消息
    intent: HistoryIntent = ""
    turn_id: str = ""
    ts:Any

class HistoryListResponseSchema(BaseModel):
    session_id:str
    items:list[HistoryItemResponseSchema] =Field(description="查询的数据记录列表", default_factory=list)
