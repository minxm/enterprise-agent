# RAG 离线评估

评估数据和指标都在 Python 进程内计算。默认运行不调用 SiliconFlow，也不读写 PostgreSQL 里的生产知识库。

## 设计

评估与生产检索分开：

- 语料和标准答案放在 `agent-python/evaluation_data/enterprise_contract_eval.json`。
- `expected_chunk_ids` 只能引用这份文件里的 `passages.chunk_id`。
- 离线模式使用词面重叠排序，再调用现有 `RAGService`。对话端是一个回显用户提示的替身，不会访问模型 API。
- 真实模式必须加上 `--live`。它调用现有 `EmbeddingClient`、`ChatClient` 和 `RAGService`，只在内存中为评估片段计算相似度，不把这些片段写入数据库。

`VectorRetriever.search(tenant_id, query, top_k=...)` 返回 `list[RetrievalHit]`。`RAGService.ask(tenant_id, question, top_k=...)` 返回 `RAGAnswer`。评估器按这两个同步接口注入实现。Day 17 到 Day 19 的检索、向量化和问答逻辑没有改动。

项目里目前没有 LangSmith 客户端，也没有已有的 evaluation 模块。本评估不接入 LangSmith。

## 数据集

根对象字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `dataset_name` | 字符串 | 报告中的数据集名称 |
| `passages` | 数组 | 评估专用片段，至少一条 |
| `cases` | 数组 | 评估样本，至少一条 |

片段字段：`chunk_id`、`document_id`、`title`、`content`。两个 ID 都是 UUID，并且 `chunk_id` 在文件内唯一。

样本字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `case_id` | 字符串 | 样本编号，文件内唯一 |
| `question` | 字符串 | 用户问题 |
| `expected_chunk_ids` | UUID 数组 | 标准片段。拒答样本可以为空 |
| `expected_facts` | 字符串数组 | 希望在答案中出现的事实短语，可以为空 |
| `expected_answer` | 字符串或 null | 可选的参考答案 |
| `should_refuse` | 布尔值 | 是否应当拒答。不能用 `0` 或 `1` 代替 |
| `description` | 字符串 | 这条样本覆盖的场景 |

需要回答的样本必须带有标准片段，并且至少提供 `expected_facts` 或 `expected_answer` 之一。引用了文件中不存在的片段会在加载时失败。

当前文件有 12 条样本，覆盖：单片段、多片段、知识库无答案、易混淆阈值、同义改写、多条件、资料冲突。内容是合成的合同、付款和审批规则。

## 指标

K 必须是 1 到 20 的整数，与 `RAGService` 的 `top_k` 上限一致。只使用检索结果的前 K 条。重复的 `chunk_id` 会记入 `duplicate_chunk_ids`。Recall 按集合计算，重复项不会提高命中数量。MRR 使用第一次出现的位置，位置从 1 开始。

### Hit Rate@K

标准片段非空时，前 K 条里至少出现一个标准片段记为 1，否则记为 0。总体值是这些样本的平均数。

### Recall@K

`|前 K 条去重后的结果 ∩ 标准片段| / |标准片段|`。

### MRR@K

第一个标准片段的排名倒数。前 K 条都没有标准片段时记为 0。

`expected_chunk_ids` 为空的拒答样本不参与这三项。它们不会被记成检索失败，总体分母也不包括它们。

### 引用

答案中的 `[1]`、`[2]` 这类编号必须是不带前导零的正整数，并且不能大于本次返回的 `sources` 数量。`[3]` 在只有两个来源时记为越界。`[1, 2]` 或 `[01]` 记为格式不符合约定。没有引用编号本身不算格式错误。

### 拒答

拒答不只匹配一句固定文案。答案中出现“知识库资料不足”“没有检索到与该问题相关”“无法根据企业知识库”等不足提示时，视为拒答。`should_refuse` 为真时应拒答；为假时应返回去掉空白后仍非空、且未被判为拒答的答案。

### 事实短语

`expected_facts` 中的字符串如果原样出现在答案里，记为匹配。这只是关键词是否出现，不等于事实正确，也不能证明答案忠实于检索上下文。报告里的字段名是 `fact_keyword_coverage`，并带有同样的说明。

每条样本都保留自己的检索和答案明细。单个样本抛错时，报告记录该 `case_id` 和异常信息，其余样本继续计算。失败样本不进入平均分。

## 运行离线评估

在 `agent-python` 目录执行：

```bash
python -m scripts.evaluate_rag --dataset evaluation_data/enterprise_contract_eval.json --top-k 5 --output reports/rag-offline.json
```

输出目录必须已经存在。参数缺失、K 不合法或数据集不合法时退出码为 2。有样本执行失败时退出码为 1。这条命令不会读取 SiliconFlow，也不会连接 PostgreSQL。

单元测试：

```bash
python -m pytest tests/test_evaluation.py
```

## 运行真实 RAG 评估

只有显式加上 `--live` 才会调用模型：

```bash
python -m scripts.evaluate_rag --dataset evaluation_data/enterprise_contract_eval.json --top-k 5 --output reports/rag-live.json --live
```

运行前需要配置 `SILICONFLOW_API_KEY`、`SILICONFLOW_BASE_URL` 和 `CHAT_MODEL`。未配置 API Key 时命令会退出，并且不会创建模型客户端。真实评估会消耗 Embedding 和 Chat 的调用额度。评估片段只在内存中向量化。

## 查看 JSON 报告

报告包含：

- `evaluation_id`
- `dataset_name`
- `mode`：`offline` 或 `live`
- `k`
- `total_cases`
- `successful_cases`
- `failed_cases`
- `retrieval_metrics`
- `answer_metrics`
- `per_case_results`
- `failures`
- `evaluation_time`：完成时的 UTC 时间
- `elapsed_seconds`

比较两次运行时，使用相同的数据集和相同的 K，再对比 `retrieval_metrics` 与 `answer_metrics`。

## 局限性

- 离线分数衡量的是词面重叠和提示回显，不是线上模型的回答质量。
- 关键词覆盖率不能当作 Faithfulness。
- 拒答判断是短语规则，可能漏掉没有使用这些说法的拒答，也可能把引用了这些说法的正常说明判成拒答。
- 真实评估没有把语料写入 pgvector，因此它检查的是同一套 `RAGService` 和模型接口，不是生产库上的 HNSW 或 SQL 检索计划。
- 评估器不会把单个失败吞掉，但也不会在第一条失败时停止后面的样本。
