# query模块所有json相关的类型
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints

from app.evolution.schema import CitationModel


# 健康检查的响应json
class HealthResponseSchema(BaseModel):
    code:int=200
    message:str=None

# 查询接口的请求参数json
class QueryRequestSchema(BaseModel):
    # strip_whitespace 先于长度校验：纯空白 query 会被去空后触发 min_length 校验而拒绝
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)]
    session_id: str | None = Field(default=None, max_length=128)
    is_stream: bool = False

# 查询模块流式响应结果
class QueryStreamResponseSchema(BaseModel):
    message:str
    session_id:str

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

# 会话记录
class HistoryClearResponseSchema(BaseModel):
    message:str
    deleted_count:int

class HistoryItemResponseSchema(BaseModel):
    id:str
    session_id:str
    role:str
    text:str
    rewritten_query:str = None
    item_names:list[str]=Field(description="关联的item_name", default_factory=list)
    image_urls:list[str]=Field(description="关联的图片地址", default_factory=list)
    citations:list[CitationModel]=Field(description="引用来源", default_factory=list)
    groundedness:float | None=None
    ts:Any

class HistoryListResponseSchema(BaseModel):
    session_id:str
    items:list[HistoryItemResponseSchema] =Field(description="查询的数据记录列表", default_factory=list)
