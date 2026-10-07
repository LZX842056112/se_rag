# RAG 评估模块说明

## 1. 模块目标

`app/rag_eval` 是当前项目的统一评估子系统，目标是把评估逻辑收敛到一个独立包里，并且对外只暴露一个测试类，方便：

- 在当前项目里统一维护评估代码
- 在另一个结构相近的 RAG 教学项目中整体迁移
- 让“测试数据准备”和“批量评测执行”流程更清晰
- 复制整个包后直接实例化类，不依赖 `tests` 目录和命令行入口

当前假设另一个项目满足下面两个前提：

- 查询链路的 `state` 字段约定一致
- 查询节点名称与调用方式一致

如果这两个前提成立，通常复制整个 `app/rag_eval` 目录，再调整少量导入路径即可完成迁移。

## 2. 包结构

```text
app/rag_eval/
  __init__.py
  runner.py
  dataset.py
  split_dataset.py
  split_metrics.py
  metrics.py
  tester.py
  README.md
```

各文件职责如下：

- `tester.py`
  - 提供统一测试类 `RagEvalTester`
  - 对外只暴露两个方法
- `dataset.py`
  - 维护常量与题库文件读写
  - 维护联网占位结果
  - 保留历史手工切片 `build_import_chunks()`（已不参与评测主路径）
- `split_dataset.py`
  - 把评测知识组织成 Markdown 文档，经**真实切分器**产出黄金切片
  - 由**内容关键词**推导 gold，不依赖切片序号
  - 读取已缓存的 MinerU 解析产物，用于切分质量基线
- `split_metrics.py`
  - 切分质量纯函数指标：源内容覆盖率、表格完整率、碎片率、面包屑覆盖率、页码溯源覆盖率、父子关联
- `metrics.py`
  - 提取 `chunk_id`
  - 计算主体命中率
  - 计算每层检索的 `precision / recall / must_hit_rate`
- `runner.py`
  - 通过真实导入链路写入评测数据
  - 执行单条评测
  - 执行批量评测与汇总
  - 生成中文报告（含切分质量段）

## 3. 当前评估流程

这套评估分两步执行。

### 第一步：准备评测数据

`runner.insert_batch_eval_dataset()` 会复用现有导入链路：

- `node_item_name_recognition`
- `node_bge_embedding`
- `node_import_milvus`

切片不再手工编写，而是由 `split_dataset.split_eval_document()` 调**真实切分器**
（`section_splitter.split_blocks`，与线上导入同一套逻辑）产出。因此：

- 切分策略的改动会被评估覆盖到；
- `chunk_id` 是确定性哈希，可在本地算出来，**不再需要「先入库再回查自增主键」**；
- gold 由内容关键词推导，切片数量变化不会让标注失效。

执行后会完成两件事：

1. 将测试 chunks 写入 `item_name` 集合和 `chunks` 集合（父块写入 MongoDB）
2. 生成批量评测样本文件 `app/rag_eval/artifacts/hak180_eval_cases.json`

> 注意：回查 chunk 集合只用于**校验写入成功**。过滤条件必须使用「实际写入的主体名」——
> 主体识别会把新名归并到库内标准名（如 `HAK 180` → `HAK 180 烫金机`），硬编码常量会稳定查到 0 行。

### 第二步：执行批量评测

`runner.run_batch_eval()` 会读取题库，然后逐条执行：

- `node_search_embedding`
- `node_search_embedding_hyde`
- `node_rrf`
- `node_rerank`

每个节点返回的数据都会写回查询 `state`，评估模块再从 `state` 中读取：

- `embedding_chunks`
- `hyde_embedding_chunks`
- `rrf_chunks`
- `reranked_docs`

最后用标注好的 `gold_chunk_ids` 和 `must_hit_chunk_ids` 做对比，统计指标。

### 切分质量（报告新增段）

`runner.collect_split_quality()` 会对**评测文档**与**已缓存的真实解析产物**分别计算切分质量，
写入报告的 `切分质量` 段。这是切分策略变更后唯一可比的证据：

