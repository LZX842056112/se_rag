# 浏览器真实环境全流程联调验证报告（2026-09-28）

> 验证对象：当前工作区（含本日重构改动）。运行方式：真实内置浏览器 + 真实 Milvus / MongoDB /
> MinIO / DashScope，不使用 mock。测试数据一律带 `e2e_` 前缀，验证结束后精确清理。

## 1. 环境与工具

| 项 | 值 |
| --- | --- |
| 查询服务 | `http://127.0.0.1:8001`（uvicorn，日志 `logs/verify-query.*.log`） |
| 导入服务 | `http://127.0.0.1:8000`（uvicorn，日志 `logs/verify-import.*.log`） |
| 浏览器 | Codex 内置浏览器（IAB），**全程可见** |
| 浏览器能力 | Playwright 定位器、`filechooser.setFiles()` 真实上传、`dev.logs()` 控制台、`screenshot()` 留证、`getJsDialog()` 处理 `confirm` |
| 外部依赖 | Milvus `192.168.200.10:19530`、MongoDB `kb002`、MinIO `knowledge-base-files`、DashScope Qwen |
| 测试文档 | `output/verify_ui/e2e_ui_0928171640.md`（唯一主体名 + 唯一事实：`12~28 摄氏度` / `55W` / `220V`） |
| 原生电脑控制 | 本会话 `cua.listApps` 不可用，未使用；验证全部在浏览器真实运行环境完成 |

## 2. 结果总览

| 流程 | 结果 |
| --- | --- |
| P1 导入页（md 上传 / 不支持类型拒绝 / 落库校验） | PASS |
| P2 问答页（流式 / 非流式 / 引用 / 置信度 / 反馈 / 历史回显 / 清空） | PASS（历史回显先失败 → 修复后 PASS） |
| P3 反馈与缺口（点踩 → 缺口扫描） | PASS（先复现缺陷 D1 → 修复后 PASS） |
| P4 审批后台（筛选 / 鉴权 / 编辑 / 通过 / 驳回） | PASS |
| P5 自进化回流（审批通过的知识被后续提问命中并作答） | PASS |
| 控制台 / 服务端日志 | 三页均无新增 error；服务端无未处理异常 |
| 数据清理 | 自建数据 0 残留，生产数据未受影响 |

## 3. 逐流程验证明细

### P1 导入页

| 验证项 | 预期 | 实际 | 证据 |
| --- | --- | --- | --- |
| 空态 | 显示「暂无导入任务」 | 一致 | 页面 DOM |
| 上传 md | 徽标 `上传中…→处理中…→已完成`，进度日志随轮询更新 | 一致：`日志（已完成7，进行中0）`，7 个节点依次完成 | 截图 `verify-01-import.png`；节点：开始上传文件 / 检查文件 / Markdown图片处理 / 文档切分 / 主体名称识别 / 向量生成 / 导入向量库 |
| 落库 | Milvus 可按 `file_title` 查到切片与主体名 | 一致：`kb_chunks=3`、`kb_item_names=1`（item_name = `e2e_ui_0928171640`） | 终端回查 |
| 上传 .txt | 入口拒绝，徽标 `失败` + 明确原因 | 一致：`失败`（`status-error`）+ `仅支持 md / pdf 格式文件` | 截图 `verify-05-import-reject.png` |
| 控制台 | 预期业务拒绝不应产生 error | 修复后：warn +1 / error +0（修复前为 error 日志，见 D2） | `dev.logs()` 计数 |

### P2 问答页

| 验证项 | 预期 | 实际 | 证据 |
| --- | --- | --- | --- |
| 健康状态 | `API: 已连接` | 一致 | 页面顶部 pill |
| 流式问答 | delta 逐字渲染 → final 收起进度 → 答案含唯一事实 + 引用 + 置信度 + 反馈栏 | 一致：答案 `…额定工作温度范围是 12~28 摄氏度。`；`引用来源（1）`（标签 `知识库`）；`回答置信度 100%` | 截图 `verify-02-chat-answer.png` |
| 非流式问答 | 关闭「流式输出」后直接返回完整答案 | 一致：`…额定功率为 55W。`，引用与反馈同样渲染 | 截图 `verify-06-nonstream.png` |
| 反馈按钮 | 点 👎 后置灰并标记「已反馈」 | 一致：两枚按钮均 disabled，标记 `已反馈` | 截图 `verify-03-feedback.png`；Mongo `fb_events` 记录 `thumbs=-1` |
| 历史回显 | 刷新后历史消息、引用块、反馈栏完整恢复 | 修复后一致：3 条消息 + `引用来源（1）` + 反馈栏 | 截图 `verify-07-history.png`（修复前见 D6） |
| 清空对话 | 弹确认 → 清空后端与界面（保留欢迎语） | 一致：服务端删除 10 条记录；界面 5 → 1 条 | 截图 `verify-09-clear.png`；服务端日志 `已清空会话 … 的 10 条记录` |

### P3 反馈 → 缺口

| 验证项 | 预期 | 实际 | 证据 |
| --- | --- | --- | --- |
| 点踩落库 | 写入 `fb_events` | 一致：`thumbs=-1`、`adopt=None`、带 `cited_chunk_ids` | Mongo 回查 |
| 点踩进入缺口扫描 | 应产出 strong 缺口 | **修复前：不产出（缺陷 D1）**；修复后：隔离会话仅发一次点踩，75s 内产出 `k_gaps=1`（`confidence=0.7`，user=1.0 / retrieval=0.0 / generation=1.0） | Mongo 回查 + 调度日志 |
| 兜底话术路径 | 命中「无法作答」时写入未解决信号并生成候选 | 一致：问答页出现 `未查询到该问题相关信息`，随后审批页出现 draft 候选 | 审批页列表 |

### P4 审批后台

