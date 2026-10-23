# Agentic RAG 离线评测

此入口用于准备 P0 基线和执行 P4 配对实验。默认命令只校验本地文件。它不会连接模型、MongoDB 或 Milvus。

当前没有新的真实质量结果。现有答案题库和意图题库仍是草稿。新增扩展题也未经人工审核。单元测试只能证明接口行为，不能证明模型质量提高。

## 1. 校验题库

```powershell
.venv/Scripts/python.exe -m app.rag_eval.agentic_comparison
.venv/Scripts/python.exe -m app.rag_eval.agentic_comparison --cases app/rag_eval/artifacts/agentic_extension_draft.json
```

第一条命令校验现有答案题库。第二条命令校验四条扩展题。

扩展题覆盖跨章节读取、证据冲突、无答案和过期政策。题目保留 `annotation_status=draft`。`source_review` 说明来源限制。`additional_sources` 保存第二处来源。校验器检查全部来源文件的 SHA-256。

后三类题需要独立测试资料。它们的 `scenario_requirements.status` 当前为 `not_created`。文档没有声称真实语料含有矛盾报价或过期报价。建立资料后，人工检查资料和参考答案。将夹具状态改为 `verified`，并填写实际的 `snapshot_id`。真实评分会拒绝缺少夹具的扩展题。`--allow-unreviewed` 不能跳过夹具检查。

人工审核题目时，同时审核原文页码、摘录、答案和问题类型。只有全部来源已审核，才可将题目标为 `reviewed`。不要只修改状态字段。

## 2. 固定隔离快照

真实采集需要一个 JSON 文件，例如 `output/eval-snapshot.json`：

```json
{
  "snapshot_id": "trial-001",
  "corpus_version": "manuals-v1",
  "index_version": "chunks-v1",
  "approved_faq": {"enabled": false}
}
```

版本号由实验人员指定。文档文件摘要由程序重新计算。程序没有验证文档版本号与 Milvus 切片索引的完整对应关系。因此，实验人员必须先冻结索引，并核对导入清单。不能只改版本号来声称索引已验证。

需要读取已审批 FAQ 时，启用独立读开关。关闭知识写入、进化调度和自动调参：

```powershell
$env:MONGO_DB_NAME = 'eval_trial'
$env:CHUNKS_COLLECTION = 'eval_chunks'
$env:ITEM_NAME_COLLECTION = 'eval_items'
$env:EVOLUTION_COLLECTION = 'eval_faq'
$env:EVOLUTION_RECALL_ENABLED = 'true'
$env:EVOLUTION_WRITE_ENABLED = 'false'
$env:EVOLUTION_SCHEDULE_ENABLED = 'false'
$env:EVOLUTION_AUTO_TUNE_ENABLED = 'false'
```

集合必须预先存在。采集器不会创建 FAQ 集合。三个 Milvus 集合必须相互独立，并使用 `eval_` 前缀。MongoDB 数据库也必须使用该前缀。查询历史和指标可以写入隔离数据库。知识发布和自动调参必须关闭。

FAQ 快照需要以下字段：

```json
{
  "enabled": true,
  "collection": "eval_faq",
  "content_sha256": "实际计算的64位十六进制摘要",
  "approval_sha256": "实际计算的64位十六进制摘要",
  "index_version": "实际审批条目的索引版本"
}
```

`content_sha256` 覆盖 Milvus 中全部 active FAQ 的标量字段。向量不计入该摘要。`approval_sha256` 覆盖治理模块确认可读取的审批及来源版本清单。程序从数据库读取这些内容，并重新计算摘要。它不会把声明中的摘要直接当作真实摘要。

可以在隔离环境中调用 `capture_faq_snapshot(settings)` 获取实际值。这是外部只读操作。先核对隔离配置，再执行该操作。空审批清单使用 `index_version=empty`。多个索引版本使用 `mixed`。这两种状态不会伪装成单一已验证索引版本。

采集前、每个模式前和每个模式后都检查 FAQ 内容。摘要变化会中止实验。该检查不能提供数据库事务级快照。实验期间仍须停止外部导入和人工修改。

## 3. 执行配对实验

为评分模型单独设置 `EVAL_LLM_MODEL`。评分模型不会自动使用回答模型。现有接口地址和密钥配置可以复用。

```powershell
$env:EVAL_LLM_MODEL = 'your-independent-judge-model'
.venv/Scripts/python.exe -m app.rag_eval.agentic_comparison --eval-snapshot output/eval-snapshot.json --allow-external --output output/agentic-comparison.json
```

草稿调试还需要 `--allow-unreviewed`。草稿报告不能作为可信基线。

该 CLI 先运行 `legacy`，再运行 `agentic`。它在独立进程内设置模式，并为实验设置 `gray_percent=100`。结束时恢复配置。服务进程不能通过 Python API 执行该模式切换。不要在正在提供服务的进程内调用私有采集函数。

两个模式使用相同题库、文档摘要、FAQ 快照、回答模型和评分配置。共享清单还记录代码摘要、Git 版本、工作区修改状态、提示词摘要、策略版本、决策模型和预算。首次直接检索也由在线 Agent 计入预算。旧链没有同等的在线工具预算约束。因此，报告保留相同预算配置，不声称旧链已执行相同预算约束。

每次查询只传入问题和新的评测会话 ID。产品标签、参考答案和预期结果不会进入运行时。评分阶段才使用参考答案。

程序生成报告和 `.snapshots.json` 文件。快照保存实际答案上下文、`evidence_manifest`、`agentic_trace`、停止原因和 Agent 指标。中间轨迹与最终答案证据分开保存。

## 4. 重用答案快照

```powershell
.venv/Scripts/python.exe -m app.rag_eval.agentic_comparison --snapshots output/agentic-comparison.snapshots.json
.venv/Scripts/python.exe -m app.rag_eval.agentic_comparison --snapshots output/agentic-comparison.snapshots.json --allow-external --output output/agentic-rescore.json
```

第一条命令只校验。第二条命令调用评分模型，但不会再次查询产品知识。

校验器检查题目 ID、问题、文档摘要、共享清单摘要和两个模式。缺少一个模式时，程序拒绝比较。内容摘要用于检查文件一致性，不能证明文件来自可信作者。需要留存审计文件时，请另行使用受控存储。

## 5. 读取报告

忠实度、相关性和事实正确性分别报告。程序不生成总分。每项指标都有覆盖率。查询失败、路由错误和空答案保留在总样本数中。无法评分时，值为 `null`，并保留原因。

对比使用同一题目的有效评分差值。报告包含配对样本数、配对覆盖率和分类结果。两个模式中任一评分缺失时，该题不进入差值均值，但仍保留在配对覆盖率分母中。至少有两对有效结果时，报告正态近似误差范围。少量样本和单次运行不足以证明稳定收益。

运行指标独立报告完成率、工具错误、停止原因、模型调用数、token、首字延迟、总延迟 p50/p95 和费用。失败请求仍计入请求数和延迟统计。未知单价或缺失用量会产生 `null` 费用。部分已知费用不会伪装成完整总费用。当前费用来自在线查询指标，未包含存储、索引构建和评分模型费用。

引用支持率、步骤完整率、正确拒答率和目标证据 Recall@K 尚无新的自动评分器。当前扩展题只进入现有三项质量指标。人工审核这些维度后，才能确定政策测试和灰度门槛。

现有 `answer_quality` 入口保持兼容。开启 FAQ 读取时，其新报告保存真实 FAQ 摘要。基线比较会拒绝不同 FAQ 快照。关闭 FAQ 读取的旧报告仍可使用原有格式。
