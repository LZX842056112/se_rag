# 技术讲解与运维手册（TECHNICAL_GUIDE）

本文从代码出发，说明系统如何完成一次导入、一次问答和一次知识闭环，并给出运维与排障步骤。产品需求见 `docs/PRD.md`，模块与数据契约见 `docs/CODE_DESIGN.md`。

## 1. 全链路总览

```text
PDF/Markdown
   ↓ 上传
导入服务 → MinerU → 图片增强 → 结构切分 → 主体识别 → 向量化
   ↓
Milvus（子块/主体名/FAQ） + Mongo（父块/任务/反馈） + MinIO（图片）

用户提问
   ↓
意图识别 → 主体确认 → 检索（legacy / agentic） → 证据校验 → 作答
   ↓
引用、接地性、历史、SSE

反馈 / 未解决信号 → 缺口 → 候选 → 审批 → 发布 → 召回 → 指标/回测/调参
```

两个进程：

| 进程 | 端口 | 主要职责 |
|---|---|---|
| 导入服务 | 8000 | 上传、解析、切分、向量化、入库 |
| 问答服务 | 8001 | 检索问答、会话、反馈、自进化、审批 |

## 2. 一次导入的完整过程

### 2.1 上传与入口校验

`POST /api/import/upload`：

1. 校验请求头 `X-Internal-Token`。
2. 一次最多接收 20 个文件；任一文件不合法则整批拒绝。
3. 只接受 `.md` / `.pdf`。
4. 检查文件大小上限。
5. PDF 检查 `%PDF-` 文件头；Markdown 检查 UTF-8 与空字节。
6. `policy` 类型必须填写版本号和未来失效时间。

通过后：

- 逐文件生成 UUID `task_id`，各建一个任务目录 `output/<YYYYMMDD>/<task_id>/`。
- 保存原文件。
- 写入 `_import_meta.json`：原文件名、资料类型、版本、失效时间。
- 返回 `task_ids`（与文件顺序一致），每个任务在后台独立导入。
- 落盘中途失败时，清理本批已建任务与目录，整批返回 `save_failed`。

### 2.2 任务执行

上传后，任务先置为 `pending`，再提交到进程内线程池。线程池按 `IMPORT_MAX_CONCURRENT_TASKS` 并行执行。超出上限的任务在线程池队列中排队。

`invoke_import_graph`：

1. 抢导入并发名额。
2. 状态置 `processing`，写磁盘快照。
3. 构造 `ImportGraphState`。
4. 运行导入图；失败按错误码判断是否重试。
5. 成功置 `completed`；失败置 `failed`，记录 `error_code` 与中文原因。

导入错误码：

| 错误码 | 含义 | 是否重试 |
|---|---|---|
| `mineru_timeout` | MinerU 超时 | 是 |
| `mineru_failed` | MinerU 任务失败 | 是 |
| `storage_unavailable` | 存储不可用 | 是 |
| `internal_error` | 未归类异常 | 是 |
| `unsupported_type` | 文件类型不支持 | 否 |
| `parse_failed` | 解压后没有 Markdown | 否 |
| `split_empty` | 切分后为空 | 否 |
| `index_count_mismatch` | 对账不符 | 否 |
| `embedding_failed` | 向量生成失败 | 否 |
| `item_name_failed` | 主体识别失败 | 否 |
| `index_failed` | 写入失败 | 否 |
| `import_busy` | 并发名额等待超时 | 否 |

### 2.3 PDF 转 Markdown

`parse_pdf_to_markdown`：

1. 校验 PDF 路径与工作目录。
2. 若开启断点续传，且 `<stem>.md` 比 PDF 新，直接复用。
3. 申请 MinerU 上传地址。
4. 上传 PDF。
5. 轮询任务状态：
   - `done`：取 `full_zip_url`。
   - `failed`：抛出带 `err_msg` 的失败。
   - 其他：继续等待，最长 600 秒。
6. 下载 zip，解压到 `<stem>/`。
7. 找主 Markdown：
   - 与文件同名优先；
   - 否则 `full.md`；
   - 再否则第一个 Markdown。
8. 重命名为 `<stem>.md`。

模型选择：

- 首选 `vlm`。
- 若任务判定失败，自动改用 `pipeline`。
- 原因：A3 单页排版稿等超大页面会让 `vlm` 连续失败，而 `pipeline` 可以解析。

### 2.4 图片增强

