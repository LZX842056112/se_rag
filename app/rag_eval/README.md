# RAG 离线评测

评测工具通过 HTTP 调用查询服务。它检查回答行为、检索质量和 RAGAS 分数。它也可以比较两次运行。

## 准备

1. 启动查询服务。默认地址为 `http://127.0.0.1:8001`。
2. 如需 RAGAS，请安装依赖。

```powershell
uv sync --group eval
```

3. 如需 RAGAS，请在 `.env` 中配置独立的 judge 模型。

```text
EVAL_LLM_MODEL=
EVAL_LLM_BASE_URL=
EVAL_LLM_API_KEY=
EVAL_LLM_TIMEOUT_SECONDS=120
```

若 judge 接口地址或密钥为空，工具分别使用 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`。报告记录模型名和接口地址的哈希值。

如需记录知识库版本，请在启动查询服务前设置 `EVAL_KNOWLEDGE_BASE_VERSION`。请使用能唯一识别当前知识库的版本号。可通过 `GET /api/health?include_evidence=true` 检查服务提供的来源信息。

- `service_source_sha256` 记录服务启动时的 `app` 运行源码指纹。Git 部署包含已跟踪及未忽略的新文件。无 Git 元数据时，服务扫描部署目录的源码。指纹覆盖 Python、提示词和页面资源。
- 无 Git 元数据的部署应设置 `EVAL_SERVICE_COMMIT`。请填写构建服务时使用的提交号。
- 用 `python -m app.api.http.query_server` 启动时，服务记录实际启动的 worker 数。直接用 Uvicorn 启动 Redis 模式时，请将 `EVAL_QUERY_WORKER_COUNT` 设置为与 `--workers` 相同的数值。

## 数据集

使用 JSON 数组或 JSONL。请为每题填写唯一的 `case_id`、`question` 和 `ground_truth`。其他字段均可省略。

- `expected_outcome` 指定预期结果。可用值为 `answered`、`clarification`、`handoff`、`chitchat` 和 `out_of_scope`。默认值为 `answered`。
- `expected_item_names` 指定应确认的主体。填写非空列表时，返回的主体集合必须与标注相同。比较时忽略顺序、首尾空格和字母大小写。
- `required_answer_terms` 指定回答必须包含的词或短语。每项均须出现。
- `forbidden_answer_terms` 指定回答禁止包含的词或短语。出现任意一项即失败。两项词语检查均使用不区分字母大小写的子串匹配。
- `gold_chunk_ids` 标注相关切片。检索指标使用这些切片 ID。
- `must_hit_chunk_ids` 标注必须命中的切片。
- `chunk_equivalents` 可为已标注切片列出等效 ID。只有同型号、同条件且能支持同一事实的切片才可并入。每组计一次命中。
- `tags` 指定标签。使用 `--tags` 选择包含任一指定标签的题目。

[示例数据集](datasets/sample_cases.json)包含 300 题。其中 270 题带有手册证据，30 题检查预期行为。更换知识库后，请核对切片 ID 和参考答案。对有证据支持的否定回答，请标注 `answered`。

## 运行

```powershell
uv run python -m app.rag_eval.cli run --dataset app/rag_eval/datasets/sample_cases.json
uv run python -m app.rag_eval.cli run --dataset app/rag_eval/datasets/sample_cases.json --no-ragas --concurrency 2
```

工具在请求完成时显示 HTTP 进度。请求完成后，工具逐题执行 RAGAS。默认输出目录为 `output/eval`。

- `eval_<时间戳>.json` 保存完整报告。元数据包括数据集 SHA-256、选中用例、标签、k、并发、指标版本、服务来源和模型信息。
- `eval_<时间戳>.md` 显示结论、检查覆盖率、逐题结果和基线对比。
- `latest.json` 保存最近一次报告。

## 分数和验收

评测发送同步查询，设置 `include_evidence=true`。报告逐题记录实际查询路径、实际结果、各阶段切片 ID 和候选数。

检索指标根据实际执行的阶段计算。`keyword` 记录已确认产品内的正文和章节关键词候选。`reranked` 记录截断前的排序。`selected` 记录动态截断后的切片。`final` 记录业务策略处理后用于作答的切片。报告也记录 `embedding`、`hyde`、`rrf` 和 `agentic_retrieval`。条数只统计有 ID 的切片。RAGAS 上下文另含实际使用的联网证据。工具跳过未执行的阶段。阶段执行后没有结果时，有标注的用例计零分。

报告用 `manual_rerank_input` 和 `local_rerank_input` 记录两种重排方式收到的切片。用这些字段定位候选来源和降级路径。

- `precision@k` 为前 k 位中命中的唯一证据数除以 k。
- `recall@k` 为前 k 位中命中的相关切片数除以相关切片总数。
- `mrr@k` 为首次命中排名的倒数。
- `ndcg@k` 衡量相关切片的排名质量。重复切片仅在首次出现时得分。
- `must_hit@k` 为前 k 位中命中的必命中切片数除以必命中切片总数。

验收使用标注的应回答题和 `final` 阶段。要求平均 `recall@5 >= 0.8`，平均 `must_hit@5 = 1.0`。若 `--ks` 缺少 5，工具会加上 5。缺少对应标注的题目不参与该指标的平均。

RAGAS 使用 `evaluation.contexts` 中实际送给生成模型的证据。应回答的用例必须有有效回答和上下文。启用 RAGAS 时，每道应回答题都需要 `faithfulness`、`answer_relevancy`、`context_precision` 和 `context_recall` 四个有限分数。报告分别显示 HTTP 失败、行为不符、上下文缺失和评分失败。通过行为检查的非回答用例显示 `expected_no_score`。

评判模型的输出上限为 4096 token。如果输出达到上限并被截断，工具用同一模型和同一输入对该指标重试一次。重试的输出上限为 8192 token。`ragas_retries` 记录指标、触发原因、token 上限和重试结果。

HTTP 延迟包括客户端等待和重试间隔。报告逐题记录 HTTP 尝试次数及失败原因。服务提供降级标记时，报告保留标记，并按原因统计用例数。服务提供队列或阶段耗时时，报告同时记录服务端计时和样本数。

- 退出码 `0` 表示通过，或当前选择缺少可判定的质量检查。
- 退出码 `1` 表示 HTTP、行为、覆盖率或检索验收失败。指定 `--fail-on-regression` 后，基线回归也返回 `1`。
- 退出码 `2` 表示工具无法生成报告或执行比较，例如服务健康检查失败。

### 复核异常零分

检查报告中的问题、答案和实际评分上下文。记录支持答案的原句和人工结论。保留原评分。以下两题来自 2026 年 11 月 12 日 19:51 的 300 题报告。

- `eval_20261112_029` 的 `answer_relevancy` 为 `0.0`。第一个上下文来自 Brother HAK 180 手册第 1 页。它要求使用设备随附的电源线。答案直接回答了电源线类型。
- `eval_20261112_172` 的 `faithfulness` 为 `0.0`。第一个上下文来自华为擎云 S540 手册第 8 页。它说明关机或睡眠时充电更快。该上下文直接支持答案。

报告会列出零分指标和用例 ID。2026 年 11 月 13 日 23:48 的报告中，`eval_20261112_252` 的忠实度为 `0.0`。华为擎云 W585 手册第 9 页说明，BIOS 开机密码丢失后只能更换主板才能进入系统。答案与手册一致。

## 创建并比较基线

先对当前数据集和目标服务运行评测，生成新版基线。保存基线需要完整的 HTTP、行为和必需分数覆盖率。无法判定质量的运行不能保存为基线。检索质量低于目标时，工具仍可保存覆盖完整的测量。

```powershell
uv run python -m app.rag_eval.cli run --dataset app/rag_eval/datasets/sample_cases.json --save-baseline output/eval/baseline.json
uv run python -m app.rag_eval.cli run --dataset app/rag_eval/datasets/sample_cases.json --baseline output/eval/baseline.json --fail-on-regression
uv run python -m app.rag_eval.cli compare --baseline output/eval/baseline.json --current output/eval/latest.json
```

质量分数下降超过 `--max-drop` 时，工具报告回归。默认阈值为 `0.02`。HTTP 平均延迟或 P95 增加超过 `--max-latency-increase-ms` 时，工具报告延迟回归。默认阈值为 `1000` 毫秒。基线中的指标消失或样本数下降时，工具报告覆盖率回归。

基线和当前报告须有相同的数据集 SHA-256、选中用例 ID、指标版本和响应评测版本。字段缺失或不一致时，报告标记两次运行不可比，并保留数值差。退出码由当前报告自身的验收结果决定。实际查询路径或服务来源有差异时，报告显示警告。若旧基线缺少必要字段，请用当前工具和同一套数据集重新生成基线。

## 多 worker 验证

性能对比应使用同一题集、知识库、模型和客户端并发数。每组先预热。记录首段文本延迟、完整响应 P95、吞吐量、错误率和进程内存。HTTP 和 SSE 的计时口径应分别报告。

运行跨进程测试时，请启动独立 Redis 实例，并选择空数据库。以下命令使用本机端口 `16397` 和数据库 `15`。

```powershell
$env:MULTI_WORKER_REDIS_URL = 'redis://127.0.0.1:16397/15'
uv run python -m pytest -m e2e tests/e2e/test_multi_worker_redis.py
```

测试会启动两个独立服务进程。它检查会话互斥、SSE 重连、退出后的租约回收和调度器接管。模型、文档服务和计划任务使用测试替身。
