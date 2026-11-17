# RAG 评估

Day 20 的默认评估在 Python 进程内计算。它不调用 SiliconFlow，也不读写 PostgreSQL。Day 21 在显式加上 `--live-pgvector` 之后，把评估语料写入专用租户，并用真实的 PostgreSQL + pgvector 检索。两条链路的报告用 `mode` 区分，不会放在一起算平均分。

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
- `--live` 没有把语料写入 pgvector。它检查的是同一套 `RAGService` 和模型接口，不是数据库里的向量检索。
- `--live-pgvector` 只评估检索，不调用 Chat Model，因此没有答案质量分数。
- 评估器不会把单个失败吞掉，但也不会在第一条失败时停止后面的样本。

## Day 20 与 Day 21

| | Day 20 离线 | Day 20 `--live` | Day 21 `--live-pgvector` |
| --- | --- | --- | --- |
| 检索 | 词面重叠 | 内存余弦相似度 | `VectorRetriever.search` |
| 向量存放 | 不生成 | 进程内存 | PostgreSQL `public.vector(1024)` |
| Chat Model | 不调用 | 调用 | 不调用 |
| 默认是否执行 | 是 | 否 | 否 |

离线报告的 `mode` 是 `offline`。内存真实模型报告的 `mode` 是 `live`。pgvector 报告的 `mode` 和 `evaluation_mode` 都是 `pgvector`。准备数据和清理数据使用 `pgvector-prepare`、`pgvector-cleanup`，这两份报告的 `retrieval_metrics` 为空，不能拿来和检索分数平均。

## 评估租户和数据隔离

评估租户固定为 `00000000-0000-4000-8000-00000000e021`。它只写在评估模块里，不进入应用配置，也不是业务租户。

所有读写都走现有的 `tenant_transaction`，由它设置 `app.tenant_id`，因此 RLS 仍然生效。评估代码不访问 `business` schema，也不新增表或修改 migration。

每条标准片段对应一篇 DOCX。`file_id` 由数据集名称和标准 `chunk_id` 通过 UUID v5 稳定生成。打包时把 ZIP 条目时间固定下来，同一段文字每次生成的文件校验和相同，入库服务才能认出这是同一篇文档。再次运行时如果校验和未变，并且唯一的 Chunk 已经有模型 `Qwen/Qwen3-Embedding-0.6B` 和 1024 维向量，就不再调用 Embedding，也不再插入文档。

清理只删除当前数据集这些 `file_id`。同一个评估租户里、属于其他数据集的文档会留下。其他租户的文档因为租户条件、RLS 和清理前的租户校验，都不会被这条命令删除。清理包在一个事务里；中途失败会整批回滚。

`--cleanup` 默认关闭。不加 `--live-pgvector` 时，清理参数会被拒绝。

## 数据如何进入数据库

1. 读取 Day 20 的 `enterprise_contract_eval.json`。其中的 `expected_chunk_ids` 是标准片段编号，不是数据库生成的 Chunk UUID。
2. 把每条 `passages.content` 打成一篇 DOCX，先用现有的提取和 `chunk_text` 检查它恰好变成一个 Chunk，并且正文与标准片段一致。对不上就停止，不用猜测的 ID 计分。
3. `KnowledgeIngestionService.ingest` 写入 `agent.knowledge_document` 和 `agent.knowledge_chunk`。
4. 向量缺失、模型不对或维度不是 1024 时，调用 `VectorizationService.vectorize`。评估代码不自己写 `embedding` 列，也不使用随机向量。
5. 记下标准片段 ID、`document_id` 和数据库 `chunk_id` 的对应关系。

当前语料都是短句，默认 800 字的切分会保持一段对一段。如果以后的片段被切成多段，准备阶段会失败。

## pgvector 检索

每条问题调用现有的 `VectorRetriever.search`。查询向量仍由 `EmbeddingClient.embed_query` 生成，文档向量仍是入库时 `embed` 的结果，两边的文本格式保持 Day 18 的差异。

返回顺序不重新排序。数据库 Chunk UUID 先映射回标准片段 ID，再交给 Day 20 的 `score_retrieval` 和 `aggregate_retrieval`。空的 `expected_chunk_ids` 不进入 Hit Rate、Recall、MRR 的平均。检索返回空列表是未命中，记 0 分；抛出的异常记入 `failures`，不记成 0 分。

`embedding_latency_ms` 是 `search` 内部查询向量那一次调用的耗时。`retrieval_latency_ms` 是 `search` 总耗时减去它，里面包含 SQL 和少量 Python 开销。入库和向量化的时间记在 `prepare_elapsed_seconds`，不混进单题检索耗时。这些数字只描述当前机器和当前配置。

## 运行真实向量评估

在 `agent-python` 目录执行。输出目录必须已经存在。

```bash
python -m scripts.evaluate_rag --dataset evaluation_data/enterprise_contract_eval.json --top-k 5 --output reports/pgvector.json --live-pgvector
```

只入库、不检索：

```bash
python -m scripts.evaluate_rag --dataset evaluation_data/enterprise_contract_eval.json --top-k 5 --output reports/pgvector-prepare.json --live-pgvector --prepare-only
```

清理当前数据集在评估租户中的文档：

```bash
python -m scripts.evaluate_rag --dataset evaluation_data/enterprise_contract_eval.json --top-k 5 --output reports/pgvector-cleanup.json --live-pgvector --cleanup
```

需要 `POSTGRES_HOST`、`POSTGRES_DB`、`AGENT_APP_PASSWORD`。除了只清理之外，还需要 `SILICONFLOW_API_KEY`。缺配置、参数冲突或报告目录不存在时退出码为 2。某条检索执行失败时退出码为 1。这条命令不会调用 Chat Model。

## pgvector 报告字段

在 Day 20 字段之外增加：

| 字段 | 说明 |
| --- | --- |
| `evaluation_mode` | `pgvector`、`pgvector-prepare` 或 `pgvector-cleanup` |
| `tenant_id` | 评估租户 |
| `embedding_model` | 入库向量的模型名 |
| `database_vector_dimension` | 数据库中的向量维度 |
| `top_k` | 本次 K |
| `hit_rate_at_k` / `recall_at_k` / `mrr_at_k` | 与 `retrieval_metrics` 中的三项相同 |
| `embedding_latency_ms` / `retrieval_latency_ms` / `total_latency_ms` | 成功样本的耗时合计，单位毫秒 |
| `per_case_results` | 每题的 `expected_chunk_ids`、`actual_chunk_ids`、映射后的 `retrieved_chunk_ids`、`similarities` 和三项指标 |

`answer_metrics.evaluated` 为 false，`case_count` 为 0。这表示没有做答案评估，不是拒答得了 0 分。

## 当前局限

- 标准片段必须能被现有切分器切成恰好一段。做不到时整次评估停止。
- 检索分数反映的是当前 Embedding 模型和 pgvector 的余弦排序，没有 Hybrid Search、Reranker 或 HNSW。
- 耗时包含本机网络和进程开销，不能当作生产容量数据。
- 评估文档留在专用租户里，直到显式执行 `--cleanup`。清理不会顺便删掉其他租户。
