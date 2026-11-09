# 代码设计（CODE_DESIGN）

本文说明系统的模块边界、数据契约、关键流程与设计取舍。产品需求见 `docs/PRD.md`，逐段技术讲解与运维手册见 `docs/TECHNICAL_GUIDE.md`。

## 1. 设计目标

1. **可追溯**：每个答案都能回到原始文档、切片、页码与版本。
2. **可控**：模型只能使用服务端授权的主体范围；价格/售后只认有效政策。
3. **可恢复**：导入、发布、缺口处理都可重试；中断不丢状态。
4. **可演进**：知识缺口能沉淀为候选，经人工审批后上线。
5. **可测试**：主要流程可在离线环境验证，不依赖真实外部服务。

## 2. 运行视图

系统由两个独立 FastAPI 进程组成，共用一套代码与配置。

```text
导入进程 :8000（可多 worker）
  ├─ 上传 / 状态 / 重试 / 删除接口
  ├─ 导入图（LangGraph）
  └─ 全局并发名额 + 磁盘任务快照

问答进程 :8001（可多 worker）
  ├─ 提问 / SSE / 历史 / 反馈 / 自进化接口
  ├─ 查询图（LangGraph）
  ├─ Agentic 控制器
  ├─ 自进化调度器（主节点租约）
  └─ 模型预热

共享运行期状态（Redis，可选）
  ├─ task:state:<id>          任务进度
  ├─ sse:stream:<session_id>  SSE 事件流
  ├─ query:active:<session_id> 会话查询锁
  ├─ import:slots             全局导入名额
  └─ evolution:scheduler:leader 调度器主节点
```

设计取舍：

- 导入与查询分离，重任务不会阻塞在线问答。
- 未配置 `REDIS_URL` 时保持单 worker：查询服务用文件锁，任务状态、SSE 频道与会话锁都在进程内。
- 配置 Redis 后查询与导入都可用多个 worker：任务状态、SSE、会话锁与导入名额跨进程共享。
- 导入任务提交到进程内线程池；单 worker 用 `BoundedSemaphore`，多 worker 用 Redis 有序集合限制全局名额。

## 3. 分层与模块职责

| 层 | 路径 | 职责 |
|---|---|---|
| 接入层 | `app/api/http/` | 服务入口、生命周期、路由挂载 |
| 路由层 | `app/api/routers/` | 参数校验、鉴权、响应封装 |
| 编排层 | `app/process/import_/agent/`、`app/process/query/agent/` | LangGraph 图与节点 |
| 业务层 | `app/rag/import_/`、`app/rag/query/`、`app/rag/agentic/`、`app/rag/item_name/` | 解析、切分、检索、作答、Agentic、主体匹配 |
| 进化层 | `app/evolution/` | 反馈、缺口、候选、审批、索引、调度、指标、调参、回测 |
| 共享层 | `app/shared/` | 配置、存储客户端、模型、日志、SSE、任务状态、通用工具 |
| 页面层 | `app/resources/` | 对话页、导入页、审批页与静态资源 |
| 评估层 | `app/rag_eval/` | 离线评估、切分评估、答案质量、Agentic 对比、隔离联调 |

## 4. 数据契约

### 4.1 Milvus

#### `kb_chunks`（子块主集合）

| 字段 | 类型 | 说明 |
|---|---|---|
| `chunk_id` | VARCHAR(40)，主键 | 确定性哈希：`doc_key#heading_path#occurrence#part` |
| `doc_id` | VARCHAR(64) | 文档级稳定标识 |
| `parent_id` | VARCHAR(40) | 章节父块标识 |
| `file_title` | VARCHAR(512) | 文件标题 |
| `item_name` | VARCHAR(512) | 主体标准名 |
| `title` | VARCHAR(512) | 子块标题 |
| `parent_title` | VARCHAR(512) | 父块标题 |
| `heading_path` | VARCHAR(1024) | 标题面包屑 |
| `content` | VARCHAR(65535) | 子块正文 |
| `content_hash` | VARCHAR(40) | 正文指纹 |
| `document_kind` | VARCHAR(16) | `manual` / `policy` |
| `document_version` | VARCHAR(128) | 文档版本 |
| `part` | INT16 | 章节内子块序号 |
| `seq` | INT32 | 文档内顺序 |
| `page` | INT32 | 页码 |
| `valid_until` | DOUBLE | 政策失效时间戳 |
| `dense_vector` | 稠密向量 | BGE-M3 dense |
| `sparse_vector` | 稀疏向量 | BGE-M3 sparse |

