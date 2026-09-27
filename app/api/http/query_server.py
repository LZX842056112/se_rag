import asyncio
import datetime
from contextlib import asynccontextmanager
from mimetypes import guess_type
from pathlib import Path
import sys
import uuid

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from starlette.middleware.cors import CORSMiddleware
from app.infra.persistence.history_repository import history_repository

from app.api.schema.query_schema import HealthResponseSchema, QueryRequestSchema, QueryStreamResponseSchema, \
    QueryNotStreamResponseSchema, HistoryClearResponseSchema, HistoryListResponseSchema, HistoryItemResponseSchema
from app.evolution.schema import CitationModel
from app.evolution.feedback.api import feedback_router
from app.evolution.approval.api import approval_router
from app.evolution.feedback.collector import flush_session_signals
from app.evolution.scheduler import evolution_scheduler_loop
from app.evolution.config import evolution_config
from app.shared.runtime.logger import PROJECT_ROOT, logger
from app.infra.config.providers import settings
from app.process.query.agent.main_graph import query_app
from app.process.query.agent.state import create_query_default_state, QueryGraphState
from app.shared.utils.sse_utils import SSEEvent, create_sse_queue, push_to_session, sse_generator
from app.shared.utils.task_utils import (
    TASK_STATUS_COMPLETED,
    TASK_STATUS_FAILED,
    TASK_STATUS_PROCESSING,
    clear_task,
    get_done_task_list,
    get_task_result,
    update_task_status,
)

# 自进化调度器：服务启动时挂载后台循环，关闭时取消（单 worker 部署前提）
@asynccontextmanager
async def lifespan(_app: FastAPI):
    _sched_task = None
    if getattr(evolution_config, "enabled", False) and getattr(evolution_config, "scheduler_enabled", False):
        _sched_task = asyncio.create_task(
            evolution_scheduler_loop(evolution_config.schedule_interval_minutes * 60.0)
        )
        logger.info(
            f"自进化调度器已启动: interval={evolution_config.schedule_interval_minutes}min, "
            f"batch={evolution_config.scan_batch}"
        )
    try:
        yield
    finally:
        if _sched_task is not None:
            _sched_task.cancel()
            try:
                await _sched_task
            except asyncio.CancelledError:
                pass


# 定义fastapi对象
app = FastAPI(
    title=settings.query_app_name,
    description="描述,进行rag查询的服务对象",
    version="0.2.0",
    lifespan=lifespan,
)

# 跨域处理 [前端和后端可能不在一个服务器,非同源,浏览器认为跨域,拦截了 ]
# CORS_ORIGINS 环境变量可配白名单（逗号分隔），默认 * 保持现状；import_server 同规则
app.add_middleware(
    CORSMiddleware,
    allow_origins = list(settings.cors_origins) or ['*'],
    allow_methods = ['*'],
    allow_headers = ['*']
)

# 自进化反馈路由（POST /evolution/feedback）
app.include_router(feedback_router)
# 自进化候选审批路由（GET/POST /evolution/candidates…）
app.include_router(approval_router)

# 接口1.1: 审批后台页面
@app.get("/html_approval")
def query_html_approval():
    html_obj: Path = PROJECT_ROOT / "app" / "resources" / "html" / "approval.html"
    return FileResponse(
        path=str(html_obj),
        media_type=guess_type(html_obj.name)[0]
    )

# 共用前端 JS：chat / approval 页面 <script src="/js/common.js"> 使用
@app.get("/js/common.js")
def common_js():
    js_obj: Path = PROJECT_ROOT / "app" / "resources" / "js" / "common.js"
    return FileResponse(
        path=str(js_obj),
        media_type="text/javascript"
    )

# 接口1: 返回页面
@app.get("/html")
def query_html():
    query_html_obj:Path = PROJECT_ROOT / "app" / "resources" / "html" / "chat.html"
    return FileResponse(
        path=str(query_html_obj),
        media_type=guess_type(query_html_obj.name)[0]
    )

#接口2: /health
# {code:200,message=xx}
@app.get("/health")
def health():
    logger.info(f"完成健康检查!{datetime.datetime.now()}")
    return HealthResponseSchema(
        code=200,
        message=f"完成健康检查!{datetime.datetime.now()}"
    )

def _build_citations(state: QueryGraphState) -> list[CitationModel]:
    """将状态中的引用转为对外 CitationModel；优先复用答案回填结果，未回填时按 id 现算。"""
    cached = state.get("citations")
    if cached:
        return [CitationModel(**c) for c in cached]
    ids = state.get("cited_chunk_ids", []) or []
    evo = set(str(i) for i in (state.get("faq_evo_ids", []) or []))
    return [
        CitationModel(faq_id=str(cid), source="evolution" if str(cid) in evo else "kb")
        for cid in ids
    ]