`enrich_markdown_images`：

1. 读取 Markdown。
2. 扫描图片引用。
3. 取图片前后各 200 字符作为上下文。
4. 调用视觉模型生成中文说明。
5. 上传图片到 MinIO：`<image_dir>/<stem>/<image>`。
6. 把引用替换为 `![说明](公网地址)`。
7. 保存为 `<stem>_new.md`，并更新后续节点使用的 `md_path`。
8. 清理本次未再引用的旧图片。

图片说明让图片内容可被文本检索；公网地址让答案可以展示图片。

### 2.5 结构解析与切分

`split_document`：

1. 读取 Markdown，统一换行符。
2. `resolve_blocks`：
   - 优先读 `*_content_list.json`；
   - 缺失或损坏时回退 Markdown 解析；
   - 两条路径产出同构 `Block`。
3. `build_sections`：
   - 按标题层级建树；
   - 记录 `heading_path` 与同名标题的 `occurrence`；
   - 每个标题产出一个父块，正文为整棵子树文本。
4. `split_section_children`：
   - 只使用本章节自身的原子块；
   - 段落、表格、代码、图片按 `CHUNK_SIZE` 贪心打包；
   - 单块超过 `CHUNK_MAX_SIZE` 才从中间切；
   - 切分时保留 `CHUNK_OVERLAP`；
   - 短于 `CHUNK_MIN` 的正文尝试与后续块合并。
5. 表格：HTML 转 Markdown；超长表格分块时复制表头与分隔行。
6. 代码：只按行切。
7. 图片：使用增强后的 Markdown 引用。

长度口径：

- 所有阈值按“正文”计算，不含前置标题。
- `CHUNK_SIZE` 是软目标，不是硬切点。
- 单个原子块落在 `[CHUNK_SIZE, CHUNK_MAX_SIZE]` 时不切。

确定性 ID：

```text
doc_id    = sha1(规范化文件名)
parent_id = sha1(doc_id#heading_path#occurrence)
chunk_id  = sha1(doc_id#heading_path#occurrence#part)
```

因此重复导入不会产生新 ID；内容变化由 `content_hash` 记录。

### 2.6 主体识别

`recognize_and_index_item_name`：

1. 取前 10 个切片。
2. LLM 识别主体名。
3. 为空时用文件标题兜底。
4. 在主体目录中做同一实体归并。
5. 回填所有子块与父块的 `item_name`。
6. 写入 `kb_item_names`：
   - 先插入新记录并取回自增 pk；
   - 再删除同一 `file_title` 下其他 pk；
   - 避免“先删后插”造成主体名丢失。

主体名口径在导入端与查询端共用，保证“导入时归并到什么名字，查询时就能确认到什么名字”。

### 2.7 向量化

`generate_chunk_embeddings`：

- 批量大小默认 6。
- 向量化文本：`主体:<item_name>,内容:<content>`。
- 输出 `dense_vector` 与 `sparse_vector`。
- 与查询端构造方式一致，避免语义偏移。

### 2.8 写入与对账

`index_chunks`：

1. 集合不存在时创建。
2. 对齐 schema：
   - 按 UTF-8 字节截断 VARCHAR；
   - 补默认值；
   - 忽略未知字段。
3. 分批 `upsert`。
4. 删除该文档本次未再产出的旧块。
5. 父块写入 Mongo。
6. 跨存储对账：
   - Milvus：按 `chunk_id` 集合比对；
   - Mongo：比对父块；
   - MinIO：只统计对象数，不做判定。

对账只读，不阻断导入。Milvus 刚写入时可能不可见，对账会重试并在第二次尝试前 `flush`。

## 3. 一次 Legacy 问答的完整过程

### 3.1 入口

`POST /api/query`：

1. 校验会话归属。
2. 预留会话查询名额；同会话只允许一个问题。
3. 非流式同步执行；流式立即返回 `session_id`，后台执行。

### 3.2 意图识别

- LLM 二分类 `rag` / `chitchat`。
- 超时 2 秒；异常、非法 JSON、未知枚举一律兜底为 `rag`。
- 闲聊直接作答，不进入检索。

### 3.3 主体确认

优先级：

1. `rewrite_hint`：点选主体时，用主体名 + 意图原句确定性拼装。
2. 上一轮反问且本轮输入是候选主体名：用挂起意图复原。
3. 否则：历史 + LLM 改写 + 主体目录匹配。

未确认主体：

