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
