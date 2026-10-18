# 内部试用部署与验收

## 运行边界

- 使用 Python 3.11 或更高版本。依赖按 `uv.lock` 安装：`uv sync --frozen`。
- 查询服务使用一个 worker：`uvicorn app.api.http.query_server:app --workers 1 --port 8001`。
- 启动时取得主机端口锁。第二个查询 worker 会拒绝启动。
- 同会话的第二个问题返回 HTTP 409。其他会话可以并行。
- 在同源网页中使用客服页。API 客户端必须保留服务端 Cookie。
- 本次不引入 Redis、分布式调度或图检查点。扩容前必须外置 SSE 和任务状态，并增加调度锁。

## 配置

以下配置放入任务环境的 `.env`。不要提交真实密钥。

```dotenv
SESSION_OWNERSHIP_ENABLED=true
INTENT_TIMEOUT_SECONDS=2
EVOLUTION_AUTO_TUNE_ENABLED=false
DEFAULT_ISSUE_OWNER=
LLM_MODEL_PRICES_JSON={}
```

`INTENT_TIMEOUT_SECONDS` 最大为2秒。分类不重试。分类容量满、超时或输出无效时进入 RAG。

`LLM_MODEL_PRICES_JSON` 按实际模型名配置每百万输入和输出 token 的美元价格。例如：

```json
{"your-model":{"input":1.0,"output":2.0}}
```

上例只是格式示例。请替换为实际合同价格。模型价格未知或调用没有返回用量时，费用为 `null`。
`first_token_ms` 只测量流式回答。非流式响应不能提供实际首 token 时间。

审批资料读取、待办管理和试用指标均要求 `X-Internal-Token`，值为 `EVOLUTION_ADMIN_TOKEN`。
导入继续使用独立的 `IMPORT_ADMIN_TOKEN`。请覆盖开发默认令牌。

## 会话与反馈兼容

浏览器取得 HttpOnly、SameSite=Strict 的归属 Cookie。数据库保存 Cookie 的哈希与会话绑定。
HTTPS 或 `APP_ENV=prod` 时 Cookie 带 Secure。

无归属记录的旧会话不能被新浏览器认领。迁移前应备份旧历史。旧历史继续保留在数据库。
员工可点击“新建会话”开始试用。不要通过关闭访问保护来共享会话。
如果确需兼容可信旧调用方，可明确设置 `SESSION_OWNERSHIP_ENABLED=false`；该模式不验证会话归属。

新增响应字段：`turn_id`、`answer_id`、`business_tag`、`issue_id`、`handoff_required` 和 `metrics`。
任务状态与历史记录也携带 `turn_id`。刷新恢复以轮次 ID 区分旧回答和本轮回答。

反馈携带 `answer_id`。后端读取本会话中已保存的助手回答，重建问题、主体、引用和意图。
无 ID、无效 ID、跨会话 ID 以及旧记录的未知意图均不能触发知识生成。
旧反馈请求仍可被接受，但不生成知识缺口。闲聊反馈单独保存在质量反馈集合。
`resolved` 是可选的明确解决反馈。点赞不自动表示问题已解决。

`groundedness=null` 表示未评估；`0.0` 表示评估为零。新数据保存此差别。
旧数据中已经存成零的未评估记录不能自动推断恢复。本次不批量改写旧历史。

## 政策资料

业务标签为产品知识、操作排障、价格政策、售后、投诉。保留 RAG 与闲聊两条图分支。
价格和售后问题只使用有效业务政策。普通产品手册不能作为实时承诺的依据。

管理员上传资料时可填写：

- `document_kind`：`manual` 或 `policy`。
- `document_version`：资料版本，最多128字符。
- `valid_until`：政策失效时间，Unix秒。

政策必须有版本和未来失效时间。上传界面提供对应字段。上传政策前需由业务负责人核实内容。
旧文档默认作为手册处理，不会自动获得政策身份。

当前集合使用动态字段。新增元数据不需要删除或重建集合。不要运行清空数据的强制 schema 迁移。
引用显示文档名、章节、页码和版本。版本表示当前上传版本；本次不实现多版本并存。

## 待办与指标

审批页提供未解决问题列表。管理员可以指定负责人、改为处理中，并记录处理结果。
完成待办必须有负责人和处理结果。系统保存创建时间和完成时间。
问题类型包括缺资料、检索失败、回答错误和业务投诉。

试用指标统计近24小时，单集合读取上限10000条：

- 明确解决率：明确标记已解决的知识回答数 / 全部知识提问数。
- 解决反馈覆盖率：有明确解决反馈的知识回答数 / 全部知识提问数。
- 重复提问率：同会话中规范化文本重复的次数 / 全部知识提问数。
- 转交率：系统要求负责人处理的知识提问数 / 全部知识提问数。
- 知识补充时长：已完成的缺资料待办，从创建到完成的平均小时数。

未知解决状态不视为已解决。重复率只检查文本重复，不判断语义相似。
耗时、首字时间、模型调用次数和估算费用按 RAG、闲聊、降级分别统计。
默认关闭参数自调。指标采集和人工审批仍可使用。
即使显式启用自调，也要求至少100条反馈；恶化时仅恢复可核验的历史稳定参数，没有可靠快照就保持当前参数。
本地嵌入和重排依赖延迟到首次模型使用时加载。

## 完整流程评测

`app/rag_eval/artifacts/intent_trial_draft.json` 包含200条样本草稿。标签来自代理，尚未人工复核。
样本覆盖产品问题、纯闲聊、混合输入、指代追问和投诉。

先校验题库，不调用模型：

```powershell
python -m app.rag_eval.full_flow
```

业务人员应检查问题、历史、产品标签和答案检查词。复核后将每条样本的 `annotation_status` 改为 `reviewed`，并填写 `reviewed_by`。
不能仅批量填入审核人名称。复核意味着审核了实际问题和标签。

真实评测会调用模型并通过查询接口写入历史。仅使用独立测试数据库和测试知识库。
准备好已复核题库后执行：

```powershell
python -m app.rag_eval.full_flow --cases reviewed-cases.json --allow-external --revision candidate --output output/candidate-report.json
```

评测器经真实 HTTP 入口执行。历史提问也通过接口执行，不注入标准主体或固定 HyDE 输出。
路由门槛要求至少200条已复核样本、知识诉求召回率不低于99%、关键产品问题误入闲聊为零、HTTP错误为零。
路由门槛通过不表示答案质量通过。缺少答案检查词时报告明确标为仍需审核。

比较两个版本时使用同一份题库：

```powershell
python -m app.rag_eval.full_flow --cases reviewed-cases.json --allow-external --revision candidate --baseline output/baseline-report.json --output output/candidate-report.json
```

同时比较知识诉求召回率、产品识别、答案检查、P95耗时、模型调用数和费用。

## 开发检查

```powershell
python -m compileall -q app tests
python -m pyflakes app tests
python -m pytest -q
node --test tests/frontend/chat_resume.test.cjs
```

默认离线测试禁止外部连接。真实模型、数据库和人工标签验收需单独完成。