| 验证项 | 预期 | 实际 | 证据 |
| --- | --- | --- | --- |
| 列表与筛选 | 渲染候选与计数，按状态筛选生效 | 一致：`3 条`，`待审批 / 已入库 / 已驳回` 状态标签正确 | 截图 `verify-04-approval.png` |
| 鉴权（空 Token） | 拒绝写操作 | 一致：toast `通过失败：鉴权失败`（HTTP 401） | toast + 服务端 401 |
| 鉴权（错误 Token） | 拒绝写操作 | 一致：同上 | toast |
| 编辑候选 | 保存后内容更新 | 一致：toast `已保存，重新加载`，卡片答案已更新（`【联调验证 2026-09-28】…`） | 卡片文本 |
| 通过候选 | 状态转 `已入库` 并写入 Milvus | 一致：toast `已审批通过并入知识库`；Mongo `status=active`、`evo_doc_id=evo_89c8068174e6` | Mongo 回查 |
| 驳回候选 | 确认后状态转 `已驳回` | 一致：`confirm` 弹窗 accept → toast `已驳回` → 状态 `已驳回` | 截图 + Mongo |

### P5 自进化回流（闭环）

完整链路：**点踩/兜底 → 缺口 → 候选 → 人工编辑 → 审批通过 → 回流 Milvus `kb_evolution_items` → 后续提问命中并作答**。

| 验证项 | 预期 | 实际 | 证据 |
| --- | --- | --- | --- |
| 审批时写入进化条目 | `kb_evolution_items` 出现 `evo_doc_id` | 一致：`evo_89c8068174e6`、`evo_8ec6bd00f962` | Milvus 回查 |
| 回流被召回 | 后续提问引用出现 `自进化` 标签 | 一致 | 截图 `verify-08-evolution-recall.png`：引用标签 `自进化`、`evo_8ec6bd00f962`、置信度 100% |
| 回流内容被用于作答 | 答案包含审批时写入的知识 | 一致：答案 `…标准包装清单：主机 1 台、电源线 1 根、快速入门指南 1 本。`（与人工编辑后的候选答案逐字一致） | 同上截图 |

## 4. 发现的问题与处理

| 编号 | 问题 | 根因 | 处理 | 复验结果 |
| --- | --- | --- | --- | --- |
| D1 | 用户点踩不会被缺口扫描消费，自进化闭环缺一条入口 | `scan_unresolved_feedbacks` 只查 `{"adopt": False}`，而点踩事件 `adopt=None` | 过滤条件改为 `$or: [{"adopt": False}, {"thumbs": {"$lt": 0}}]`；新增 3 条离线用例（含点踩消费、点赞忽略、阈值分级） | 隔离会话仅发一次点踩 → 75s 内产出 strong 缺口与 draft 候选 |
| D2 | 导入页把预期业务拒绝（422）打成 `console.error`，污染错误监控 | 页面 catch 中直接 `console.error(error)` | 改为 `console.warn`（失败原因仍在页面日志区展示） | 重传 .txt：warn +1、error +0 |
| D3 | 查询侧主体抽取对「纯型号/编号」不稳定（同句 8 次采样中 3 次返回空），表现为「请您明确主体再提问」兜底话术 | 抽取提示词只要求「商品名称」，未覆盖型号/编号 | `rewritten_query_and_itemnames.prompt` 新增第 6 条：型号、编号、系列代号只要指向产品就必须提取 | 用原句 `e2e_ui_0928171640 的额定工作温度范围是多少？` 一次命中并正常作答 |
| D4 | 导入服务缺少健康检查路由（与查询服务口径不一致） | 健康路由只定义在查询服务 | 新增共享 `app/api/routers/health.py`，两服务统一 `/api/health`；路由契约测试同步 | 两服务 `/api/health` 均 200 |
| D6 | 刷新页面后历史不回显（提示「历史加载失败」） | 上一轮重构删除 `common.js` 后，chat 页仍裸调旧全局 `formatTime(ts)` → `ReferenceError`，且被 `catch(_)` 静默吞掉 | 补 `const formatTime = App.formatTime;`；catch 改为先 `console.error` 再提示；新增前端静态守卫测试（含「守卫自检」用例，确保能捕获该回归） | 刷新后 3 条消息 + 引用块 + 反馈栏全部恢复 |
| D5 | 审批页「驳回」与问答页「清空对话」使用原生 `confirm()`，模态会阻塞渲染进程（自动化点击超时、标签页可能直接卡死） | 浏览器原生模态对话框阻塞渲染进程 | 新增公共库 `App.confirm()` 页内确认浮层（`app.js` + `app.css`），两处改为 `await App.confirm(...)`；同时把 chat 页残留的 `alert()` 换成 `App.toast()`；新增「页面不得使用原生 confirm/alert/prompt」静态守卫测试 | 复验：驳回与清空均「无原生模态 + 页内浮层可见 + 点击不被阻塞 + 操作生效」，见截图 `verify-10-inpage-confirm.png`、`verify-11-inpage-reject.png` |
| D7 | 早前 `pytest -m e2e` 跑批在 Milvus `kb_item_names` 遗留 3 条 `e2e_sample_*` 记录 | 清理按 `file_title` 删除后立即结束，遇 Milvus 可见性延迟未复核 | 历史遗留已手工清除；并把 E2E 清理加固为「删除 → 等待 → 复核」，仍有残留则重试，最终残留会以断言失败显式暴露（`chunks`/`item_name`/进化条目三处都走该流程） | 加固后跑一次真机 E2E：跑批后审计 Milvus 与 Mongo 的 `e2e_*` 残留全部为 0，生产数据不变 |
| D8 | **用户报告**：候选「已入库（active）」后，客服提问仍然回答「未查询到该问题相关信息，无法作答」，但引用里确实出现了 `自进化` 条目 | 候选生成器在**没有检索证据**时，让大模型把「未提及…建议联系官方」写成了 FAQ 答案；审批链路把这种「无信息结论」当成知识入库 → 召回成功但模型读到的内容本身没有事实，只能继续答“无法作答”，并被记成新的缺口（可能循环生成同类候选） | 新增 `app/evolution/quality.py::looks_like_non_answer`；生成器改为：无证据 / 生成答案疑似「无信息」→ 登记 `need_info`（待人工补充，绝不产生 draft）；`approve` 拒绝 `need_info` 与「无信息」答案并回显原因；`edit` 补齐事实后自动回到 `draft`；召回端过滤历史遗留的「无信息」条目；审批页新增 `待人工补充` 状态与筛选，并为 active 条目提供「🗑 下架」（`DELETE /api/evolution/candidates/{id}`） | 浏览器全链路复验：提问→缺口→候选以 `待人工补充` 入库（`faq_answer` 为空）→ 编辑补齐真实答案 → 状态自动变 `待审批` → 通过 → `已入库` → **再次提问得到真实答案 + `自进化` 引用 + 置信度 100%**；「下架」按钮确认后条目移出检索且列表不再显示 |
| D9 | **用户报告（第二轮）**：候选已按 D8 流程补齐真实答案并入库（`配对码123456`），客服**仍然答「无法作答」且完全没有引用** | 重排阶段三重问题：① 进化条目参与重排时只喂了答案文本（“配对码123456”），没带它的 FAQ 问题，跨编码器打分被压低（实测 0.4793）；② 4 条联网结果分数 0.9996/0.9996/0.9995/0.9987 全排在前面；③ 动态截断在 0.9987 → 0.4793 的断崖处把本地知识（含已审批 FAQ）整体切掉，而重排阶段没有 RRF 阶段那种「权威条目保底」 | ① `deal_rrf_and_web_result` 对 `source=evolution` 的文档用「FAQ 问题 + 答案」作为重排文本（实测同一 FAQ 分数 0.4793 → **0.9999**）；② 新增 `cap_web_docs`：本地有命中时联网最多保留 `WEB_MAX_IN_CONTEXT`（默认 2）条；③ 新增 `ensure_evolution_docs`：重排截断后把被切掉的自进化权威条目补回并置于上下文最前 | 真实链路复验：`联网 5 → 保留 2`，最终上下文 `[evolution 0.9999, web, web]`，回答 **“HAK 180 烫金机的蓝牙配对码是123456。”**，引用 `evo_df12c80a13c9 / source=evolution`，`evolution_hit: true`；浏览器 UI 复验同样得到该答案 + `自进化` 引用 + 置信度 100% |
| D10 | **用户报告（第三轮）**：日志显示「缺口扫描完成，产出 strong 缺口 0 条」，但刚刚确实回答过「无法作答」 | 缺口去重是**按会话**做的（`session_id` 已有 pending/candidate 缺口 → 整条会话跳过），而缺口在候选通过/驳回后**从不复位**（`approve/reject` 只改候选、不碰 `k_gaps`）。于是同一浏览器会话里：蓝牙问题留下一个永久 `candidate` 缺口 → **后续所有问题永远扫不到**（实测该会话留着 7.2 分钟前的 candidate 缺口，新问题的两条未解决信号被整会话跳过） | ① 去重改为**按问题**（`query`）：同问题有 pending 缺口或窗口内出现过才跳过；已生成过候选的问题由候选级 `_exists_question` 兜住；② 缺口生命周期闭环：`KnowledgeGap.gap_id` 透传到候选（`KnowledgeCandidate.gap_id`），`approve`/`reject` 分别把来源缺口标记为 `resolved`/`rejected`，不再永久残留 | 用用户库里的真实未解决信号复跑一轮调度：`{"scanned": 1, "generated": 1, "skipped": 0, "failed": 0}`（修复前恒为 0），产出该问题的 `candidate` 缺口（confidence 0.7）与 `draft` 候选（带 `gap_id`）；离线新增用例覆盖「同会话新问题必须被扫到」「同问题窗口内去重」「审批/驳回回写缺口状态」 |

