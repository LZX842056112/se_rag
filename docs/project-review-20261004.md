# se_rag 项目全面审查报告

> 审查日期：2026-10-04
> 审查范围：整体代码结构与模块划分、技术架构与设计模式、核心业务流程与数据流转、关键依赖与配置、潜在问题与风险点
> 审查方式：三路并行深度探索（结构与架构 / 业务流程与数据流 / 依赖配置与风险），全部结论均经实际 import 关系与源码交叉验证

---

## 目录

1. [项目定位](#一项目定位)
2. [整体架构：一套代码，两个服务，五层结构](#二整体架构)
3. [主干流程一：文档导入](#三主干流程一文档导入数据如何进来)
4. [主干流程二：问答查询](#四主干流程二问答查询系统的心脏)
5. [特色模块：自进化闭环](#五特色模块自进化闭环项目的灵魂)
6. [存储与依赖全景](#六存储与依赖全景)
7. [测试与工程化](#七测试与工程化)
8. [潜在问题与风险清单](#八潜在问题与风险清单)
9. [最值得关注的三个核心要点](#九最值得关注的三个核心要点)

---

## 一、项目定位

**se_rag**（原名 `sgg_KB_RAG`，2026-09-30 更名，git log 可查）是一个**企业级 RAG 知识库客服系统，带"知识自进化"闭环**。

核心价值主张：

- 把企业文档（`doc/` 下 404MB 的 H3C/华为路由器、打印机等 PDF 产品手册）导入向量库；
- 通过网页提供自然语言客服问答（流式输出、带引用、带图片）；
- 独特的闭环设计让知识库越用越"聪明"：

```
用户提问 → 检索增强生成 → 用户反馈 → 发现知识缺口
    ↑                                      ↓
知识库回流 ← 人工审批 ← 候选 FAQ 生成 ←──┘
```

---

## 二、整体架构

### 2.1 技术栈

| 类别 | 选型 |
|---|---|
| 语言/工具链 | Python ≥3.11，`uv` 管理依赖（`pyproject.toml`，锁定于 `uv.lock`） |
| Web 框架 | FastAPI + Uvicorn，SSE 流式响应 |
| 工作流编排 | LangGraph `StateGraph`（确定性图，非自主 Agent）+ LangChain 生态 |
| 向量数据库 | Milvus（`pymilvus[model]`，稠密 HNSW + 稀疏倒排混合检索） |
| 业务数据库 | MongoDB（`pymongo`，6 个集合） |
| 对象存储 | MinIO（Markdown 图片外链，公共读桶） |
| PDF 解析 | MinerU 云服务 |
| 嵌入模型 | BGE-M3（本地，稠密 1024 维 + 稀疏双向量） |
| 重排模型 | BGE-Reranker-Large（本地交叉编码器） |
| 生成 LLM | DashScope 通义千问（OpenAI 兼容接口，默认 `qwen3-32b`） |
| 联网搜索 | 阿里百炼 `bailian_web_search`（经 openai-agents 的 MCP 客户端） |
| 前端 | 原生 HTML/CSS/JS，无构建链 |

### 2.2 部署形态：一套代码，两个服务

| 服务 | 入口 | 端口 | 职责 |
|---|---|---|---|
| 导入服务 | `app/api/http/import_server.py` | 8000 | 文档上传入库 |
| 查询服务 | `app/api/http/query_server.py` | 8001 | 问答；lifespan 中额外拉起自进化调度器（`query_server.py:31-43`，受 `EVOLUTION_ENABLED` 开关控制） |

### 2.3 五层结构（严格单向依赖，已逐层验证）

```
app/api        接口层：路由 + Pydantic 契约（~580 行）
   ↓
app/process    编排层：两个 LangGraph 图，节点极薄（平均 15 行/文件）
   ↓
app/rag        业务实现层：导入/查询两条流水线的具体 service
app/evolution  自进化闭环：反馈→缺口→候选→审批→回流
   ↓
app/shared     底座：配置、外部客户端网关、模型门面、运行时工具
```

依赖方向约束 `api → process → rag/evolution → shared → 外部` 经 grep 实际 import 验证成立（`shared` 无反向依赖）。仅有的两处跨层例外均与自进化有关，属有意设计：

- `app/rag/query/pipeline.py:4` 引用 `app.evolution.feedback.collector`（查询结束回传会话信号）；
- `app/process/query/agent/nodes/node_search_embedding.py` 引用 `app.evolution.retrieval`（查询时召回自进化 FAQ）。

### 2.4 模块职责明细

**app/api（入口编排）**
- `routers/import_.py` → 调 `app.rag.import_.pipeline.invoke_import_graph`
- `routers/query.py` → 调 `app.rag.query.pipeline.invoke_query_graph`、`sse_broker.stream_events`
- `routers/evolution.py` → 调审批/反馈/仓储/线上指标服务
- Pydantic 契约在 `app/api/schema/`，统一错误体在 `app/api/errors.py`

**app/process（LangGraph 图编排）**
- 导入图 `app/process/import_/agent/main_graph.py`：`entry →(条件边 md/pdf)→ pdf_to_md → md_img → document_split → item_name_recognition → bge_embedding → import_milvus → END`
- 查询图 `app/process/query/agent/main_graph.py`：`item_name_confirm →(条件边)→ 三路并行召回 [search_embedding | search_embedding_hyde | web_search_mcp] → rrf → rerank → answer_output → END`
- 每个节点仅委托给 `app/rag/**` 的 service，如 `node_rerank.py → app.rag.query.rerank_service`

**app/rag（业务实现，~2275 行）**
- `import_/`：`pdf_parse_service.py`（MinerU）、`split_service.py`、`item_name_service.py`、`embedding_service.py`、`index_service.py`、`pipeline.py`
- `query/`：`embedding_search_service.py`（稠密+稀疏混合检索）、`hyde_search_service.py`、`web_search_service.py`（MCP）、`rrf_service.py`、`rerank_service.py`、`answer_service.py`、`item_name_confirm_service.py`、`citations.py`、`pipeline.py`
- `item_name/`：`catalog.py` / `match.py` 商品主体库与对齐

**app/evolution（自进化闭环，~1250 行）**
- `feedback/collector.py` → `gap/detector.py` → `candidate/generator.py`（+`pii.py` PII 拦截、`quality.py` 质量闸门）→ `approval/service.py` → `index/update.py`（回流 Milvus）
- `scheduler.py`：定时驱动全闭环 + 指标快照（`online_eval/metrics.py`）+ 参数自调（`tuning/controller.py`）+ 回测止损（`backtest/runner.py`）
- `repositories.py`：Mongo 仓储（唯一写入者模式）

**app/shared（底座，~1453 行）**
- `config/settings.py`：`Settings` 聚合 12 个配置段（LLM/Embedding/Reranker/Milvus/Minio/Mineru/Mcp/Mongo/Api/Evolution/Runtime）
- `clients/`：`milvus_gateway.py`（单例 + 混合检索 + `in_expr`/`eq_expr` 表达式防注入）、`mongo.py`、`minio_gateway.py`、`history_repository.py`
- `models/`：`providers.py` 暴露 `llm_providers` 门面（对话/视觉/嵌入/重排统一入口）
- `runtime/`：`logger.py`（loguru）、`prompts.py`；`utils/`：`sse_broker.py`、`task_state.py`、`rate_limit.py`

**app/rag_eval（独立评估子系统）**
- `runner.py` / `dataset.py` / `metrics.py` / `tester.py`，artifacts 含基线与回归报告 JSON；设计为可整体复制迁移到其他 RAG 项目

### 2.5 设计模式

1. **分层架构 + 严格单向依赖**（README §2.1 有明文约束，代码验证成立）
2. **LangGraph 确定性工作流编排**：条件边路由（文件类型分支、主体确认分支）+ 扇出并行召回 + 汇聚（RRF）——刻意不用自主 Agent 循环，保证行为可预测
3. **薄节点/厚服务**：process 节点 ~15 行纯委托，业务逻辑与编排解耦
4. **服务-仓储模式**：`app/evolution/repositories.py` 是 Mongo 唯一写入者
5. **客户端网关单例 + 模型门面**：`milvus_gateway` 单例；`shared/models/providers.py` 的 `llm_providers` 统一模型入口
6. **管道模式**：导入链与查询链均为固定阶段流水线
7. **闭环/自进化架构**（本项目特色）：带质量闸门、PII 拦截、人工审批、回测止损多重防护
8. **调度器模式**：`app/evolution/scheduler.py` asyncio 循环，单 worker 设计（注释明示多 worker 需分布式锁）
9. **SSE 发布-订阅**：`app/shared/utils/sse_broker.py`（channel 管理 + `stream_events` 异步迭代器）

---

## 三、主干流程一：文档导入（数据如何进来）

入口 `POST /api/import/upload` → `app/rag/import_/pipeline.py:15` 的 `invoke_import_graph` 驱动导入图（文件保存于 `output/<date>/<task_id>/`，任务状态存内存 TTL 6 小时，经 `/api/import/status/{task_id}` 查询）：

| 节点 | 实现文件 | 职责 |
|---|---|---|
| entry | `node_entry.py` → `entry_service.py` | 识别文件类型，条件边路由 md / pdf（不支持类型直接结束） |
| pdf_to_md | `pdf_parse_service.py` | 上传 PDF 到 MinerU 云服务，轮询（最长 600s），下载解压 Markdown 结果 |
| md_img | `enrich_markdown_images.py:123` | 扫描本地图片 → 视觉 LLM 生成图片摘要（prompt `image_summary.prompt`）→ 上传 MinIO → 重写 Markdown 链接 |
| document_split | `split_service.py:21,45` | 清理换行 → 按标题语义切分 + `RecursiveCharacterTextSplitter` 兜底（CHUNK_SIZE=600 / OVERLAP=50 / MIN=400，`app/rag/import_/config.py:18-24`） |
| item_name_recognition | `item_name_service.py:97` | LLM 从块中提取"商品主体"（prompt `item_name_recognition.prompt`），对齐 Milvus 主体库并 upsert `kb_item_names` |
| bge_embedding | `embedding_service.py:36` | BGE-M3 批量生成稠密 + 稀疏双向量 |
| import_milvus | `index_service.py:39` | 写入 `kb_chunks`（schema 见 ：23-34），按 `file_title` 先删后插实现幂等覆盖（:48） |

**设计要点**：同名文件重复上传不会产生重复数据；商品主体（item_name）在导入期就完成对齐，为查询期的主体过滤打基础。

---

## 四、主干流程二：问答查询（系统的心脏）

入口 `POST /api/query` → `app/rag/query/pipeline.py:20` 的 `invoke_query_graph`。流式模式下立即返回 `session_id`，图在后台执行，SSE 事件序列为 `ready → progress → delta → final → close`。

### 4.1 主体确认 + 查询改写

`node_item_name_confirm.py` → `item_name_confirm_service.py:110`：

- 结合 10 轮历史（`history_utils.py:21`，只取带 `item_names` 标记的消息——这是"是 X 型号呢？"这类追问能正确解析主体的关键机制）；
- JSON 模式 LLM 提取商品名并改写问题（prompt `rewritten_query_and_itemnames.prompt`）；
- 对齐 Milvus 主体库（`app/rag/item_name/match.py`，目录缓存在 `catalog.py`）；
- **主体不明确时**设置"带可点击选项的澄清回答"（`apply_item_name_result`:44），条件边直接短路到作答；
- 同时把用户本轮消息落库 Mongo（:97）。

### 4.2 三路并行召回（扇出）

| 路线 | 实现 | 说明 |
|---|---|---|
| ① 向量检索 | `node_search_embedding.py:16` | 改写问题 embed 一次，`chunk_search.py:41` 混合检索 `kb_chunks`（稠密 0.6 / 稀疏 0.4 加权，`item_name in [...]` 过滤 + 同名等价扩展），**同时召回自进化 FAQ**（`app/evolution/retrieval.py` 的 `search_evolution_items`） |
| ② HyDE | `hyde_search_service.py:20` | LLM 先虚构一个"假想答案"（prompt `hyde_prompt.prompt`），用 `question:假想答案` 去检索 |
| ③ 联网搜索 | `web_search_service.py:54` | MCP 调百炼 `bailian_web_search`，硬超时，失败静默降级为空 |

### 4.3 RRF 融合 → 重排 → 作答（汇聚）

1. **RRF 融合**（`rrf_service.py:47`）：加权倒数排名融合三路结果；**已审批的自进化条目有"保送"机制**（:61-65），保证不被网络结果稀释；
2. **重排**（`rerank_service.py:248`）：合并 RRF + 网页文档（:40）→ 超长文本并发 LLM 摘要再打分（:136,:169）→ BGE-Reranker 交叉编码（:213）→ 本地文档优先于网页（:88）→ 网页数量封顶（:76）→ 动态分数差截断（:222）→ 自进化权威条目重入（:117）；
3. **作答**（`answer_service.py:142`）：编号上下文 + 6 轮历史组装 prompt（模板 `answer_out.prompt`）→ LLM 流式生成，token 以 SSE delta 实时推送（:63-76）→ 提取图片 URL（:79，"无法作答"兜底时抑制）→ 回填三类引用 kb/evolution/web（`citations.py`）→ LLM 自评接地性 groundedness（`app/evolution/online_eval/grounding.py:29`）→ 助手消息落 Mongo（:128）。

**一句话总结**：`POST /api/query → 主体确认/改写 → 三路召回 → RRF → 重排 → 流式作答`，全程 SSE 可视化。

---

## 五、特色模块：自进化闭环（项目的灵魂）

由 `app/evolution/scheduler.py` 的 asyncio 循环驱动（`EVOLUTION_ENABLED` 开关，当前 `.env` 配置 `EVOLUTION_SCHEDULE_INTERVAL_MINUTES=1` 即每 1 分钟一轮）：

```
① 反馈收集  用户点踩（POST /api/evolution/feedback）
            + 自动信号（零召回 / 未检索 / 接地性低，查询结束自动回传）
            → Mongo fb_events
② 缺口发现  gap/detector.py 扫描分级出强/弱知识缺口 → k_gaps
③ 候选生成  candidate/generator.py 用 LLM 基于现有知识库上下文蒸馏 FAQ
            → PII 拦截（pii.py）→ 质量闸门（quality.py）→ k_candidates
④ 人工审批  approval/service.py 状态机（草稿→生效/拒绝，原子认领）
            管理员 token 保护，前端 /approval 页面操作
⑤ 回流生效  审批通过 → index/update.py upsert 到 Milvus kb_evolution_items
            → 成为查询时第四路召回源（带"保送"和"权威重入"特殊逻辑）
⑥ 线上自调优 tuning/controller.py 自动调 RRF_K/RRF_TOP/RERANK_TOP_K（可回滚）
            backtest/runner.py 回测止损；online_eval/metrics.py 指标快照
```

**设计要点**：这个闭环是项目区别于普通 RAG 的差异化设计，但它也把进化数据通路（反馈接口）直接暴露给了终端用户——这是后文风险清单里"反馈注入"攻击面的来源。

---

## 六、存储与依赖全景

### 6.1 数据契约

| 存储 | 集合/桶 | 用途 |
|---|---|---|
| Milvus | `kb_chunks`（自增主键，dense COSINE + sparse IP） | 文档分块 + 双向量 |
| Milvus | `kb_item_names` | 商品主体目录 |
| Milvus | `kb_evolution_items` | 已审批的自进化 FAQ |
| MongoDB | `chat_message` | 会话历史 |
| MongoDB | `fb_events` / `k_gaps` / `k_candidates` | 反馈 / 缺口 / 候选 |
| MongoDB | `k_metrics` / `param_registry` | 指标快照 / 参数注册表 |
| MinIO | 公共读桶 | 手册图片外链 |
| 本地磁盘 | `output/<date>/<task_id>/`、`logs/` | 上传文件、Markdown 备份、日志 |
| 进程内存 | `task_state.py`（TTL 6h）、`sse_broker.py` | 任务进度、SSE 通道 |

### 6.2 关键依赖（`pyproject.toml`，锁定于 `uv.lock`）

| 依赖 | 版本 | 备注 |
|---|---|---|
| fastapi | ≥0.135.1 | 当前 |
| torch / transformers / flagembedding | 2.10.0 / 4.57.6 / 1.3.5 | 重量级；仅用于 BGE-M3 与重排的 **CPU 推理**，跑在 API 进程内 |
| langchain / langgraph / langchain-openai | 1.2.13 / 1.3.x | 全家桶 |
| openai-agents | 0.4.2 | 第二套 Agent 框架，仅为 MCP 联网搜索所用（与 LangGraph 并存） |
| pymilvus[model] + pymilvus-model | 2.6.10 / 0.3.2 | 冗余——`pymilvus[model]` 已含 model extra |
| numpy | 2.4.3 | 前沿版本 |
| minio / pymongo / uvicorn / loguru 等 | 当前 | 正常 |

版本声明均为无上界 `>=`（有 `uv.lock` 兜底，但改用 pip 安装则会不受约束解析）。

### 6.3 配置

唯一配置出口 `app/shared/config/settings.py`（12 个配置段，全部 env 解析集中于此，设计良好）。`.env` 含 47 项配置（DashScope key、MinerU token、Milvus/Mongo/MinIO 地址凭据、BGE 设备、演化阈值等）。

---

## 七、测试与工程化

- **单元测试**：`tests/unit/` 31-32 个离线测试文件，覆盖切分、RRF 融合、重排（限流/来源优先/截断）、引用、主体匹配、嵌入复用、Milvus 表达式转义、MinIO/Mongo 客户端、SSE broker、任务状态、API 路由、前端页面，以及整个进化闭环（反馈、缺口、候选、审批、仓储、质量、信号完整性、PII、装配）；
- **E2E 测试**：`tests/e2e/test_import_query_flow.py`，需 `E2E_ENABLED=1` 且 `-m e2e` 显式运行（`pyproject.toml:40-46` 默认跳过）；`tests/conftest.py` 有离线护栏阻止真实连接；
- **代码卫生亮点**：无裸 `except:`、无 TODO/FIXME/HACK（0 命中）、无超 500 行文件（最大 `app/rag_eval/runner.py` 419 行）、模块 docstring 覆盖率高质量好、Milvus 表达式注入已做转义（`milvus_gateway.py:39-55`）、上传路径 basename 防目录穿越（`import_.py:39`）、静态资源白名单（`pages.py:23-39`）、源码零硬编码密钥。

---

## 八、潜在问题与风险清单

### 🔴 高

| # | 风险 | 位置 | 说明 |
|---|---|---|---|
| H1 | **API 几乎无鉴权** | `import_.py:30`、`query.py`、`query.py:98`、`evolution.py:56` | 上传文档、提问、删除任意会话历史、提交反馈全部开放（仅审批接口有 token）。网络内任何人可**上传文档污染知识库**、**注入反馈操纵进化循环** |
| H2 | **无入站限流/大小上限** | `app/shared/utils/rate_limit.py`（出站） | 该文件是对 LLM API 的*出站* 3000 req/min 节流；入站接口零保护。上传无文件大小上限；接口收文件列表却只处理 `files[0]`（`import_.py:38`，静默丢弃其余） |
| H3 | **`.env` 存真实密钥** | `.env:12,51,71` | DashScope key、MinerU token、进化管理员 token 明文在盘。已验证未进 git 历史（`git log --all -- .env` 为空），但若目录被共享/同步则泄露；无 `.env.example`，新人无法发现 47 项配置 |

### 🟡 中

| # | 风险 | 位置 | 说明 |
|---|---|---|---|
| M1 | 内网服务全部无凭据 | `.env:31,36-37,24` | Milvus/Mongo 无认证；MinIO 用默认 `minioadmin/minioadmin` 且 `MINIO_SECURE=False` |
| M2 | `.idea/` 已提交 | `.gitignore:164` 被注释 | 8 个 IDE 文件（含 workspace.xml）进了仓库 |
| M3 | 404MB 语料进 git | `doc/` | 全部 PDF + `project_plan.xlsx` 在 git 历史中，克隆成本极高；语料应放对象存储 |
| M4 | `asyncio.run()` 脆弱耦合 | `web_search_service.py:58` | 同步函数里调 `asyncio.run()`，仅因节点跑在线程池才安全；若从有运行中 loop 的线程调用即抛 RuntimeError |
| M5 | 任务状态纯内存 + 长导入 | `task_state.py`、`pdf_parse_service.py:94-133` | 重启丢失所有进行中任务；MinerU 轮询最长 600s 且用 `time.sleep`；高并发上传线程池无上限增长 |
| M6 | 调度器单 worker 无强制 | `app/evolution/scheduler.py:10` | 仅注释说明"多 worker 需分布式锁"；起多个 uvicorn worker 会重复跑进化循环、产生重复候选 |
| M7 | `limit` 参数无上限 | `evolution.py:63`、`query.py:75` | `?limit=1000000` 可拉全表 |
| M8 | CORS 缺省回落通配 | `settings.py:229`、两个 server 的 `list(...) or ["*"]` | 漏配环境变量时静默放开为 `*`（当前 `.env` 已显式配置，但含占位符 `https://your-dom.example`） |

### 🟢 低

| # | 风险 | 位置 | 说明 |
|---|---|---|---|
| L1 | 依赖冗余 | `pyproject.toml` | `pymilvus[model]` 与 `pymilvus-model` 双条目；`openai-agents` 与 LangGraph 两套框架并存（前者仅一个 MCP 调用）；torch 全家桶跑在 API 进程做 CPU 推理（响应慢、内存大，未与 API 分离） |
| L2 | 宽泛 `except Exception` | 全局（均带 `# noqa: BLE001`） | 有意降级设计，但 `milvus_gateway.py:34` 把连接错误吞成 `None`——Milvus 宕机时用户只会看到"无法作答"，故障被隐藏 |
| L3 | 演化节奏激进 | `.env` `EVOLUTION_SCHEDULE_INTERVAL_MINUTES=1` | 全 LLM 驱动的扫描/生成循环每分钟跑一轮，成本与噪声需评估 |
| L4 | MinerU 压缩包无防护 | `pdf_parse_service.py:150` | `shutil.unpack_archive` 解压云端 URL 内容，无 zip 炸弹/大小防护（风险低，落地在任务自身输出目录内） |
| L5 | 无容器化 | 全仓 | 无 Dockerfile/docker-compose，Milvus/Mongo/MinIO 三件套 + 模型缓存部署全靠手工（README 有说明但无脚本） |
| L6 | 命名/常量小重复 | `doc/` vs `docs/`；`app/rag/query/config.py` vs `import_/config.py` | 易混淆；分块/检索常量双处定义 |
| L7 | 直读环境变量 | `evolution.py:35` | 绕过 settings 模块直读 `EVOLUTION_ADMIN_TOKEN`，违反自己声明的"唯一配置出口" |

### 值得表扬的方面

无 SQL（Mongo dict 过滤 + Milvus 表达式转义）、无 `eval`/`exec`/`pickle`/`subprocess`/`shell=True`（grep 全仓验证 0 命中）、无模型下载（本地路径加载 + `MODELSCOPE_OFFLINE=1`）、`.gitignore` 主干完好（`.env`/`.venv`/`__pycache__`/`output/` 均覆盖且 `.env` 确实未入库）。

---

## 九、最值得关注的三个核心要点

### 要点一：查询图的"三路召回 + RRF + 重排"链路（理解它 = 理解系统 80% 的价值）

`app/process/query/agent/main_graph.py` + `app/rag/query/`。这是检索质量的决定因素。特别留意两处把自进化闭环与主链路"缝合"的特殊逻辑：

- **保送机制**（`rrf_service.py:61-65`）：已审批进化条目在 RRF 融合中不被稀释；
- **权威重入**（`rerank_service.py:117` 的 `ensure_evolution_docs`）：重排截断后进化条目强制回位。

### 要点二：自进化闭环既是差异化亮点，也是最大攻击面

`app/evolution/` 让知识库自我生长，但开放接口可注入反馈 → 制造假缺口 → 生成假 FAQ；唯一的防线是人工审批这一道闸门，而闸门 token 是 `.env` 里的静态字符串。闭环的安全强度 = 审批环节的安全强度。

### 要点三：安全与运维短板是当前最紧迫的债

鉴权缺失（H1）+ 密钥裸放（H3）+ 内网服务无凭据（M1）三者叠加，意味着这套系统**目前只能在内网信任环境中运行**。上生产前的优先动作：

1. 轮换 `.env` 中的真实密钥，补 `.env.example`；
2. 给上传 / 历史删除 / 反馈接口加鉴权，加入站限流与文件大小上限；
3. 给 Mongo/MinIO 加凭据，替换 `minioadmin` 默认口令；
4. 把 `doc/`（404MB）与 `.idea/` 移出 git 跟踪并清理历史；
5. 收敛依赖：二选一 Agent 框架、去重 pymilvus 包；考虑把 CPU 嵌入/重排移出 API 进程。

---

## 附：端到端一图流

```
┌─────────── 导入服务 :8000 ───────────┐        ┌─────────────────── 查询服务 :8001 ───────────────────┐
│ POST /api/import/upload              │        │ POST /api/query → session_id                         │
│   → entry → pdf_to_md(MinerU)        │        │   → item_name_confirm（主体确认+改写，可短路澄清）      │
│   → md_img(VL摘要+MinIO)             │        │   → 三路并行召回：向量混合检索 │ HyDE │ MCP 联网        │
│   → document_split → item_name       │        │   → RRF 融合（进化条目保送）                          │
│   → bge_embedding(BGE-M3 双向量)      │        │   → BGE-Reranker 重排（本地优先/网页封顶/动态截断）      │
│   → import_milvus(kb_chunks 幂等)     │        │   → answer_output（LLM 流式 → SSE delta）             │
└──────────────────────────────────────┘        │   → 引用 + 图片 + groundedness → Mongo 历史           │
                                                └──────────────────────────────────────────────────────┘
                                                                   │ 点踩 / 自动信号（零召回/未检索/低接地）
                                                                   ▼
                                                ┌────────────── 自进化闭环（scheduler 每 1 分钟）────────┐
                                                │ fb_events → 缺口扫描(k_gaps) → LLM 候选 FAQ(k_candidates)│
                                                │ → PII 拦截+质量闸门 → 人工审批(/approval)               │
                                                │ → Milvus kb_evolution_items ←── 成为第四路召回源         │
                                                │ → 参数自调(RRF_K 等,可回滚) + 回测止损 + 指标快照          │
                                                └──────────────────────────────────────────────────────┘
```