索引：`dense` 使用 COSINE，`sparse` 使用 IP。`auto_id=False`，主键由业务生成。

#### `kb_item_names`（主体名目录）

| 字段 | 类型 | 说明 |
|---|---|---|
| `pk` | INT64，自增主键 | 无业务含义 |
| `file_title` | VARCHAR(512) | 来源文档 |
| `item_name` | VARCHAR(512) | 主体标准名 |
| `dense_vector` / `sparse_vector` | 向量 | 主体名向量 |

写入策略：先插入新行并取回自增 pk，再删除该 `file_title` 下其他 pk。避免“先删后插”造成主体名丢失。写入端只做词法归并（精确同名 / 品牌前缀后缀），不用向量相似度自动合并。

#### `kb_evolution_items`（已审批 FAQ）

| 字段 | 类型 | 说明 |
|---|---|---|
| `evo_doc_id` | VARCHAR(128)，主键 | 由发布任务 ID 派生 |
| `faq_question` | VARCHAR(512) | 规范问题 |
| `faq_answer` | VARCHAR(65535) | 答案正文 |
| `source_refs` | VARCHAR(2048) | 来源切片 |
| `item_name` | VARCHAR(512) | 主体标准名 |
| `status` | VARCHAR(32) | `active` 等 |
| `file_title` | VARCHAR(512) | 固定为 `__evolution__` |

扩展字段：`source_version`、`document_version`、`publication_status`、`knowledge_origin`、`index_version`、`source_versions`、`manual_edited_at`、`approved_at`、`approved_content_sha256`、`dense_vector`、`sparse_vector`。

索引：`dense` 使用 IP（稠密向量已 L2 归一化）。

### 4.2 MongoDB 集合

| 集合 | 用途 | 关键索引 |
|---|---|---|
| `chat_message` | 会话消息 | `(session_id, ts desc)` |
| `query_sessions` | 会话归属 | `_id = session_id` |
| `query_runs` | 查询指标 | `turn_id` 唯一、`ts desc` |
| `query_feedback` | 显式反馈 | `answer_id` 唯一 |
| `query_issues` | 未解决问题 | `(status, created_at desc)`；`_id` 由固定键派生，重复登记幂等 |
| `fb_events` | 反馈事件 | `(session_id, ts desc)`、`ts desc` |
| `k_gaps` | 知识缺口 | `(session_id, ts desc)`、来源唯一约束 |
| `k_candidates` | 知识候选 | `faq_question`、`status` |
| `k_candidates_publication_tasks` | 发布任务队列 | 任务状态与租约 |
| `k_metrics` | 指标快照 | `ts desc` |
| `param_registry` | 自调参数 | `key` 唯一 |
| `kb_parent_chunks` | 章节父块 | `parent_id` 唯一、`doc_id` |

### 4.3 MinIO

- 对象键：`<image_dir>/<stem>/<image_name>`
- 访问地址：`<scheme>://<endpoint>/<bucket>/<object_key>`
- 导入时先上传新图，再清理本次未引用的旧对象。

### 4.4 本地目录

```text
output/
├─ <YYYYMMDD>/
│  └─ <task_id>/
│     ├─ <原文件>
│     ├─ <stem>.zip                # MinerU 结果压缩包
│     ├─ <stem>/                   # 解压后的 Markdown、content_list、images
│     ├─ <stem>_new.md             # 图片增强后的 Markdown
│     └─ _import_meta.json         # 重试用的上传参数
└─ tasks/
   └─ <task_id>.json               # 任务状态磁盘快照
```

## 5. 导入子系统

### 5.1 入口

`app/api/routers/import_.py`：

1. `require_import_admin_token` 校验 `X-Internal-Token`。
2. `validate_upload_files` 整批校验文件数量、名称、后缀、大小、文件头；任一文件不合法即整批拒绝。
3. 业务政策校验版本与有效期。
4. 逐文件创建 `task_id` 与任务目录，保存文件与 `_import_meta.json`；返回与文件顺序一致的 `task_ids`。
5. 落盘中途失败时回滚已建任务与目录。
6. `BackgroundTasks` 为每个任务调用 `submit_import_graph`，由进程内线程池并行执行。