| D5（补充） | 原生模态 `confirm()` 阻塞渲染进程 | 浏览器模态对话框特性 | 如上：新增 `App.confirm()` 页内确认浮层并替换 2 处调用 + 2 处 `alert()`；新增原生模态静态守卫 | 复验通过（见 D5 行） |

修复后回归：`pyflakes app tests` 0 告警；`compileall` 通过；离线单测 **73 passed**（含新增 D1/D4/D5/D6 用例）。

## 5. 数据清理记录

| 目标 | 清理前 | 清理后 |
| --- | --- | --- |
| Milvus `kb_chunks`（`file_title=e2e_ui_0928171640`） | 3 | 0 |
| Milvus `kb_item_names`（同上） | 1 | 0 |
| Milvus `kb_evolution_items`（`evo_89c8068174e6` / `evo_8ec6bd00f962`） | 2 | 0 |
| Milvus 历史遗留 `e2e_sample_*`（item_name） | 3 | 0 |
| Mongo `k_candidates`（本次自建 3 条） | 3 | 0（生产候选 1 条保持不变） |
| Mongo `chat_message`（测试会话） | 7 | 0（其他会话 4 条保持不变） |
| Mongo `fb_events` / `k_gaps`（测试会话） | 6 / 3 | 0 / 0 |

两个 uvicorn 进程已关闭，端口 8000/8001 均已释放；服务日志保留在 `logs/verify-*.log`。

### 5.1 依赖裁剪后的环境一致性验证（补充）

`pyproject.toml` 裁剪依赖后，验证本地环境与锁文件确实一致、且裁剪后的环境仍能跑通全链路：