def invoke_query_graph(session_id:str,original_query:str,is_stream:bool)-> QueryGraphState:
    try:
        # 任务整体状态监控!!
        # 需不需要传入第三个参数???
        # 清空task字典! 不管是流式还是非流式都需要!!!
        clear_task(session_id)
        # 如果是流式,我们可以提前创建队列! session_id : queue
        if is_stream:
            create_sse_queue(session_id)
        # 考虑: 可能异步执行 is_stream=True 也可能同步执行 False
        update_task_status(session_id,TASK_STATUS_PROCESSING,push_queue=is_stream)
        # 封装成state
        query_state = create_query_default_state(session_id=session_id,original_query=original_query,is_stream=is_stream)
        # 执行图对象
        result_state =  query_app.invoke(query_state)
        # process ...
        update_task_status(session_id,TASK_STATUS_COMPLETED,push_queue=is_stream)

        # 执行完毕以后 [final - sse - is_stream = True]
        if is_stream:
            push_to_session(
                session_id,
                SSEEvent.FINAL,
                {
                    "answer": result_state.get('answer'),
                    "status": "completed",
                    "image_urls": result_state.get('image_urls',[]),
                    "citations": [c.model_dump() for c in _build_citations(result_state)],
                    "groundedness": result_state.get('groundedness', 0.0),
                }
            )
        # 自进化：会话未解决信号异步落库（enable 时，异常不影响主链路）
        if getattr(evolution_config, "enabled", False):
            try:
                flush_session_signals(result_state)
            except Exception:
                pass
        return result_state
    except Exception as e:
        update_task_status(session_id, TASK_STATUS_FAILED,push_queue=is_stream)
        push_to_session(session_id,SSEEvent.ERROR,{"error":f"{session_id}业务失败!原因:{str(e)}"})
        logger.exception(f"执行查询流程报错,错误信息:{str(e)}")
        return None


# 接口3: 查询接口
@app.post("/query")
def query(backgroundtasks:BackgroundTasks,query_params:QueryRequestSchema):
    """
    查询数据
    :param backgroundtasks:
    :param query:
    :return:
    """
    # 1. 获取参数
    query = query_params.query
    session_id = query_params.session_id or str(uuid.uuid4())
    is_stream = query_params.is_stream
    # 2. 封装执行图对象的方法
    # 3. 判断是不是流式
    if is_stream:
        # 4. 是 异步执行图方法
        backgroundtasks.add_task(
            invoke_query_graph,
            session_id = session_id,
            original_query = query,
            is_stream = is_stream
        )
        return QueryStreamResponseSchema(
            message=f"已经开始:{query}任务查询!",
            session_id=session_id
        )
    else:
        # 5. 不是 直接调用执行图方法
        state =  invoke_query_graph(
            session_id=session_id,
            original_query=query,
            is_stream=is_stream
        )
        if state is None:
            raise HTTPException(status_code=500, detail="query_graph execution failed")

        # task_utils
        done_list = get_done_task_list(session_id)

        return QueryNotStreamResponseSchema(
            message=f"完成{query}所有内容检索!",
            session_id=session_id,
            answer=state.get("answer"),
            done_list=done_list,
            image_urls=state.get("image_urls",[]),
            citations=_build_citations(state),
            groundedness=state.get("groundedness",0.0),
            retrieval_signals=state.get("retrieval_signals",{})
        )


# 接口4: 流式接口
@app.get("/stream/{session_id}")
def stream(session_id:str,request:Request):

    # request.is_disconnected() 通过request对象可以检查前端是否已经断开!
    return StreamingResponse(
        sse_generator(session_id,request),
        media_type="text/event-stream"
    )

@app.delete("/history/{session_id}")
def clear_history(session_id:str):
    deleted_count = history_repository.clear_session(session_id=session_id)
    return HistoryClearResponseSchema(
        message=f"session_id:{session_id}历史聊天记录已经清空!",
        deleted_count=deleted_count
    )

@app.get("/history/{session_id}")
def get_history(session_id:str, limit:int=10):
    history_list : list[dict] = (
        history_repository.list_recent(session_id=session_id,limit=limit))
    return HistoryListResponseSchema(
        session_id=session_id,
        items=[
            HistoryItemResponseSchema(
                id=str(item.get("_id")),
                session_id=session_id,
                role=item.get("role"),
                text=item.get("text"),
                rewritten_query=item.get("rewritten_query"),
                item_names=item.get("item_names",[]),
                image_urls=item.get("image_urls",[]),
                citations=item.get("citations",[]),
                groundedness=item.get("groundedness",0.0),
                ts=item.get("ts")
            )
            for item in history_list
        ] # _id  |  _id ObjectId
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.app_host, port=settings.query_app_port)