### 5.2 执行器

`app/rag/import_/pipeline.py`：

- 抢并发名额 `IMPORT_MAX_CONCURRENT_TASKS`，超时则失败为 `import_busy`。
- 状态置为 `processing` 并写快照。
- 按 `IMPORT_MAX_RETRY_ATTEMPTS` 重试；只重试可重试错误码。
- 成功置 `completed`，失败置 `failed` 并记录 `error_code` 与中文消息。
- 状态落盘 `output/tasks/<task_id>.json`。

### 5.3 导入图

`app/process/import_/agent/main_graph.py`：

```text
node_entry
  ├─ .md  → node_md_img
  └─ .pdf → node_pdf_to_md → node_md_img
node_md_img → node_document_split → node_item_name_recognition
            → node_bge_embedding → node_import_milvus → END
```

| 节点 | 实际服务 | 职责 |
|---|---|---|
| `node_entry` | `entry_service.resolve_input_file` | 分派 md/pdf，写 `file_title` |
| `node_pdf_to_md` | `pdf_parse_service.parse_pdf_to_markdown` | MinerU 上传、轮询、下载、解压、重命名 |
| `node_md_img` | `enrich_markdown_images.enrich_markdown_images` | 图片说明、MinIO 上传、链接替换、写 `_new.md` |
| `node_document_split` | `split_service.split_document` | 结构解析、章节识别、父块/子块 |
| `node_item_name_recognition` | `item_name_service.recognize_and_index_item_name` | 主体识别、词法归并、写索引、回填 |
| `node_bge_embedding` | `embedding_service.generate_chunk_embeddings` | 批量向量化 |
| `node_import_milvus` | `index_service.index_chunks` | 建集合、对齐、upsert、清理旧块、写父块、对账 |

### 5.4 MinerU 解析

- 默认 `model_version=vlm`。
- `vlm` 对超大单页排版稿（如 A3）可能连续失败。失败时自动改用 `pipeline` 再解析一次。
- 轮询默认每 3 秒一次，最长 600 秒。
- 下载 zip 后选择主 Markdown：优先与文件同名，否则 `full.md`，再否则第一个 Markdown。
- 断点续传：若 `<stem>.md` 比 PDF 新，直接复用。
- 失败原因取自服务端 `err_msg`，不只记录 `batch_id`。

### 5.5 切分

`app/rag/import_/split_service.py` + `section_splitter.py`：

1. 读取 Markdown（优先 `_new.md`）。
2. `resolve_blocks`：优先 `*_content_list.json`，缺失回退 Markdown。
3. `build_sections`：按标题层级构建章节树，记录 `heading_path` 与 `occurrence`。
4. 每个章节产出父块：整棵子树文本。
5. 每个章节产出子块：仅本章节自身原子块，避免与子章节重复。
6. 长度按正文口径计算：
   - `CHUNK_SIZE`：软目标，用于打包多个原子块。
   - `CHUNK_MAX_SIZE`：硬上限，单块超过才切。
   - `CHUNK_MIN`：短块合并阈值。
   - `CHUNK_OVERLAP`：仅超长原子块切分时保留尾部重叠。
7. 表格：HTML 转 Markdown；切分时复制表头与分隔行。
8. 代码：只按行切分。
9. 图片：使用增强后的 Markdown 引用，说明可被检索。

### 5.6 确定性 ID

`app/rag/import_/ids.py`：

- `doc_id = sha1(规范化 file_title)`
- `parent_id = sha1(doc_key#heading_path#occurrence)`
- `chunk_id = sha1(doc_key#heading_path#occurrence#part)`
- `content_hash = sha1(content)`

ID 只编码结构，不编码内容。内容变化由 `content_hash` 感知，因此重复导入、标注、反馈、引用都稳定。

### 5.7 写入与一致性

`app/rag/import_/index_service.py`：

1. 集合不存在时创建。
2. `_align_to_schema`：补齐缺省字段、按 UTF-8 字节截断 VARCHAR、忽略未知字段。
3. `upsert` 分批写入确定性主键。
4. `_delete_stale_chunks` 删除该文档本次未再产出的旧块。
5. 父块写入 Mongo；失败只告警，不阻断导入。
6. `reconcile_after_import` 只读对账：Milvus 按 `chunk_id` 集合比对，Mongo 比对父块，MinIO 只统计对象数。