| 检查 | 结果 |
| --- | --- |
| `uv lock --check` | `Resolved 159 packages`（exit 0），锁文件与 `pyproject.toml` 一致 |
| `uv sync` | 仅清理了一个游离包（`cryptography`，不在依赖图中），无新增安装 |
| 已移除的直依赖是否还在 venv | 全部不在（`dashscope` / `magic-pdf` / `modelscope` / `torchaudio` / `torchvision` / `grandalf` / `langchain-community` / `langchain-mcp-adapters` / `mineru-kie-sdk`） |
| 关键运行期依赖 | 在位（`fastapi` / `flagembedding` / `langgraph` / `langchain-openai` / `pymilvus` / `pymongo` / `minio` / `torch` / `openai-agents`） |
| 锁中剩余的“疑似多余”包 | `datasets`、`pandas` 仍在锁中，但它们是 `flagembedding` 的传递依赖（`uv.lock` 第 563 行），非遗漏 |
| 离线回归 | `pyflakes` 0 告警、`compileall` 通过、`pytest` 73 passed |
| 真机端到端（裁剪后环境） | `E2E_ENABLED=1 uv run pytest -m e2e` → **1 passed / 47.37s** |
| 浏览器冒烟（裁剪后环境） | 三页均正常启动：问答页 `API: 已连接` + 欢迎语可用；审批页渲染生产候选 1 条 + Token 输入框；导入页空态正常；三页控制台 0 error |

## 6. 结论

- 三个页面与「导入 → 问答 → 反馈 → 缺口 → 候选 → 审批 → 回流」全链路在真实浏览器与真实依赖下**均跑通**，其中自进化闭环已用「人工编辑并审批的知识逐字出现在后续答案中」证明。
- 本轮共发现 5 个需修复问题（D1/D2/D3/D4/D6）并全部修复复验；2 个观察项（D5 原生 confirm 的可测性、D7 跑批清理复核）记录在案。
- 值得注意的教训：**静默 catch 会掩盖真实缺陷**（D6 与历史「反馈按钮不渲染」同源），本轮已把该处改为显式打印，并新增静态守卫测试。

## 7. 附录

### 7.1 截图索引（会话可视化目录）

| 文件 | 内容 |
| --- | --- |
| `verify-01-import.png` | 导入页：md 上传完成（7 个节点全绿） |
| `verify-02-chat-answer.png` | 流式回答 + `知识库` 引用 + 置信度 100% |
| `verify-03-feedback.png` | 点踩后置灰 + 「已反馈」 |
| `verify-04-approval.png` | 审批后台：通过 / 驳回后的状态 |
| `verify-05-import-reject.png` | 不支持类型被拒（失败徽标 + 原因） |
| `verify-06-nonstream.png` | 非流式问答结果 |
| `verify-07-history.png` | 刷新后历史回显（修复 D6 后） |
| `verify-08-evolution-recall.png` | 自进化回流：答案采用审批知识 + `自进化` 引用 |
| `verify-09-clear.png` | 清空对话后仅剩欢迎语 |
| `verify-10-inpage-confirm.png` | 问答页「清空对话」的页内确认浮层（替代原生 confirm） |
| `verify-11-inpage-reject.png` | 审批页「驳回」的页内确认浮层（替代原生 confirm） |

### 7.2 关键日志

- `logs/verify-import.out.log` / `verify-import.err.log`
- `logs/verify-query.out.log` / `verify-query.err.log`（含 `GET /api/history/... 200 OK`、
  `DELETE /api/history/... 200 OK`、缺口扫描与自进化条目写入等下架记录）

---

## 8. 第二轮复验（代码改动后，2026-09-28 晚）

> 触发：第一轮验证后又修复了 D11–D14（工作区 7 个文件改动 + 2 个新增回归用例），需**重扫改动面**并在
> **真实浏览器**中对「导入 → 问答 → 反馈 → 缺口 → 候选 → 审批 → 回流」再做一次端到端复验。

### 8.1 环境与工具

| 项 | 值 |
| --- | --- |
| 查询服务 | `http://127.0.0.1:8001`（以当前工作区代码重启，清空 60s 主体名目录缓存） |
| 导入服务 | `http://127.0.0.1:8000`（同批重启） |
| 浏览器 | **外部 Chrome（TRAE Chrome 扩展）**，真实可见标签页 |
| 浏览器能力 | `browser_navigate/snapshot/click/type/evaluate/console_messages`；文件上传用页内 `DataTransfer` 模拟（原生 `filechooser` 被桥接拒绝） |
| 测试文档 | `output/verify_ui/e2e_ui_20260928c.md`（唯一主体 `e2e_ui_20260928c烫金机` + 唯一事实：`12~28 摄氏度` / `55W` / 包装清单） |
| 外部依赖 | 真实 Milvus `192.168.200.10:19530` / MongoDB `kb002` / MinIO / DashScope Qwen，无 mock |

### 8.2 改动面（本轮复验对象）

| 文件 | 改动 |
| --- | --- |
| `app/resources/prompts/rewritten_query_and_itemnames.prompt` | 新增「主体以当前问题为准」最高优先级规则；示例型号名去掉真实主体 `HAK180` |
| `app/resources/js/app.js` | `formatDateTime` 修复秒级时间戳被渲染成 1970 年 |
| `app/rag/query/answer_service.py` | `cited_chunk_ids` / `faq_evo_ids` 落状态前统一 `str()` 归一 |
| `app/api/routers/query.py`、`app/api/schema/query_schema.py`、`app/rag/query/pipeline.py` | 查询响应与 SSE `final` 暴露 `item_names` |
| `app/resources/js/chat.js` | 反馈载荷透传 `item_names`；历史回显同步带主体 |
| `tests/unit/test_item_name_prompt.py`、`tests/unit/test_evolution_signal_integrity.py` | 新增回归守卫 |

### 8.3 结果总览