| 指标 | 含义 | 期望 |
| --- | --- | --- |
| 源内容覆盖率 | 源块文本都能在某个切片中找到 | 100% |
| 表格完整率 | 每个数据行都出现在「带表头」的切片里 | 100% |
| 碎片率 | 分别给出碎片块**数量占比**与**内容占比** | 看内容占比 |
| 章节面包屑覆盖率 | 切片带 `heading_path` 的比例 | 100% |
| 页码溯源覆盖率 | 切片带有效 `page` 的比例 | 结构感知路径 100% |
| 父子关联 | 每个子块的 `parent_id` 都能在父块列表中找到 | 100% |

碎片率口径说明：结构感知切分下，短小节会天然产出短切片——**不同标题的切片不合并**，以保证切片标题与
引用溯源精确（合并会让标题与实际内容不符）。因此验收看的是碎片块**承载的内容占比**，而不是数量占比。

## 4. 如何获取每层召回结果

评估系统不会额外去日志或数据库反查，而是直接读取查询 `state`：

- `embedding_chunks`：普通检索召回结果
- `hyde_embedding_chunks`：HyDE 检索召回结果
- `rrf_chunks`：RRF 融合后的候选结果
- `reranked_docs`：最终重排后保留的上下文结果

这也是它容易迁移的原因之一：只要新项目保留同样的字段名，评估逻辑就不用重写。

## 5. 如何理解“交给大模型的数据”

当前评估涉及两类模型输入：

### 5.1 HyDE 的 LLM 输入

- 输入：`rewritten_query`
- 输出：假设性答案 `hyde_answer`
- 用途：把“问题 + 假设答案”一起向量化，再去做 HyDE 检索

### 5.2 reranker 模型输入

- 输入：`[question, text]` 对
- 来源：
  - 本地 Milvus 候选 `rrf_chunks`
  - 联网占位文档 `web_search_docs`
- 用途：给最终候选重新打分排序

注意：

- 当前评估系统并没有走“最终回答生成”
- 它评估到 `reranked_docs` 这一层就结束
- 所以它评估的是“检索与重排质量”，不是最终回答质量

## 6. 推荐用法

推荐直接使用测试类：

```python
from app.rag_eval import RagEvalTester

tester = RagEvalTester()
```

### 6.1 导入评测数据

```python
insert_result = tester.run_insert_test_data()
```

### 6.2 执行批量评测

```python
eval_result = tester.run_eval()
```

## 7. 这两个方法分别做什么

### 7.1 `tester.run_insert_test_data()`

负责：

- 通过真实导入链路写入评测数据
- 查询并确认 item_name / chunk 数据成功入库
- 生成批量评测题库

返回值包含：

- `item_name`（**实际写入的主体名**，可能是归并后的库内标准名）
- `item_rows`
- `chunk_rows`
- `case_count`
- `split_quality`

### 7.2 `tester.run_eval()`

负责：

- 读取题库
- 执行 embedding / HyDE / RRF / rerank 四层评测
- 统计主体命中率、召回率、精确率、必命中率
- 汇总切分质量
- 输出报告文件

返回值包含：

- `eval_results`
- `summary`
- `split_quality`
- `report_path`

## 8. 为什么适合迁移

这个包内部已经自带：

- 测试样本定义
- 评测数据导入逻辑
- 批量评测逻辑
- 指标统计
- 报告输出

因此迁移时优先以 `app/rag_eval` 为最小复制单元，不再依赖 `tests` 目录下的脚本，也不需要额外保留命令行入口。

## 9. 迁移到另一个项目时要改什么

如果另一个项目结构接近，优先复制 `app/rag_eval` 整个目录。

通常只需要检查这些点：

- `runner.py` 里导入的节点路径是否一致
- `create_default_state` 和 `create_query_default_state` 路径是否一致
- `milvus_gateway` 与配置模块路径是否一致
- 项目中的 `state` 字段名是否仍然是：
  - `embedding_chunks`
  - `hyde_embedding_chunks`
  - `rrf_chunks`
  - `reranked_docs`
  - `item_names`

如果这些约定不变，这套评估系统基本可以直接复用。