写入顺序为“先写新、后清旧”，避免“删成功、插失败”的数据空窗。

### 5.8 重试与删除

- `POST /api/import/retry/{task_id}`：仅允许 `failed`；复用原任务目录与原文件；清空旧进度；状态置 `pending` 后重新提交。业务政策重试前检查有效期。
- `DELETE /api/import/records/{task_id}`：清理内存状态、磁盘快照、任务目录。进行中任务返回 409。已入库向量不删除。
- 两者共用「进程内锁 + Redis 会话锁」的保护，避免双击重试导致同一文件并发导入两次。

## 6. 查询子系统

### 6.1 查询图

`app/process/query/agent/main_graph.py`：

```text
node_intent_route
  ├─ chitchat → node_chitchat_answer → END
  └─ rag → node_item_name_confirm
              ├─ 有答案（反问/兜底）→ node_answer_output → END
              ├─ agentic → node_agentic → END
              └─ legacy → 三路召回 → node_rrf → node_rerank
                        → node_parent_expand → node_answer_output → END
```

### 6.2 意图与主体

- 意图识别：LLM 二分类，超时或异常一律兜底为 `rag`；并发受信号量限制。
- 主体确认：
  1. `rewrite_hint` 存在时，先做确定性主体对齐。
  2. 上一轮反问且本轮是候选主体名时，用挂起意图复原问句。
  3. 否则读历史 + LLM 改写 + 主体匹配。
  4. 未确认时返回候选、反问或兜底。

### 6.3 Legacy 检索

1. 向量召回：BGE-M3 混合检索，权重 0.6 / 0.4，候选 10、返回 5。
2. HyDE：生成假设答案后拼入查询；可对短事实型问题跳过。
3. 联网：MCP 搜索，定价/售后禁用，最终上下文最多保留 2 条。
4. RRF：`weight / (k + rank)`，`k=60`，Top 5；自进化条目不因融合被挤出。
5. 重排：BGE-Reranker 打分；动态截断，范围 2~6；本地知识优先，自进化条目保底。
6. 父块回溯：按重排分数分配字符预算，单块 1200、总量 4000。
7. 作答：组装上下文与历史；流式或非流式；回填引用、页码、章节、接地性，落库历史。

### 6.4 Agentic

入口 `app/rag/agentic/controller.py`：

- 业务标签分类；投诉类直接应用策略并终止。
- 工具集：`RetrievalTools`。
- 预算：工具 4 次、联网 1 次、token 8000、时长取配置与外部剩余时间的最小值。
- 状态：证据字典、轨迹、覆盖度、动作、最终证据、计数器。

状态机：

```text
bootstrap → assess
              ├─ supported 且证据已读 → draft → verify
              │                              ├─ 通过 → answered
              │                              └─ 不通过 → draft（一次）→ 兜底
              └─ missing / conflicting → select → execute → assess
```

关键约束：

- `assess`：只有覆盖度 `supported`、引用的证据都存在且已读，才进入 `draft`。
- `select`：严格 `Action` Schema；非法动作允许修正一次。
- `execute`：预算记账、主体范围校验、证据合并、连续两轮无新证据则终止。
- `draft`：发布前复核证据仍有效；空答案判失败。
- `verify`：事实与来源 ID 必须由最终证据支持；核验通过才发布。
- 任何异常都转成终止原因；唯一正常出口是 `finish`。

工具与安全：

| 工具 | 来源 | 约束 |
|---|---|---|
| `semantic_search` | `kb_chunks` | 主体过滤 |
| `keyword_search` | `kb_chunks` | 有界扫描，超过上限报错 |
| `read_chunk` | `kb_chunks` | 指定块 + 邻居 + 父块 |
| `search_approved_faq` | `kb_evolution_items` + Mongo | 发布状态、来源、主体三重校验 |
| `web_search` | MCP | 定价/售后禁用 |

证据：`evidence_id = 来源:标识:版本哈希:正文哈希`。发布前 `revalidate` 校验切片仍存在、版本/类型/有效期/父块一致。

### 6.5 会话、SSE 与恢复

