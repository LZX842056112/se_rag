# 评测标准与执行手册

本文是 `app/rag_eval` 的验收口径。评测分离线与线上两部分，两侧用同一个 `case_id` 对齐。

## 1. 离线检索指标

| 指标 | 口径 | 实现 |
|---|---|---|
| `Recall@5` / `Recall@10` | 前 k 条命中的 gold chunk 数 / gold chunk 总数 | `metrics.compute_recall_at_k` |
| `MRR` | 第一个相关 chunk 的排名倒数；未命中记 0 | `metrics.compute_mrr` |
| `NDCG@5` / `NDCG@10` | 二值相关性下的归一化折损累计增益 | `metrics.compute_ndcg_at_k` |
| `precision` / `recall` / `must_hit_rate` | 辅助指标，保留历史口径 | `metrics.compute_chunk_metrics` |

分层结果至少覆盖：普通向量召回、HyDE 召回、RRF 融合、重排后；agentic 覆盖候选证据与最终证据。

## 2. 离线生成指标

Ragas 0.4.3，固定 judge 模型、temperature 0：

- `faithfulness`
- `answer_relevancy`
- `context_precision`
- `context_recall`
- `factual_correctness`（保留为补充指标）

上下文为空时，`faithfulness` / `context_precision` / `context_recall` 记 `missing_contexts`，不伪造 0。

## 3. 答案评测集

- 规模：100-300 条。
- 每条必须包含：`case_id`、`query`、`reference`、`source.path`、`source.pdf_page`、`source.section`、`source.excerpt`、`source.sha256`、`annotation_status`、`reviewed_by`。
- 全部样本必须 `reviewed` 才能作为基线。
- 审计命令：
  ```powershell
  python -m app.rag_eval.dataset_audit --require-reviewed --min 100 --max 300
  ```
- 审计失败时，评测报告只能标记为探索性，不能作为发布基线。

当前状态：`answer_quality_draft.json` 40 条，0 条复核；`intent_trial_draft.json` 200 条，但只有意图与关键词，没有标准答案和出处。**这是当前最大的阻塞项。**

## 4. LLM-as-judge 与人工校准

- judge 模型固定：`EVAL_LLM_MODEL`，temperature 0，记录在报告 `scoring_config`。
- rubric 版本固定：`RUBRIC_VERSION = ragas-0.4.3-default-v1`，记录在 `scoring_config.rubric_version`。
- 人工校准：至少 30 条样本，人工打分与 judge 分数按同一 rubric 比较。
- 通过门槛：容差内一致率 ≥ 0.8，Cohen's kappa ≥ 0.6。
- 校准命令：
  ```powershell
  python -m app.rag_eval.calibration --report answer-quality-report.json `
    --labels human_labels.json --metric faithfulness --output calibration.json
  ```

## 5. 线上指标

`app/evolution/trial_metrics.py` 输出：

- `resolution_rate`
- `downvote_rate` / `downvote_feedback_coverage`
- `handoff_rate`
- `first_token_p95_ms`
- `input_tokens_total` / `output_tokens_total`
- `cost_usd` 与 `cost_coverage`
- `case_id_coverage` / `feedback_case_id_coverage`

`cost_usd` 只有在 `LLM_MODEL_PRICES_JSON` 配置了模型单价时才有效；否则只报 token。

## 6. 离线与线上对齐

线上回放同一道离线题时，请求体携带 `case_id`。链路会把它写入 `query_runs` 与反馈事件。

- 对齐检查：
  ```powershell
  python -m app.rag_eval.alignment --cases app/rag_eval/artifacts/answer_quality_draft.json `
    --online online_runs.json --output alignment.json
  ```
- 也支持从 Mongo 直接取近 N 小时记录：
  ```powershell
  python -m app.rag_eval.alignment --mongo-hours 24
  ```
- 没有 `case_id` 的线上记录不能参与离线/线上对比。

## 7. 回归门禁

每次改动至少执行：

```powershell
python scripts/run_eval_regression.py
```

严格模式（数据集必须达到 100-300 条已复核样本）：

```powershell
python scripts/run_eval_regression.py --strict-dataset `
  --baseline output/baseline.json --current output/current.json
```

CI：`.github/workflows/eval-gate.yml` 在 push / PR 上运行离线单测和数据集审计。
真实 RAG 回归需要 Milvus / Mongo / MinIO 与模型密钥，请在自托管 runner 上运行上面的命令。

## 8. 当前阻塞项

1. 答案集只有 40 条，且 0 条复核；未达到 100-300 条门槛。
2. 200 条全流程集没有标准答案与出处，不能替代答案评测集。
3. 线上反馈只有少量演示数据，`case_id` 覆盖还不能形成有效对齐样本。
4. 还没有已复核基线与正式回归报告。