- 有候选：返回可点选选项。
- 无候选：返回补充说明或兜底。

### 3.4 三路召回

1. **向量检索**：BGE-M3 混合检索，dense/sparse 权重 0.6 / 0.4；主体过滤；候选 10、返回 5。
2. **HyDE**：LLM 生成假设答案，拼接后检索；短事实型问题可跳过。
3. **联网**：MCP 搜索；定价/售后禁用。

### 3.5 融合与重排

RRF：

```text
score = Σ weight_i / (k + rank_i)
k = 60，Top = 5
```

- 向量路与 HyDE 路权重 1.0。
- 自进化路权重 `EVOLUTION_RRF_WEIGHT`。
- 已审批 FAQ 若未进入 Top-N，强制并入，最多 `EVOLUTION_RECALL_LIMIT` 条。

重排：

- BGE-Reranker 打分。
- 动态截断：保留 2~6 条。
- 分数差距过小或绝对分过低时少保留。
- 联网结果最多 2 条进入最终上下文。
- 本地知识优先；自进化权威条目有保底。

### 3.6 父块回溯

- 只对本地知识库子块生效。
- 按重排分数降序分配预算。
- 单父块最多 1200 字符，总预算 4000 字符。
- 父块太长时从头部截断，保留定义与前提。
- 同一父块只注入一次。

### 3.7 作答

1. 拼接上下文：标题、来源、分数、页码、章节背景、正文。
2. 业务策略：
   - 投诉 → 转人工；
   - 价格/售后 → 只保留当前有效政策；
   - 无有效政策 → 兜底并转人工。
3. 调用 LLM；流式逐段推送。
4. 提取答案图片。
5. 回填引用、页码、章节、版本。
6. 计算接地性；无法计算时保持 `None`，前端显示“未评估”。
7. 落库助手消息。
8. 推送 `final` 与 `close`。

## 4. Agentic 问答

### 4.1 预算

| 预算 | 默认 | 超出行为 |
|---|---|---|
| 工具调用 | 4 次 | `tool_budget` |
| 联网调用 | 1 次 | `web_budget` |
| 上下文 token | 8000 | `context_budget` |
| 总时长 | 60 秒 | `deadline` |

补充规则：

- 同一工具同一参数不重复执行。
- 连续两轮没有新证据则结束。
- 时长取“配置值”和“外层剩余时间”的较小者。
- 请求预算通过 `ContextVar` 传递；Mongo RPC 使用同一预算。

### 4.2 状态机

```text
bootstrap
   ↓
assess（覆盖度：supported / missing / conflicting）
   ├─ supported 且证据已读 → draft → verify
   │                          ├─ 通过 → answered
   │                          └─ 不通过 → draft（一次）→ 兜底
   └─ 其他 → select → execute → assess
```

各阶段：

| 阶段 | 输入 | 输出 |
|---|---|---|
| `bootstrap` | 改写后的问题 | 首轮语义检索证据 |
| `assess` | 问题 + 证据 | 覆盖度 + 引用的证据 ID |
| `select` | 问题 + 证据 + 轨迹 + 覆盖度 | 下一步动作 |
| `execute` | 动作 | 新证据 + 轨迹 |
| `draft` | 最终证据 + 答案提示词 | 草稿答案 |
| `verify` | 草稿 + 证据 | 是否支持 |

动作：

| 动作 | 说明 |
|---|---|
| `search` | 继续检索 |
| `read` | 必须是 `read_chunk` |
| `answer` | 仅覆盖度 supported 且有最终证据时允许 |
| `clarify` | 反问 |
| `handoff` | 转人工 |

### 4.3 关键规则

1. **证据可否定**：证据明确说明“不支持该功能”或“无法执行该操作”时，评估应判 `supported`。不能因为问题预设了能力就判 `missing`。
2. **终止动作容错**：`clarify` / `handoff` 不需要 `tool` 与 `arguments`；模型补空 `tool` 时会被丢弃，不消耗修复次数。
3. **动作校验**：严格 JSON Schema；非法动作允许修正一次。
4. **发布前复核**：`draft` 与 `verify` 通过后都会再次 `revalidate`；来源撤回、版本变化或正文变化会阻止发布。
5. **失败关闭**：任何异常、超时、非法动作、证据失效都终止为兜底，不输出未核验内容。

### 4.4 工具与权限