| 流程 | 结果 | 说明 |
| --- | --- | --- |
| P1 导入（md 上传 → 落库） | PASS | 7 节点全绿；Milvus `kb_chunks=6`、`kb_item_names` 主体 = `e2e_ui_20260928c烫金机` |
| P2 问答（流式 / 引用 / 置信度 / 反馈栏） | PASS | 答案逐字含导入事实 `55W`；`引用来源（4）`（标签 `知识库`）；置信度 100% |
| P3 反馈与缺口 | PASS | 兜底话术写入未解决信号 → 调度器产出缺口 + `need_info` 候选 |
| P4 审批后台（渲染 / 鉴权 / 控制台） | PASS | 列表正常、写操作仍受 `X-Internal-Token` 约束、控制台 0 error |
| P5 自进化回流 | PASS | 早段复验：查询命中 `evo_d805489677bc`，日志「重排截断后补回 1 条自进化权威条目」 |
| P6 历史与会话 | PASS | 刷新完整回显；清空走页内浮层且服务端删除计数一致；不存在会话返回 0 |
| P7 边界与降级 | PASS | 库外问题走兜底话术且反馈栏仍在；三页控制台 0 error；服务端无异常堆栈 |
| 离线回归 | PASS | `pyflakes` 0 告警、164 个 py 文件 AST 解析无语法错误、`pytest` **114 passed / 1 deselected** |
| 数据清理 | PASS | `e2e_` 残留全部为 0，生产数据计数不变 |

### 8.4 逐流程明细（本轮实测证据）

**P1 导入**：上传 `e2e_ui_20260928c.md` → 徽标 `上传中…→处理中…→已完成`，7 个节点依次完成
（开始上传文件 / 检查文件 / Markdown图片处理 / 文档切分 / 主体名称识别 / 向量生成 / 导入向量库）；
导入服务日志 `知识库写入完成：insert_count=3`（页内 `DataTransfer` 模拟触发两次 `change`，故落 2 份共 6 条切片）；
回查 `kb_chunks=6`（`item_name=e2e_ui_20260928c烫金机`）、`kb_item_names=2`。

**P2 问答**：流式提问「e2e_ui_20260928c烫金机的额定功率是多少？」→ 阶段进度 7 节点完成 →
答案 `e2e_ui_20260928c 烫金机的额定功率为 55W。`（与导入事实逐字一致）→ `引用来源（4）`（标签 `知识库`）→
`回答置信度 100%` → 反馈栏（👍/👎）渲染。

**P3 反馈 → 缺口**：兜底话术路径命中「现有参考内容与历史对话中未查询到该问题相关信息，无法作答」，
查询服务日志 `flush_session_signals … 已写入未解决信号到 fb_events`；随后调度器产出缺口
`6aba753a…c63`（`status=candidate`, `confidence=0.7`）与候选 `6aba753c…c64`（`status=need_info`，符合 D8 质量闸门）。

**P6 历史与会话**（本轮浏览器实测）：

| 验证项 | 预期 | 实际 |
| --- | --- | --- |
| 刷新回显 | 历史消息 + 引用块 + 反馈栏完整恢复 | 一致：用户问题 + 答案（含 `55W`）+ `引用来源（4）` + `回答置信度 100%` + 👍/👎 全部回显 |
| 清空对话 | 页内确认浮层（非原生 `confirm`）→ 后端与界面同时清空 | 一致：浮层文案「确定要清空当前会话的历史记录吗？这将无法恢复。」；服务端 `已清空会话 e2e_sess_20260928b 的 2 条记录`；界面 2 条消息 → 仅剩欢迎语（`.msg.user=0`、`.feedback-bar=0`） |
| 不存在会话 | 正常返回 0，不报错 | 一致：`DELETE /api/history/no-such-session-xyz` → 200，`deleted_count=0` |

**P7 边界与降级**：提问库外事实「e2e_ui_20260928c烫金机的防水等级是多少？」→ 兜底话术
`现有参考内容与历史对话中未查询到该问题相关信息，无法作答`、`回答置信度 0%`、反馈栏仍在；
三页（客服 / 审批 / 导入）`browser_console_messages` 均为 `(none)`（0 error / 0 warn）；
查询与导入服务日志按 `ERROR|Traceback|Exception` 检索无命中。

### 8.5 发现的问题与处理（D11–D14）

| 编号 | 问题 | 根因 | 处理 | 复验结果 |
| --- | --- | --- | --- | --- |
| D11 | 历史会话在讨论 A 主体后，用户改问 B 主体，模型把主体**替换成历史里的 A**，召回按错误主体过滤 → 答「无法作答」 | ① 提示词缺少「当前问题主体优先、禁止用历史主体替换」的强约束；② 规则示例直接用了库内真实主体名 `HAK180`，既污染识别又诱导模型照抄 | 提示词新增「主体以当前问题为准」最高优先级段（含反例）；示例型号名改为中性占位（`XYZ-123`/`UI0928`/`A1B2`）；新增 `tests/unit/test_item_name_prompt.py` 三条静态守卫 | 用 B 主体提问一次命中并正确作答；守卫用例断言提示词不得出现 `HAK180`/`HAK 180` |
| D12 | 前端时间显示为 **1970 年** | 秒级时间戳是**合法的毫秒值**（落在 1970），`new Date()` 不会得到 `Invalid Date`，因此「仅在 Invalid 时才纠正」的兜底逻辑永远不触发 | `app.js::formatDateTime` 改为**先按量级判定**秒/毫秒（`n > 1e11` 视作毫秒，否则 `×1000`），不再依赖 `Invalid Date` 分支 | 审批页 / 客服页时间戳显示为当前时间，不再是 1970 |
| D13 | 自动会话信号被**静默丢弃**，缺口漏检 | Milvus 数值型主键被解析成 `int`，`FeedbackEvent.cited_chunk_ids(list[str])` 校验失败抛异常，被上层 catch 吞掉 | `answer_service.backfill_evolution_outputs` 落状态前 `[str(c) for c in …]` 归一；新增 `test_evolution_signal_integrity.py` 断言数值主键必须转 str | 回归用例通过；浏览器点踩后 `fb_events` 正常落库且带 `cited_chunk_ids` |
| D14 | 显式 👎 反馈未携带 `item_names` → 缺口/候选**主体丢失** | 查询响应与 SSE `final` 未回传已识别主体，前端反馈载荷因此无从携带 | 非流式响应 schema 与 SSE `final` 增加 `item_names`；`chat.js` 的 `buildBotMeta` / 历史回显 / 反馈载荷全链路透传；新增回归用例 | 浏览器点踩后 Mongo `fb_events.item_names` 为真实主体（如 `["e2e_ui_20260928b"]`） |