- Cookie `kb_owner` → `owner_hash` 绑定 `query_sessions`。
- `reserve_query` 保证同一会话同时只有一个查询。
- SSE 事件：`ready`、`progress`、`delta`、`final`、`error`、`close`。
- 页面刷新：先读 `/api/query/status/{sid}`，再读历史；进行中则重建进度并重连 SSE。
- 查询指标写入 `query_runs`；反馈写入 `query_feedback` 与 `fb_events`。

## 7. 自进化子系统

### 7.1 数据流

```text
用户反馈 / 未解决信号
        ↓
   fb_events
        ↓ 调度器扫描（游标升序，去重）
   k_gaps（缺口分级）
        ├─ 非知识缺失 → query_issues（策略待办）
        └─ 知识缺失 → k_candidates
                          ├─ need_info（待人工补充）
                          └─ draft → 审批
                                        ↓
                              publication_tasks
                                        ↓
                              kb_evolution_items
                                        ↓
                                查询召回 + 指标 + 回测
```

### 7.2 缺口

- 信号：用户差评、检索未命中、生成未达标。
- 权重：用户 0.4、检索 0.3、生成 0.3；强 0.65、弱 0.45。
- 分类：`retrieval_miss`、`reading_miss`、`generation_error`、`knowledge_missing`。
- 去重：会话 + 产品 + 问题；窗口 1 小时；已忽略问题不再登记。
- 状态：`pending`、`candidate`、`processing`、`strategy_pending`、`reviewing`、`resolved`、`rejected`、`dismissed`。

### 7.3 候选

`app/evolution/candidate/generator.py`：

- 只处理 `knowledge_missing`。
- 无检索证据 → `need_info`。
- LLM 生成 JSON，答案 60~300 字。
- 疑似“无信息”答案 → `need_info`。
- PII 命中或问题重复 → 留痕，不进入可审批队列。
- 状态：`draft`、`need_info`、`active`、`rejected`、`hold`、`deprecated`。
- 发布状态：`draft`、`indexing`、`active`、`deprecated`、`revoked`。

### 7.4 审批与发布

`app/evolution/governance.py`：

1. 校验候选存在、状态可审批、答案含事实、来源可验证。
2. 状态置 `indexing`，计算内容哈希，写入发布任务队列。
3. 消费发布任务：校验租约、内容哈希、来源；写入 `kb_evolution_items`；候选置 `active`。
4. 失败按指数退避重试；任务可恢复。
5. 驳回/下架：撤销发布状态，删除索引记录。
6. 编辑：内容版本 +1，撤销旧索引，重新审批后发布。

发布任务使用租约，避免多实例并发发布同一条知识。

### 7.5 召回与保护

- 已审批 FAQ 进入 RRF，权重 `EVOLUTION_RRF_WEIGHT`。
- 若 FAQ 未被 RRF Top-N 收录，强制并入，最多 `EVOLUTION_RECALL_LIMIT` 条。
- 重排阶段保留自进化权威条目，避免被普通切片挤出。
- `is_retrievable` 要求状态、发布状态、来源版本、内容哈希全部一致。

### 7.6 指标、调参与回测

- `k_metrics`：采纳率、缺口率、参数快照。
- 自调：`RRF_K`、`RRF_TOP`、`RERANK_TOP_K`；有上下限；样本 ≥ 100 且采纳率变化明显才动作。
- 回测：观察窗内统计 FAQ 命中、采纳、拒绝；命中不足只观察；拒绝率超限下架。
- 问题：同一会话/产品/问题的未完成登记合并；支持负责人与处理记录。
- 试用指标：解决率、重复提问率、转人工率、知识补全时长、各路由耗时。

## 8. API 设计

### 8.1 统一约定

- 错误响应体：`{"code": "<机器码>", "message": "<中文提示>"}`。
- 业务错误使用 4xx；依赖不可用使用 503；未处理异常统一 500。
- 写接口要求 `X-Internal-Token`。
- 查询与会话接口依赖浏览器归属 Cookie。
- 文件上传为 multipart；其余为 JSON。

### 8.2 鉴权矩阵