| 工具 | 访问 | 约束 |
|---|---|---|
| `semantic_search` | `kb_chunks` | 主体过滤；BGE-M3 混合检索 |
| `keyword_search` | `kb_chunks` | 有界扫描；超过 500 条报错 |
| `read_chunk` | `kb_chunks` | 读指定块、邻居、父块 |
| `search_approved_faq` | `kb_evolution_items` + Mongo | 发布状态与来源校验 |
| `web_search` | MCP | 定价/售后禁用 |

主体范围：

- 服务端先确认 `item_names`。
- 模型只能在范围内收窄。
- 逐个校验每个请求的主体名。
- 相似匹配不能授权其他产品。

### 4.5 证据

```text
evidence_id = 来源:标识:版本哈希:正文哈希
```

- 正文哈希覆盖父块 + 子块。
- 模型只看到可见副本，不看到内部元数据。
- `revalidate` 校验切片仍存在、版本/类型/有效期一致、父块正文一致、FAQ 仍 active。
- 联网证据保留为带时间戳的检索快照。

### 4.6 终止原因

| 原因 | 含义 |
|---|---|
| `answered` | 核验通过 |
| `deadline` | 总时长用尽 |
| `tool_budget` | 工具调用次数用尽 |
| `repeated_action` | 重复动作 |
| `web_budget` | 联网次数用尽 |
| `context_budget` | 上下文 token 超限 |
| `no_new_evidence` | 连续两轮无新证据 |
| `invalid_action` | 动作两次非法 |
| `model_or_tool_error` | 模型或工具异常 |
| `unsupported_answer` | 修复后仍无法核验 |
| `stale_evidence` | 证据失效 |
| `cancelled` | 客户端取消 |
| `business_policy` | 投诉类直接走策略 |

### 4.7 冷启动

问答服务启动时会：

1. 加载 BGE-M3。
2. 加载 BGE-Reranker。
3. 加载完成后才开始接收请求。

原因：Agentic 的 60 秒预算从请求开始计时。若模型在请求内首次加载，预算会被冷启动耗尽，后续检索与作答全部抛 `deadline exceeded`。

## 5. 自进化闭环

### 5.1 反馈

- 用户点赞、点踩、解决/未解决。
- 读链路的未解决信号也会写入。
- 反馈带 `session_id`、`answer_id`、问题、引用、主体、业务标签。

### 5.2 缺口

调度器按时间游标升序扫描，避免旧信号被新信号淹没。

信号权重：

```text
confidence = 0.4 × user + 0.3 × retrieval + 0.3 × generation
```

| 等级 | 条件 |
|---|---|
| strong | `>= 0.65` |
| weak | `>= 0.45` |
| none | 其他 |

分类：

| 类型 | 触发 |
|---|---|
| `knowledge_missing` | 明确没有来源 |
| `generation_error` | 证据充足但答案未达标 |
| `reading_miss` | 有读过的切片但仍未作答 |
| `retrieval_miss` | 没有有效召回 |

只有 `knowledge_missing` 会生成知识候选；其他类型转策略问题。

去重：

- 会话 + 产品 + 问题。
- 1 小时窗口。
- 已忽略的问题不再登记。
- 来源反馈键有唯一约束。

### 5.3 候选

- 从缺口与会话提炼标准 FAQ。
- 无证据或疑似“无信息”答案 → `need_info`。
- PII 命中或问题重复 → 留痕，不进入可审批队列。
- 管理员可编辑；补齐事实后自动回到 `draft`。

### 5.4 审批与发布

1. 校验来源、答案事实、状态、发布状态。
2. 置 `indexing`，计算内容哈希。
3. 写入发布任务队列。
4. 消费任务：
   - 校验租约与内容哈希；
   - 写入 `kb_evolution_items`；
   - 候选置 `active`；
   - 标记来源缺口 `resolved`。
5. 失败按指数退避重试。
6. 驳回/下架：撤销发布，删除索引记录。

### 5.5 召回保护

- 已审批 FAQ 参与 RRF。
- 未进入 Top-N 时强制并入。
- 重排阶段保留权威条目。
- 检索时再次校验发布状态与内容哈希。

### 5.6 指标与治理

- 每次查询写 `query_runs`。
- 定时写 `k_metrics`：采纳率、缺口率、参数快照。
- 自动调参只调整 `RRF_K`、`RRF_TOP`、`RERANK_TOP_K`，有上下限。
- 回测：
  - 命中不足：观察；
  - 拒绝率超限：下架；
  - 采纳率达标：保留；
  - 其余：继续观察。

