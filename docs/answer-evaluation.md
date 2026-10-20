# 离线答案质量评测

使用 Ragas 评估答案。现有检索评测和完整 HTTP 入口评测继续保留。
本工具用于分析质量，不自动判定是否允许上线。

## 安装

在 `codex/rag-internal-trial` 的独立工作区执行：

```powershell
uv sync --group eval
```

没有 uv 时，可以在项目的虚拟环境中执行：

```powershell
python -m pip install "ragas==0.4.3" "langchain-community==0.4.1"
```

必须同时安装这两个版本。`langchain-community` 0.4.2 移除了 Ragas 0.4.3 仍会导入的 VertexAI 模块。
相关性指标复用现有 BGE-M3 稠密向量。它不会另行调用远程向量接口。
向量计算在单独的工作进程中运行，并在批次内复用模型。
该进程会单独加载同一套 BGE 模型，需要相应内存或显存。
向量计算超时时，工具终止该进程，不再为本批次启动向量任务；其他可用指标继续评分。

## 校验题库

```powershell
python -m app.rag_eval.answer_quality
```

也可以使用统一测试类：

```python
from app.rag_eval import RagEvalTester

result = RagEvalTester().run_answer_eval()
```

默认操作仅检查样本和来源文件。它不加载 Ragas 或模型，不连接数据库，也不写报告。
独立题库可通过 `--cases 文件路径` 指定。

默认题库为 `app/rag_eval/artifacts/answer_quality_draft.json`，包含 40 条单轮产品问题：

- HAK180：16 条。
- HL3040：12 条。
- H3C ER2100：12 条。

题型包括事实查询、参数、操作步骤和安全注意事项。
来源为项目 `doc/` 中的四份手册：HAK180 使用说明书、HAK180 产品安全手册、HL3040 网络说明书和 H3C ER2100 用户手册。
每条样本记录参考答案、章节、证据摘录、来源文件 SHA-256 和 PDF 页序号。

`pdf_page` 从 PDF 的第一页开始计数，封面也计入。它不是书内印刷页码。
来源文件变化后，校验会失败。此时必须重新核对参考答案和出处。

所有初始样本都是代理整理的草稿。人工检查实际问题、答案和出处后，才可将 `annotation_status` 改为 `reviewed`，并填写 `reviewed_by`。
不要仅批量填写复核人名称。原有 200 条意图草稿不属于本答案题库。

## 配置评测模型

在这个独立工作区的 `.env` 中设置：

```dotenv
EVAL_LLM_MODEL=填写实际评测模型名称
EVAL_LLM_TIMEOUT_SECONDS=120
```

模型名称必须明确填写。工具不会自动使用回答模型。
接口地址和密钥默认复用 `OPENAI_BASE_URL` 和 `OPENAI_API_KEY`。
如需覆盖，可设置 `EVAL_LLM_BASE_URL` 和 `EVAL_LLM_API_KEY`。
接口需支持 OpenAI 兼容的结构化 JSON 输出。

项目配置会从 `.env` 读取，并覆盖同名进程环境变量。修改配置后，在新进程中运行工具。
评测报告不记录密钥。报告会注明评测模型是否与生成快照中的回答模型相同。

## 准备独立测试资源

采集答案会执行真实查询流程，并写入会话历史和运行记录。
请为采集进程配置独立数据库与集合：

```dotenv
MONGO_DB_NAME=eval_se_rag
CHUNKS_COLLECTION=eval_chunks
ITEM_NAME_COLLECTION=eval_item_names
EVOLUTION_ENABLED=false
```

上述三个资源名必须以 `eval_` 开头。两个 Milvus 集合名必须不同。
检查会在导入查询图和执行查询之前完成。
使用独立的命令行进程运行。不要在正在提供请求的服务进程中切换资源配置。

沿用现有数据库、模型和存储服务的连接配置。
先用同一套测试配置启动导入服务，再通过现有导入流程，将上述四份 PDF 导入测试知识库。
不要将样本导入正在使用的业务知识库。
本工具不会自动导入文档、修改知识或启动自进化任务。

## 先运行 5 条样本

只有显式增加 `--allow-external`，才会执行查询和模型评分。
首次接入可用草稿检查流程：

```powershell
python -m app.rag_eval.answer_quality --allow-external --allow-unreviewed --limit 5 --output output/answer-smoke.json
```

每个问题使用一个新的 `eval-` 会话。查询只接收原始问题，不接收参考答案、标准产品名称或固定检索结果。
生成节点保存实际放入答案提示词的证据，包括实际使用的父章节和联网内容。
公开查询 API 不增加证据全文字段。

采集后先保存 `output/answer-smoke.snapshots.json`，然后评分。
即使评分接入失败，已采集的快照仍可用于复评分。

## 复评分与基线

只重新评分，不重新查询：

```powershell
python -m app.rag_eval.answer_quality --allow-external --allow-unreviewed --limit 5 --snapshots output/answer-smoke.snapshots.json --output output/answer-rescore.json
```

快照要求题库、样本 ID、问题和来源文档摘要完全一致。
使用 `--limit 5` 采集的快照，也必须使用同一子集复评分。
修改参考答案或复核状态会改变题库版本。此时应重新采集对应版本的快照。

完成题库人工复核后，生成第一份基线：

```powershell
python -m app.rag_eval.answer_quality --allow-external --output output/answer-baseline.json
```

修改回答模型、检索或提示词后，可以重新采集并比较：

```powershell
python -m app.rag_eval.answer_quality --allow-external --baseline output/answer-baseline.json --output output/answer-candidate.json
```

基线比较要求题库、来源文件和评分配置一致。
改变裁判模型、评分参数或评分用向量模型时，需要建立新基线。
未复核或评分不完整的报告不能作为可信基线。
如果候选运行评分不完整，工具仍保存报告，并将 `comparison_error` 标为 `incomplete_scoring`。

## 阅读报告

三项评分分别报告，不合成为总分：

- `faithfulness`：答案中的事实是否有实际检索证据支持。
- `answer_relevancy`：答案是否回答问题。它使用问题与反向生成问题的向量相似度；该值可能为负。
- `factual_correctness`：答案与参考答案的事实一致程度，使用 F1 模式。

报告给出逐题评分、均分、有效评分数和有效评分比例。
有效评分比例的分母包含全部样本。真实零分会参与均分。
空证据、缺少参考答案、评分超时、无效 JSON 和无效数值均使用 `null`，并记录原因。
查询错误、路由错误和空答案分别计数。错误不会被当成成功样本。
`review_queue` 列出未复核标注或存在缺失评分的样本。其余逐题低分也应人工检查。

报告和快照包含问题、答案及证据全文。默认保存在 Git 忽略的 `output/` 目录。
它们记录生成版本、代码摘要、来源文档摘要、生成模型和评分配置。
模型自动评分用于定位问题。检查低分样本后，再决定是否修改检索、提示词或回答模型。