> 说明：D11–D14 的修复**均在本轮复验前已完成**，本轮职责是「改动后重新联调复验 + 数据清理 + 报告」，未再引入新缺陷。

### 8.6 数据清理记录

| 目标 | 清理前 | 清理后 |
| --- | --- | --- |
| Milvus `kb_chunks`（`item_name like "e2e_%"`） | 10 | 0 |
| Milvus `kb_item_names`（同上） | 3 | 0 |
| Milvus `kb_evolution_items`（同上） | 1 | 0 |
| Mongo `chat_message`（测试会话） | 4 | 0 |
| Mongo `fb_events`（测试会话） | 8 | 0 |
| Mongo `k_gaps`（测试会话） | 5 | 0 |
| Mongo `k_candidates`（本轮自建 5 条，按 `_id` 精确删除） | 5 | 0 |

清理脚本 `output/verify_ui/cleanup_e2e.py`（Milvus 走「删除 → 等待 → 复核」，Mongo 按会话 ID 与候选 `_id` 精确删除）；
审计脚本 `output/verify_ui/residue_audit.py` 复核：`e2e_` 残留**全部为 0**，生产数据不变
（`k_candidates=2`、`chat_message=10`、`kb_evolution_items=2` 条 HAK 180 条目）。

### 8.7 结论（第二轮）

- 改动后的「导入 → 问答 → 反馈 → 缺口 → 候选 → 审批 → 回流」全链路在**外部真实浏览器**与真实依赖下再次跑通；
  D11–D14 四条修复链路各有针对性回归断言，离线 **114 例**全绿。
- 本轮最有价值的教训：**「只在异常分支兜底」的逻辑会漏掉语义合法但取值错误的输入**——D12 的秒级时间戳是合法毫秒值，
  D13 的数值主键是合法主键，二者都不抛错却都渲染/落库错误；应改为「按量级/类型显式判定」而非「出错才纠正」。

---

## 9. 追加复验：D15「答不出却给出图」

> 触发：第二轮复验后用户反馈——提问「烫金机怎么安装」时，回答是
> `现有参考内容与历史对话中未查询到该问题相关信息，无法作答。`，**下方却渲染出了安装示意图**。

### 9.1 根因

说明书类切片是「**零碎文字 + 多张配图**」形态。实测库内命中切片（`hak180使用说明书`）：

| chunk_id | 文字（去图后） | 配图数 |
| --- | --- | --- |
| `469389094813929834` | `## 重要事项 / 请务必使用随机附带的电源线。/ b 安装进纸托板。/ 打开前盖。` | 3 |
| `469389094813929835` | `## 重要事项 / 打开前盖。/ e 将烫金膜盒装入烫金膜盒支架中。…` | 3 |
| `469389094813929837` | `## 3.4.1 装入全幅烫金膜盒 / a 打开烫金膜盒支架盖。…` | 2 |

链路：① `answer_out.prompt` 规则 2 限定模型**只能用「文字信息」**、规则 5 规定无匹配即输出固定兜底话术；
② 模型读到零碎步骤文字 + 图片 Markdown 链接（**看不到图像像素**），判为「文字不足以回答」→ 输出兜底话术；
③ 但 `answer_service.extract_text_image_url` **独立地**从同一批切片抽出图片 → `image_urls` 非空 →
前端 `renderAnswerWithImages` 照常渲染。于是「嘴上说答不出、手上却给出图」自相矛盾，
并因兜底话术命中 `_NO_ANSWER_MARKERS` 而产出**假缺口/假候选**，污染自进化闭环。

### 9.2 处理（按用户选定的取向：不展示图片，只说答不出）

| 文件 | 改动 |
| --- | --- |
| `app/shared/utils/answer.py` | **新增**：`NO_ANSWER_MARKERS` + `is_no_answer()`，查询端与自进化端**共用一份口径** |
| `app/rag/query/answer_service.py` | `extract_text_image_url` 命中兜底话术时**不回填** `image_urls` 并记日志 |
| `app/evolution/feedback/collector.py` | 删除本地 `_NO_ANSWER_MARKERS`，改用共享 `is_no_answer`（避免两处口径漂移） |
| `tests/unit/test_answer_image_fallback.py` | **新增** 4 条回归：兜底话术抑制图片、正常作答仍抽取、图片型 `url` 同样抑制、标记判定 |

### 9.3 复验结果

| 验证项 | 结果 |
| --- | --- |
| 离线用例 | `pytest` **118 passed / 1 deselected**（较此前 114 增加 4 条 D15 回归） |
| 真实库 + 真实模型（进程内跑图） | 提问 `HAK 180 烫金机怎么安装` → 主体确认 `HAK 180 烫金机 → Brother HAK 180 烫金机`；模型正常作答安装步骤、`image_urls` 非空 → **无矛盾**（正常链路未被误伤） |
| 边界（裸「烫金机」） | 走「主体未确认」反问短路，`image_urls=[]` → 同样无矛盾 |
| 数据清理 | 本轮 `e2e_d15_verify` 会话的 `chat_message`/`fb_events`/`k_gaps`/`k_candidates` 均已清零 |

> 说明：兜底话术是否出现取决于模型对「文字是否足以作答」的判断，属模型侧不确定性；
> 因此「兜底话术 ⇒ 不展示图片」这条**不变量**以确定性单测锁定，而非依赖某次真实提问复现。

## 10. 第三轮复验：D16「没识别到主体 → 让用户点选相似主体」

### 10.1 现象与复现（用户报告）

用户只输入**型号前缀** `hak180`（截图 `codex-clipboard-3174bd1f`），页面回：

> 本次问题没有关联到任何主体,有没有相似可选的主体! 请您明确主体再提问!