## 6. 超时、降级与错误矩阵

### 6.1 查询错误码

| 错误码 | 触发 |
|---|---|
| `milvus_unavailable` | Milvus 异常 |
| `mongo_unavailable` | Mongo 异常 |
| `rerank_failed` | 重排失败 |
| `embedding_failed` | 向量生成失败 |
| `llm_timeout` | LLM 超时或请求预算耗尽 |
| `llm_failed` | LLM 其他错误 |
| `query_failed` | 未归类 |

### 6.2 降级行为

| 故障 | 降级 |
|---|---|
| Milvus 检索失败 | 返回空召回，继续作答 |
| 联网失败 | 只用本地知识 |
| 重排失败 | 保留融合结果或降级排序 |
| 历史读取失败 | 历史为空，继续作答 |
| 自进化关闭 | 不召回 FAQ、不写缺口/候选 |
| 发布失败 | 任务保留，可重试 |
| 父块缺失 | 不注入章节背景 |

### 6.3 预算耗尽的连锁表现

Agentic 预算耗尽后会出现一组“看似多处故障”的日志：

- Milvus 报 `deadline exceeded`；
- Mongo 报剩余时间为负；
- 主体目录降级；
- LLM 客户端创建时抛 `TimeoutError`；
- 最终错误码为 `llm_timeout`。

这些不是外部服务故障，而是同一个请求预算已经用完。排障时先看最早的时间戳与剩余时间。

## 7. 运维手册

### 7.1 启动顺序

1. 确认 Mongo、Milvus、MinIO、LLM、MinerU 可用。
2. 启动导入服务。
3. 启动问答服务。
4. 等待“检索模型预热完成”。
5. 打开三张页面并填写令牌。

### 7.2 健康检查

```bash
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8001/api/health
```

返回 `code`、`message`、`asset_version`。`asset_version` 用于前端识别旧页面。

### 7.3 令牌

- 导入：`IMPORT_ADMIN_TOKEN`。
- 审批：`EVOLUTION_ADMIN_TOKEN`。
- 生产环境使用强随机值，不要使用代码内置默认值。
- 令牌只保存在当前标签页会话。

### 7.4 导入任务

- 查看状态：`GET /api/import/status/{task_id}`。
- 重试：`POST /api/import/retry/{task_id}`。
- 删除：`DELETE /api/import/records/{task_id}`。
- 磁盘快照：`output/tasks/<task_id>.json`。
- 工作目录：`output/<YYYYMMDD>/<task_id>/`。

删除记录不会删除已入库向量。如需撤回知识，请走候选下架。

### 7.5 缺口与候选

- 缺口列表：审批页“缺口处理”。
- 人工确认知识缺失：生成待补充候选。
- 人工忽略：同一会话、同一产品、同一问题不再自动登记。
- 候选列表：审批页“知识候选”。
- 候选审批：通过、驳回、编辑、下架。

### 7.6 日志

- 控制台与文件双通道；文件开关 `LOG_FILE_ENABLE`。
- 关键节点：导入各节点、查询各阶段、审批发布、调度器。
- 排查预算问题：搜索 `deadline exceeded`，比较首条与末条时间。

### 7.7 备份与清理

- Mongo：备份会话、反馈、缺口、候选、指标、父块。
- Milvus：备份三个集合。
- MinIO：图片按 `<image_dir>/<stem>/` 组织。
- `output/` 可按日期清理；删除记录时会自动清理对应任务目录。

## 8. 典型排障案例

### 案例 1：PDF 导入失败，日志只显示 batch_id

**现象**：页面显示“失败”，日志只有 `MinerU 解析任务失败：batch_id=...`。

**原因**：MinerU 服务端重试 5 次后放弃；原代码丢弃了 `err_msg`。

**处理**：

1. 代码已改为带 `err_msg`。
2. 对 A3 单页排版稿，`vlm` 失败后自动回退 `pipeline`。
3. 仍失败时，把 PDF 拆成常规页面后重新上传。

### 案例 2：首次问答报 `agentic query deadline exceeded`

**现象**：Milvus、Mongo、主体目录、LLM 同时报超时/降级。

**原因**：请求预算 60 秒被模型冷启动耗尽。

**处理**：