| 范围 | 机制 | 失败行为 |
|---|---|---|
| 导入 | `IMPORT_ADMIN_TOKEN` | 未配置 503；不一致 401 |
| 审批 | `EVOLUTION_ADMIN_TOKEN` | 未配置 503；不一致 401 |
| 会话 | `kb_owner` Cookie → `owner_hash` | 无归属 404；存储异常 503 |
| 页面 | 无 | 静态资源白名单 |

## 9. 横切设计

### 9.1 配置

- 唯一读取环境变量入口：`app/shared/config/common.py`。
- 业务模块只使用 `settings.<domain>`。
- `.env` 以 `override=True` 加载。

### 9.2 日志

- 基于 loguru；控制台与文件双通道。
- 节点日志记录开始、耗时、异常堆栈。
- 步骤日志为 DEBUG 级，避免刷屏。
- 查询指标单独落库，不依赖日志。

### 9.3 超时与重试

- `retry_call` 默认只重试 `OSError`；业务错误转成非 OSError，避免误重试。
- MinerU 申请、上传、轮询各有超时。
- Milvus 每次 RPC 都传 `timeout`，并与请求剩余时间取较小值。
- Mongo 使用 `pymongo.timeout` 绑定请求预算。
- Agentic 总预算用 `ContextVar` 传递，线程池中不泄漏。

### 9.4 并发

- 导入：单 worker 用进程内信号量；多 worker 用 Redis `import:slots` 全局名额。
- 查询：每会话互斥；单 worker 用进程内字典，多 worker 用 Redis `query:active:<id>`。
- SSE：单 worker 用进程内队列；多 worker 用 Redis Stream，支持断线回放。
- 意图识别：独立线程池 + 信号量。
- 图片说明：线程池，上限可配。
- 发布任务：Mongo 租约。
- 缺口处理：Mongo 租约与领取令牌。
- 自进化调度器：多 worker 用 Redis 主节点租约，保证只有一个实例执行周期任务。

### 9.5 幂等与一致性

- 子块确定性主键 + upsert。
- 主体名先插后删。
- 发布任务唯一键 + 租约。
- 缺口来源唯一约束。
- 对账只读，不阻断主流程。

### 9.6 安全

- 文件头嗅探，防改后缀。
- 路径只取 basename，防穿越。
- 任务 ID 必须是 UUID，防目录穿越。
- Milvus 过滤值统一转义。
- 模型输出严格 Schema 校验。
- 引用 URL 仅 http/https。

## 10. 测试策略

| 层 | 位置 | 特点 |
|---|---|---|
| 离线单测 | `tests/unit/` | 默认执行；`offline_guard` 禁止真实外部连接 |
| 真机端到端 | `tests/e2e/` | `E2E_ENABLED=1`；真实 Milvus/Mongo/MinIO/模型；测试数据带前缀并清理 |
| 前端脚本 | `tests/frontend/` | `node:test`；用 VM 模拟 DOM 与存储 |
| 静态守卫 | `tests/unit/test_frontend_pages.py` | 页面不得内联脚本/样式；资源必须带指纹 |

默认命令：

```bash
uv run pytest
node --test tests/frontend
E2E_ENABLED=1 uv run pytest -m e2e tests/e2e -s
```

## 11. 扩展点

| 需求 | 扩展位置 |
|---|---|
| 新增导入格式 | `entry_service`、导入图分支、解析服务 |
| 新增检索工具 | `app/rag/agentic/tools.py` 与 `Action.tool` 枚举 |
| 新增业务策略 | `app/rag/query/business.py` |
| 新增闭环指标 | `app/evolution/online_eval/` 与 `k_metrics` |
| 新增自调参数 | `app/evolution/tuning/controller.py` 与 `param_registry` |
| 新增页面 | `app/resources/` + `pages.py` 白名单 |
| 替换 LLM/向量模型 | `app/shared/models/` |

## 12. 已知限制

1. 多 worker 依赖 Redis；未配置时自动退回单 worker。
2. 导入任务目录与磁盘快照在本地 `output/`；多机部署需要共享存储。
3. 每个 worker 独立加载 BGE-M3 与重排模型，内存按 worker 数增长。
4. Milvus / Mongo / MinIO 无事务；只能依靠幂等与对账。
5. 本地模型在 CPU 上较慢；首次启动需要预热。
6. 鉴权为共享令牌 + Cookie 归属，不是企业级账号体系。
7. 联网检索依赖外部 MCP 服务，未配置时自动降级。