而库内主体目录里**只有一个** `Brother HAK 180 烫金机`。用户随后手打 `HAK 180 烫金机` 才拿到答案——
说明链路“认识这个主体”，只是**在阈值判定那一步把候选整批丢掉了**，用户无法自救。

### 10.2 根因（终端实测数据）

| 环节 | 实测 |
| --- | --- |
| 主体识别 | LLM 正确抽出 `item_names=["hak180"]` |
| 向量召回 | `kb_item_names` 命中 `Brother HAK 180 烫金机`，但稠密+稀疏融合分仅 **0.4093** |
| 阈值判定 | 确认阈值（`ITEM_NAME_CONFIRM_MIN_SCORE`）与可选阈值（`ITEM_NAME_OPTION_MIN_SCORE`=0.60）**双双未达** |
| 旧行为 | 走进「4) 其余丢弃」分支 → `confirmed/option` 皆空 → `apply_item_name_result` 输出「请您明确主体再提问」 |

即：**阈值是合理的**（0.409 确实不足以自动确认主体），缺的是「够不上阈值时把相似主体列出来让用户点选」
这一层降级，而不是放宽阈值（放宽会引入误召回）。

### 10.3 改动（最小改动，不动数据契约）

| 文件 | 改动 |
| --- | --- |
| `app/rag/item_name/catalog.py` | 新增 `find_similar_names(name, limit=3)`：目录归一化互子串（较短方 ≥3 字符）→ token 前缀等价；按相似强度排序 |
| `app/rag/item_name/match.py` | 新增 `_similar_fallback()`（目录相似 → 低分向量命中 → 小库直接列目录，`CATALOG_SUGGEST_MAX=5`）；`select_item_names` 增加 `similar_list`，并补上「向量零命中 + 目录未命中」分支 |
| `app/rag/query/item_name_confirm_service.py` | `apply_item_name_result` 把候选写入 `state["item_name_options"]`，文案改为「以下可能是您要找的：X。请点击下方主体直接提问。」 |
| `app/process/query/agent/state.py`、`app/api/schema/query_schema.py`、`app/api/routers/query.py`、`app/rag/query/pipeline.py` | 新增并透出 `item_name_options`（非流式响应 + SSE `final`） |
| `app/resources/js/chat.js`、`app/resources/css/chat.css` | `renderItemNameOptions()` 渲染 `.option-bar/.option-btn`；点击 → 填充输入框并自动提问（`optionQuestion()` 负责“只输型号就按主体问、否则主体+原问题”） |
| `tests/unit/test_item_name_options.py` | **新增 7 条**：相似召回 3 类判据、零命中兜底、状态透传、前端选项渲染与反馈仍带 `item_names` 的静态守卫 |

### 10.4 真实浏览器复验（本轮，临时端口 8011 跑修复后代码）

| 步骤 | 预期 | 实际 | 证据 |
| --- | --- | --- | --- |
| 输入 `hak180` | 不再只说“请明确主体”，而是给出可点选主体 | 一致：`没有识别到明确的主体，以下可能是您要找的：Brother HAK 180 烫金机。请点击下方主体直接提问。`；`.option-hint`=`请选择主体：`，按钮 `Brother HAK 180 烫金机` | 截图 `verify-22-subject-options.png` |
| 点击该按钮 | 自动以该主体重新提问并正常作答 | 一致：得到真实说明书内容（A4 90–350g/m²、15ppm/7ppm、44 页 ADF）+ 安装示意图 + `引用来源（3）`（全部 `知识库`）+ `回答置信度 90%` | 截图 `verify-23-after-option-click.png` |
| 控制台 / 服务端日志 | 无 error、无未处理异常 | 一致（`logs/verify-query.out.log` 无 `Traceback`/`ERROR`） | 服务日志 |

### 10.5 本轮数据清理记录（精确清理，先打印计数后复核）

| 目标 | 清理前 | 清理后 |
| --- | --- | --- |
| 本轮临时会话 `sess-1d4055ow9kpmul7i6v5`（8011 复验产生） | `chat_message=4`、`fb_events=1`、`k_gaps=0`、`k_candidates=0` | **全部 0（复核通过）** |
| 用户会话 `e2e_sess_20260928b`（浏览器 localStorage 里沿用第一轮联调留下的会话 id，内容是用户真实提问） | `chat_message=6`、`fb_events=2`、`k_gaps=2` | **原样保留**（判定为用户数据，未做任何删除） |
| Milvus `kb_evolution_items` | 0 条 | 0 条（本轮未产生进化条目） |
| 临时 uvicorn（端口 8011） | 监听中 | 已停止（端口不再监听） |

> 说明：`e2e_sess_20260928b` 虽带 `e2e_` 前缀，但其中的问题是用户本人输入的真实问题，且已产出 2 条
> `candidate` 缺口与 2 条 `draft` 候选（可在审批页直接处理）。按「绝不触碰既有数据」原则保留原状；
> 如需清空，可在问答页点「清空对话」后再删除 2 条 draft 候选。

### 10.6 结论（第三轮）

「没识别到主体」不再是一条死路：能确认就确认，够不上阈值就给**可点选的相似主体**，点一下即可继续。
离线门禁 `pyflakes` 0 告警、`compileall` 通过、**129 passed / 1 deselected**（含 2 条新增前端「无行内样式」守卫）；
浏览器实测两步走通，并在用户实际使用的 `:8001` 端口上复现一致（见 10.7）。

### 10.7 在用户端口 `:8001` 上的复核

本轮复查时发现 `:8000` / `:8001` 均未在监听（用户此前看到的还是**旧代码**的进程输出），因此用当前工作区代码
重新启动两个服务（后台、`-WindowStyle Hidden`，日志 `logs/ui-{import,query}.{out,err}.log`），随后：

