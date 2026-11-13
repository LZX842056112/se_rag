# RAG 离线评测

通过 HTTP 调用查询服务，计算检索指标与 RAGAS 生成指标，并支持基线回归对比。

## 1. 准备

1. 启动查询服务（8001）。
2. 安装评测依赖（只用检索指标可跳过）：

```bash
uv sync --group eval
```

3. 在 `.env` 配置独立 judge 模型，避免回答模型自评：

```text
EVAL_LLM_MODEL=
EVAL_LLM_BASE_URL=
EVAL_LLM_API_KEY=
EVAL_LLM_TIMEOUT_SECONDS=120
```

`EVAL_LLM_BASE_URL` / `EVAL_LLM_API_KEY` 为空时复用 `OPENAI_BASE_URL` / `OPENAI_API_KEY`。

## 2. 评测集格式

JSON 数组或 JSONL，每题字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `case_id` | 是 | 唯一标识，不能重复 |
| `question` | 是 | 提问原文 |
| `ground_truth` | 是 | 标准答案，RAGAS 需要 |
| `expected_item_names` | 否 | 预期主体，用于人工核对 |
| `gold_chunk_ids` | 否 | 标注的相关切片，检索指标需要 |
| `must_hit_chunk_ids` | 否 | 必须命中的关键切片 |
| `tags` | 否 | 标签，可用 `--tags` 过滤 |

示例见 `app/rag_eval/datasets/sample_cases.json`，使用的是当前知识库中的真实切片 ID。
换一套知识库时需要替换 `gold_chunk_ids` 与 `must_hit_chunk_ids`，否则检索指标会偏低。

示例数据集共 100 题，覆盖 12 个主体：

| 维度 | 数量 | 说明 |
|---|---|---|
| 总题数 | 100 | 92 题有切片标注，8 题只检查行为 |
| 正常场景 | 89 | 操作步骤 30、参数设置 14、故障排查 14、功能说明 13、网络连接 8、安全信息 7、电池与充电 7、维护保养 3 |
| 边界场景 | 11 | 模糊输入 3、超范围 3、否定问题 3、长输入 2 |
| 覆盖主体 | 12 | HAK 180、Brother 打印服务器、5 款华为笔记本、2 款华为台式机、华为显示器、Aolynk 网桥、H3C 网关 |

`边界-超范围`、`边界-否定问题` 和部分 `边界-模糊输入` 没有 `gold_chunk_ids`。
这些题只检查执行状态与回答内容，不计算检索指标。

## 3. 运行

```bash
# 检索 + RAGAS
python -m app.rag_eval.cli run --dataset app/rag_eval/datasets/sample_cases.json

# 只跑检索指标
python -m app.rag_eval.cli run --dataset ... --no-ragas

# 指定服务地址与并发
python -m app.rag_eval.cli run --dataset ... --base-url http://127.0.0.1:8001 --concurrency 2
```

默认输出目录 `output/eval/`，会写：

- `eval_<时间戳>.json`：完整报告
- `eval_<时间戳>.md`：人工查看的摘要，包含总体结论、逐题结果与基线对比
- `latest.json`：最近一次报告

运行过程中，每完成一题会打印一行关键结果。
示例：`recall@5=0.8 | must_hit@5=1.0 | faithfulness=1.0 | 1820ms`。
没有标注的题显示 `无标注`。

## 4. 指标

检索指标（按层 `embedding` / `hyde` / `rrf` / `reranked` 分别统计）：

- `precision@k`：前 k 条中相关切片比例
- `recall@k`：前 k 条覆盖了多少标注切片
- `mrr@k`：第一条命中的倒数排名
- `ndcg@k`：二值相关度的排序质量
- `must_hit@k`：关键切片命中比例

生成指标（RAGAS 0.4.3）：

- `faithfulness`
- `answer_relevancy`
- `context_precision`
- `context_recall`

## 5. 基线回归

```bash
# 首次运行并保存基线
python -m app.rag_eval.cli run --dataset ... --save-baseline output/eval/baseline.json

# 后续运行与基线对比；有回归时退出码为 1
python -m app.rag_eval.cli run --dataset ... --baseline output/eval/baseline.json --fail-on-regression

# 只对比两份已有报告
python -m app.rag_eval.cli compare --baseline output/eval/baseline.json --current output/eval/latest.json
```

对比默认阈值为 `0.02`，可用 `--max-drop` 调整。延迟指标越小越好，其余指标越大越好。
对比结论会写入 Markdown 报告的 `## 基线对比` 章节。

## 6. 结果查看

Markdown 报告按顺序展示以下内容：

| 章节 | 内容 |
|---|---|
| `## 总体结论` | 状态、判定依据、关键指标 |
| `## 检索指标` | 各层 `precision` / `recall` / `mrr` / `ndcg` / `must_hit` 与样本数 |
| `## RAGAS 生成指标` | 四项生成指标与样本数 |
| `## 延迟` | 平均与 P95 |
| `## 逐题结果` | 每题状态、证据数、`recall@k`、`must_hit@k`、`faithfulness`、延迟、失败原因 |
| `## 基线对比` | 回归与提升项、对比指标数、阈值 |

总体结论有四种状态：

- `通过`：全部样本执行成功，`recall@5 >= 0.8` 且 `must_hit@5 >= 1.0`。
- `需关注`：样本执行成功，但关键指标低于目标。
- `未通过`：有样本执行失败。
- `无法判定`：没有可计算的检索指标，例如全部样本都未标注。

没有 5 时，结论使用配置中的最大 k。
没有标注的指标在报告中显示 `无标注`，不会显示空值。

## 7. 说明

- 评测请求带 `include_evidence=true`，服务端才会回传证据正文与分层 chunk_id。
- 检索指标依赖标注的 `gold_chunk_ids`；没有标注的样本不参与对应指标平均。
- RAGAS 单指标失败只记录原因，不影响其他指标；失败样本不计入平均。
- judge 模型与 rubric 固定后，跨版本结果才可比。
