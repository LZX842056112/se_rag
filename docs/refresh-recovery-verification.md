# 反馈和导入页面刷新修复

日期：2026-10-24。分支：`codex/agentic-rag-next`。基础提交：`fc20ffe`。

## 修改内容

- 历史接口从 `query_feedback` 批量读取本会话的用户反馈。页面恢复两个独立维度的选择：帮助度、解决状态。已提交的同组按钮保持禁用。
- 自动失败信号不作为用户选择。其他会话的反馈不返回给当前会话。
- 旧回答缺少意图字段时，也保存用户反馈。未知意图保留为空，不生成知识缺口信号。
- 导入页面用 `sessionStorage` 保存任务 ID、文件名、状态和日志。存储键包含接口地址。刷新后查询原任务，不重复上传。
- 已完成和失败的结果直接回显。服务端快照过期不会覆盖已保存的最终结果。
- 保存新令牌会替换旧轮询。晚返回的旧请求不能关闭新轮询或改写页面。
- 临时网络错误和普通 HTTP 503 继续重试。鉴权失败或服务端未配置导入令牌时，暂停轮询并提示验证令牌。

## 使用范围

反馈以服务端保存值为准。导入恢复适用于同一浏览器标签页和同一接口地址。

如果刷新时还没有收到上传响应，页面显示“上传中断”。此时请先核对导入结果；确实未导入时，再选择文件。页面不自动重传文件。

本次未实现跨设备导入列表、浏览器关闭后的恢复，或服务重启后的导入执行续跑。文件内容和令牌不写入新增的任务缓存。

## 离线验证

- Python：834 项通过，1 项 E2E 未运行。存在 1 条 LangGraph 上游弃用提示。
- 前端：38 项通过。
- `pyflakes app`：通过。
- `git diff --check`：通过。

新增回归覆盖：双维度反馈恢复、部分旧记录、自动信号隔离、会话归属、旧回答、导入处理和最终结果恢复、上传中断、令牌切换竞争、临时错误重试，以及旧请求晚返回。

只读代码复核已完成。发现的轮询竞争、最终结果覆盖和 503 分类问题均已修复，最终复核未发现剩余实质问题。

## 页面验证

真实环境标识：`ui_test_20261024_a5d8b2be`。数据库、向量集合和图片桶均使用本次独立标识。客户端核对结果确认未选择正式数据库。

首次真实 Mongo 连接返回 `ServerSelectionTimeoutError`。当时暂用下面的本地模拟服务完成页面验证。网络恢复后，已补完真实链路复测，结果见下一节。

另启动完全本地的页面模拟服务。该服务不加载 `.env`，使用 `mongomock`，阻断非回环网络连接。模型和导入执行使用模拟函数。页面脚本、上传入口、鉴权、反馈、历史接口和任务状态接口使用项目实现。未新增公开测试接口。

实际页面操作结果：

1. 发送模拟问题，点击“有帮助”和“已解决”，刷新。两个选择均保留，数据库中只有一条合并反馈。
2. 通过文件选择器上传专用 Markdown，处理中刷新。任务卡 ID 不变，恢复 2 条完成日志和 1 条运行日志。
3. 结束模拟处理，再次刷新。卡片显示已完成，保留 4 条完成日志。
4. 服务日志中只有 1 次上传请求，所有状态请求使用同一个任务 ID。

截图和原始记录：

- [反馈刷新截图](../output/refresh_recovery/feedback-after-refresh.png)
- [导入处理中刷新截图](../output/refresh_recovery/import-processing-after-refresh.png)
- [导入完成刷新截图](../output/refresh_recovery/import-completed-after-refresh.png)
- [页面核验记录](../output/refresh_recovery/page-verification.json)
- [实际客户端隔离记录](../output/refresh_recovery/isolation-proof.json)
- [本地模拟服务隔离记录](../output/refresh_recovery/fixture-isolation.json)
- [Python 测试日志](../output/refresh_recovery/python-regression.log)
- [前端测试日志](../output/refresh_recovery/frontend-regression.log)

原测试服务和本地模拟服务均已停止。测试数据、日志和截图保留在 `output/refresh_recovery/`。本次未修改正式 `.env`，未自动提交、推送或部署。

## 网络恢复后的真实页面复测

2026-10-24 网络恢复后，重新核对实际 Mongo 客户端、Milvus 集合和图片桶。全部使用 `ui_test_20261024_a5d8b2be` 隔离资源。查询和导入服务分别运行在 8011、8010，每个服务一个 worker。本次使用真实 `qwen-flash` 模型、BGE-M3 和项目查询、导入流程。

验证结果：

- 真实问答：问题为“Brother HAK 180烫金机有几个转印温度设置值？”。回答完成后，点击“有帮助”并刷新。帮助度保持选中，解决状态仍可选择。随后点击“已解决”并刷新，两个选择均保持选中和禁用。
- 数据核对：会话 `sess-226a8052-c5eb-48ad-b89f-87e82cfa0cbd` 有 2 条历史消息和 1 条合并反馈，反馈为 `thumbs=1, resolved=true`。
- 真实导入：通过页面文件选择器上传专用虚构产品文档 `ui_test_refresh_manual.md`。处理中刷新，原任务卡和日志恢复；完成后再次刷新，保留 7 条完成日志，无运行项。
- 上传和索引核对：仅有 1 次上传请求。所有状态请求使用任务 `c7df8f37-82ab-4e21-8bc4-9f1ea27f2ff0`。独立向量集合新增 5 个分块，独立数据库新增 6 个父章节，文档版本为 `ui-test-refresh-20261024`。

真实页面补测的证据：

- [反馈刷新截图](../output/refresh_recovery/real-network-restored/feedback-after-refresh.png)
- [导入处理中刷新截图](../output/refresh_recovery/real-network-restored/import-processing-after-refresh.png)
- [导入完成刷新截图](../output/refresh_recovery/real-network-restored/import-completed-after-refresh.png)
- [客户端隔离核验](../output/refresh_recovery/real-network-restored/isolation-proof.json)
- [页面与数据核验](../output/refresh_recovery/real-network-restored/page-verification.json)
- [导入最终快照](../output/refresh_recovery/real-network-restored/import-snapshot.json)
- [反馈与历史原始记录](../output/refresh_recovery/real-network-restored/feedback-and-history.json)
- [测试服务停止记录](../output/refresh_recovery/real-network-restored/stop-services.json)

真实测试服务已停止。测试数据和证据保留。该复测确认本次刷新修复，不构成答案质量基线。

## 生效步骤

1. 重启使用本分支代码的查询和导入服务。
2. 刷新浏览器页面，加载带新内容指纹的脚本。
3. 点击反馈后刷新，检查原选择。接受导入任务后刷新，检查原任务卡和日志。

旧页面在本次修复前已经丢失的导入任务 ID，无法从浏览器恢复。反馈则可以回读服务端已有记录。