| 验证项 | 预期 | 实际 |
| --- | --- | --- |
| `/api/health` | 200 | 两个端口均 `200 {"code":200,"message":"ok"}` |
| 资源指纹 | 与已验证版本一致 | `app.css/chat.css/app.js/chat.js` 均为 `?v=<内容哈希>`（每次启动按内容重算） |
| 接口直查（独立会话 `e2e_sess_verify_d16`） | 返回可点选主体 | `answer`=`没有识别到明确的主体，以下可能是您要找的：Brother HAK 180 烫金机。请点击下方主体直接提问。`；`item_name_options`=`[{"item_name":"Brother HAK 180 烫金机","matched_by":"catalog_contains"}]` |
| 页面控制台 | 0 error | `dev.logs()` 长度 0；`#apiPill`=`API: 已连接` |
| 清理 | 测试会话清零 | `e2e_sess_verify_d16` 的 `chat_message=2`/`fb_events=1` 已删，复核为 0；用户会话未动 |

> 提示：服务由本轮复查处后台启动；若用户自行用 `uvicorn` 再启一次会端口占用，先停掉即可
> （`Get-NetTCPConnection -LocalPort 8000,8001 -State Listen` 查 PID → `Stop-Process -Id <pid>`）。

### 10.8 本轮观察（非缺陷判定，已记录待决策）

同一条链路在真实浏览器里出现过一次**「一行答案 + 0 引用 + 回答置信度 0%」**（`消费电力(烫印中): 少于340W`），
而同一问题随后用接口复跑得到**完整答案 + 3 条引用 + 100%**。定位到两个放大不确定性的机制：

1. **联网结果不产出引用**：重排日志显示 `Top3` 前两条是 `chunk_id=None` 的联网文档（`cap_web_docs`
   只限条数、不限排名）；`build_citations` 只为 `kb`/`evolution` 生成引用，联网来源在
   `backfill_evolution_outputs` 中被跳过 → 答案若主要由联网片段支撑，界面表现为「答了却无引用」。
2. **接地性由 LLM 判定、异常即降级 0**：`compute_groundedness` 的空证据 / 解析失败 / 调用异常都返回 `0.0`，
   前端把它渲染成「回答置信度 0%」，与「答案确实不接地」不可区分。

复现尝试：用独立会话按浏览器同样的顺序（先 `hak180` → 再点选主体提问）连查两次，
结果正常（第二轮 378 字答案 / 2 条引用 / 置信度 80%），因此判定为**模型侧不确定性放大**，
而非确定性缺陷；处理建议已写入 `docs/architecture-review-20260928.md` 第 15.4 节（含「联网排名策略」
「联网引用标签」「groundedness 未知态」三项），待与用户确认后实施。

本轮全部自建测试数据（`e2e_sess_verify_d16`~`d19` 与浏览器测试会话）已精确清理，复核残留为 0；
用户会话 `e2e_sess_20260928b` 的 6 条消息、2 条反馈、2 条缺口与其 2 条 draft 候选**原样保留**。

### 10.9 上节观察项的修复与复验（用户要求「全部修复」，2026-09-28 深夜）

| 问题 | 修复 | 复验（真实库 + 真实模型 + 真实浏览器） |
| --- | --- | --- |
| 联网结果可压过本地知识，且不产出引用 | 新增 `rerank_service.prefer_local_docs()`：本地有命中时联网排序分被压到「不超过本地最高分」，同分本地在前；`citations` 扩展为 `kb`/`evolution`/**`web`** 三类，联网引用带 `title` + 原网页链接（仅 http/https），前端标「联网」可点开；反馈载荷剔除联网引用 | 提问 `Brother HAK 180 烫金机` → 页面 `引用来源（5）`＝**3×知识库 + 2×联网**（联网项为可点链接，标题如「Brother(中国)盛装出席…」）、`回答置信度 90%`、控制台 0 error；服务日志出现 `联网结果 1 条分数被压至本地最高分 0.9992，本地知识优先排序` |
| 接地性失败与「0 分」不可区分 | `compute_groundedness()` 空证据 / 调用失败 / 解析失败统一返回 `None`（未评估）；API `groundedness` 改为可空，前端显示「回答置信度 未评估」 | 离线用例覆盖三种失败路径均返回 `None`、成功返回 0.8；真机返回数值 0.9/1.0 时正常渲染百分比 |
| 自调参 / 回测 / 在线指标从未接线 | `scheduler.run_evolution_cycle_once()` 按周期编排 `record_metric` → `adjust_step` → `run_backtest`；`adjust_step` 基线改为排除本轮快照；回测新增 `backtest_min_hits` 防单条差评误杀；新增 `GET /api/evolution/status` 并在审批页显示「闭环：缺口 N · 候选 M · 近一次自评 时间」 | 启动日志：`指标快照：采纳率=0.00 缺口率=1.00 自调=无`、`回测完成：考察 0 条，下架 0 条`；`k_metrics` 新增 1 条快照；审批页顶部 pill 文本与 `title` 明细均正确 |
| 缺口扫描只取最新 batch 条，早期信号会饿死 | 改为 **ts 升序 + 进程内游标推进**，窗口扫完归零复扫（重复由问题级去重兜住），不新增 Mongo 字段 | 真机日志：`消费信号 2 条，产出 strong 缺口 0 条，游标 1790005249 → 1790608023`，下一轮 `消费信号 0 条 … 游标 1790608023 → 0`（复扫可见）；离线新增「积压超 batch 也要扫到」「游标之后的新事件下一轮可见」两条用例 |

| 项 | 结果 |
| --- | --- |
| 离线门禁 | `pyflakes app tests` 0 输出、`compileall` 通过、**153 passed / 1 deselected**（较上轮 129 增加 24 条） |
| 浏览器 | 客服页与审批页控制台均 0 error；审批页闭环 pill 正常 |
| 数据清理 | 本轮 `e2e_sess_verify_d20` + 浏览器测试会话共 4 条 `chat_message` 已删除，复核残留 0；用户会话 6 条消息 / 2 条缺口 / 2 条 draft 候选未动 |
| 服务状态 | `:8000` / `:8001` 以最新代码后台运行中（日志 `logs/ui-{import,query}.*.log`） |