1. 重启问答服务，等待“检索模型预热完成”。
2. 仍不足时增大 `AGENT_TIMEOUT_SECONDS`。
3. 有 GPU 时设置 `BGE_DEVICE=cuda`、`BGE_FP16=true`。

### 案例 3：已审批 FAQ 已入库，但回答“证据不足”

**现象**：FAQ 状态为 active，检索工具单独调用能命中，但问答仍兜底并生成缺口。

**原因**：

1. 评估模型把“不能连蓝牙”这类否定答案判成 `missing`。
2. 后续动作输出非法，修复次数用尽，终止为 `invalid_action`。

**处理**：

1. 评估提示词已明确否定结论按 `supported`。
2. 终止动作会丢弃无意义的 `tool` 字段。
3. 重启问答服务。

### 案例 4：Milvus 召回为 0，日志报 metric 不匹配

**原因**：`kb_chunks` dense 索引为 COSINE，检索请求用了 IP。

**处理**：保持 `CHUNK_DENSE_METRIC=COSINE`，与导入端一致。BGE-M3 稠密向量已归一化，COSINE 与 IP 排序等价。

### 案例 5：会话 404

**原因**：浏览器没有 `kb_owner` Cookie，或会话归属不一致。

**处理**：新建会话；不要仅凭 `session_id` 访问他人会话。

### 案例 6：导入一直处于 processing

**原因**：MinerU 轮询中、模型排队、或并发名额等待。

**处理**：查看节点进度与日志；等待 600 秒超时；必要时重试或删除记录。

## 9. 评估与实验

### 9.1 切分评估

- `app/rag_eval/dataset.py`
- `app/rag_eval/split_metrics.py`
- `app/rag_eval/split_dataset.py`

用于构建切分数据集、计算切分指标、对比不同切分策略。

### 9.2 答案质量

```bash
uv run python -m app.rag_eval.answer_quality \
  --cases app/rag_eval/artifacts/eval_cases.json \
  --output output/answer-quality-report.json
```

可选参数：`--allow-external`、`--allow-unreviewed`、`--snapshots`、`--baseline`、`--limit`。

### 9.3 Agentic 对比

```bash
uv run python -m app.rag_eval.agentic_comparison \
  --output output/agentic-comparison.json
```

用于对比 legacy 与 agentic 的答案质量与成本。

### 9.4 全流程实验

```bash
uv run python -m app.rag_eval.full_flow --base-url http://127.0.0.1:8001
```

### 9.5 隔离联调

```bash
uv run python -m app.rag_eval.ui_trial prepare --run ui_test_YYYYMMDD_xxxxxxxx
```

命令：`prepare`、`serve-query`、`serve-import`、`inspect`、`cycle`、`e2e`、`configure-images`。

隔离资源使用独立的 Mongo 库、Milvus 集合、MinIO 桶；代码会校验资源名，拒绝连接非隔离资源。

## 10. 性能与容量

| 环节 | 主要成本 | 优化方向 |
|---|---|---|
| MinerU 解析 | 云端排队与文件复杂度 | 复用已有产物；拆分超大页面 |
| 图片增强 | 视觉模型调用 | 并发上限；无图片时跳过 |
| 向量化 | CPU/GPU 推理 | GPU；批大小；模型量化 |
| Milvus 写入 | 批量与索引 | 控制批大小；避免重复导入 |
| 查询召回 | 向量化 + 检索 | 同一次提问复用一份向量 |
| 重排 | 交叉编码器 | 限制输入条数与长度 |
| Agentic | 多次模型调用 | 工具预算；上下文预算；重排保护 |

容量建议：

- 单条切片控制在 600~1000 字，避免超长正文。
- 单文档图片数量过多时，图片增强会成为瓶颈。
- 查询服务单 worker；CPU 模型下并发能力有限。
- 导入并发默认 2，按机器资源调整。

## 11. 安全与审计

- 文件入口做文件头嗅探，防改后缀。
- 路径只取 basename，任务 ID 必须是 UUID。
- Milvus 过滤表达式统一转义。
- 模型输出严格 Schema 校验；非法即终止。
- 证据带哈希；发布前复核。
- 引用 URL 仅 http/https。
- 审批与导入写操作需要令牌。
- 会话归属使用 Cookie 哈希，不使用 `session_id` 作为凭据。

审计链：

```text
答案 → 引用 chunk_id → 文档 file_title / doc_id
     → 页码 / heading_path / document_version
     → 审批候选 → 来源缺口 → 反馈事件
```
