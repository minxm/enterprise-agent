# RAG 评估

Day 20 的默认评估在 Python 进程内计算。它不调用 SiliconFlow，也不读写 PostgreSQL。Day 21 在显式加上 `--live-pgvector` 之后，把评估语料写入专用租户，并用真实的 PostgreSQL + pgvector 检索。两条链路的报告用 `mode` 区分，不会放在一起算平均分。

## 设计

评估与生产检索分开：

- 语料和标准答案放在 `agent-python/evaluation_data/enterprise_contract_eval.json`。
- `expected_chunk_ids` 只能引用这份文件里的 `passages.chunk_id`。
- 离线模式使用词面重叠排序，再调用现有 `RAGService`。对话端是一个回显用户提示的替身，不会访问模型 API。
- 真实模式必须加上 `--live`。它调用现有 `EmbeddingClient`、`ChatClient` 和 `RAGService`，只在内存中为评估片段计算相似度，不把这些片段写入数据库。

`VectorRetriever.search(tenant_id, query, top_k=...)` 返回 `list[RetrievalHit]`。`RAGService.ask(tenant_id, question, top_k=...)` 返回 `RAGAnswer`。评估器按这两个同步接口注入实现。Day 20 编写评估时没有改动 Day 17 到 Day 19 的检索、向量化和问答逻辑。Day 24 保持这个调用形式，并允许额外传入 `strategy`；不传时默认仍是 vector。

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

所有读写都走现有的 `tenant_transaction`，由它设置 `app.tenant_id`，因此 RLS 仍然生效。评估代码不访问 `business` schema。Day 21 不新增表，也不改已有 migration。Day 22 只新增 `003_chunk_bigram.sql`，给已有的 `knowledge_chunk.content` 增加字符二元组函数和 GIN 索引。

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
- Day 21 的 pgvector 报告只反映当前 Embedding 模型和余弦排序，没有 Reranker 或 HNSW。Hybrid Search 在下一节单独比较。
- 耗时包含本机网络和进程开销，不能当作生产容量数据。
- 评估文档留在专用租户里，直到显式执行 `--cleanup`。清理不会顺便删掉其他租户。

## Day 22：Hybrid Search

Hybrid Search 在同一次问题里做两路检索，再用 RRF 合并。向量路继续调用现有的 `VectorRetriever.search`。关键词路是独立的 `KeywordRetriever.search`。`HybridRetriever.search` 先跑向量，再跑关键词，各自使用自己的 `tenant_transaction`，不共用一条数据库连接。两路都为空时返回空列表。

`RetrievalHit.similarity` 仍然只表示余弦相似度。融合结果使用 `HybridRetrievalHit`，单独保存 `vector_rank`、`keyword_rank`、`rrf_score`，以及向量路带来的 `similarity`。某一路没有命中时，对应 rank 为空。

默认每一路先取 20 条候选，再融合后截断到请求的 Top-K。评估比较里的 Vector Only 仍按 Top-K=5 调用，和 Day 21 使用同一口径。Keyword Only 也按 5 计分。Hybrid 的候选宽度是 20，最终指标仍是 @5。

### 关键词检索为什么不用全文检索

检查当时的数据库是 PostgreSQL 16.15。镜像里可以安装 `pg_trgm` 1.6，但扩展没有安装。`agent_migrator` 不是超级用户，也没有数据库的 CREATE 权限，执行 `CREATE EXTENSION pg_trgm` 会被拒绝。`zhparser`、`pg_jieba` 不在镜像里。已安装的扩展只有 `plpgsql` 和 `vector`。`knowledge_chunk` 上原来没有全文或三元组索引。

因此没有启用 PostgreSQL 全文检索，也没有安装 `pg_trgm`。关键词检索把查询切成相邻字符二元组，外加长度至少为 2 的英文字母和数字串。SQL 用 `position` 统计一个片段里命中了多少个不重复的词项，再用 `agent.character_bigrams(content)` 上的 GIN 索引限制候选。这不是中文分词，也不能把 `pg_trgm` 的三字符相似度说成已经具备。

不足两个汉字、或不足两个英文字符的查询产生不了词项，返回空列表，不算错误。合同编号会整段保留，所以 `HT2024` 可以精确出现在正文里；同时仍会拆出二元组，供 GIN 索引使用。相似度没有使用 0 到 1 的模糊阈值，至少要命中 1 个词项才进入候选，避免把完全无关的片段收进来。相同命中数按 `chunk_id` 升序，同一次查询的顺序是稳定的。

`003_chunk_bigram.sql` 只新增函数、执行权限和索引，不改已有的 001、002，也不改表结构。迁移器把每个脚本放在一个事务里，所以不能使用 `CREATE INDEX CONCURRENTLY`。建索引期间会挡住 `knowledge_chunk` 的写入，当前评估语料只有十几行，锁的时间很短。不要为了这条索引去改数据库角色权限。

### RRF

默认常数 k=60。排名从 1 开始：

`RRF(d) = Σ 1 / (k + rank(d))`

同一个 Chunk 同时出现在两路结果里时，两路分数相加。同一路里重复出现的 Chunk 只保留第一次，不重复累加。排序先按 `rrf_score` 降序，分数相同再按 `chunk_id` 的文本升序。

### 三种策略

| 策略 | 检索 | 计分时的 K |
| --- | --- | --- |
| Vector Only | 现有 pgvector 余弦检索 | 5 |
| Keyword Only | 字符二元组，不调用 Embedding | 5 |
| Hybrid | 两路各取最多 20 条，RRF 后取 5 | 5 |

三种策略共用 Day 20 的 `score_retrieval` 和 `aggregate_retrieval`。空的 `expected_chunk_ids` 仍不进入平均。标准答案不因分数高低而改动。比较报告把三项指标分策略存放，不把三种策略再平均成一个数。

### 运行比较

在 `agent-python` 目录执行。输出目录必须已经存在。

```bash
python -m scripts.evaluate_rag --dataset evaluation_data/enterprise_contract_eval.json --top-k 5 --output reports/hybrid-day22.json --live-pgvector --search-strategy all
```

`--search-strategy` 还可以是 `vector`、`keyword` 或 `hybrid`。不写该参数时，`--live-pgvector` 仍是 Day 21 的向量报告。比较必须同时带 `--live-pgvector`，并且不能和 `--prepare-only` 或 `--cleanup` 一起用。`keyword` 不调用 Embedding，也不调用 Chat Model。`vector`、`hybrid` 和 `all` 会调用 Embedding，不调用 Chat Model。

报告里的 `strategies` 按策略给出总体指标、每题的 `expected_chunk_ids`、数据库 `actual_chunk_ids`、映射后的 `retrieved_chunk_ids` 和 `ranks`。`comparison` 并列三项指标。`focus_case` 记录“超过 50 万元付给新供应商”这一题是否召回标准片段 `00000000-0000-4000-8000-000000000008`。

### 耗时怎么读

三种策略分开计时、分开求和。Hybrid 是串行的：`embedding_latency_ms` 只计查询向量，`vector_latency_ms` 是向量 `search` 减去查询向量之后的时间，`keyword_latency_ms` 是关键词 SQL，`fusion_latency_ms` 是 RRF，`total_latency_ms` 是这三段接起来的总时间。不要把三策略的耗时加在一起当成一次请求。入库向量化的时间仍在准备阶段，不混进这些数字。Keyword Only 的 Embedding 和向量耗时为空。

### 当前局限

- 字符二元组会把“发票”和“专用发票”联系起来，也会让共享“审批”“万元”等相邻字的片段一起进入候选。它不理解词边界。
- 查询很短时没有词项，关键词路为空，Hybrid 只剩向量结果。
- 索引不是并发创建的。数据量变大后，再次部署这条 migration 会在事务里锁表。
- 普通单元测试使用替身，不能当成 PostgreSQL 的检索结果。真实排序以带 `postgres` 标记的集成测试和 `--live-pgvector` 报告为准。

## Day 23：Reranker

Reranker 不检索、不写数据库、也不生成答案。它只给已经召回的候选打相关性分数。链路是：

向量检索 → 关键词检索 → RRF → Reranker → 最终 Top-K。

`HybridRetriever` 本身不会调用 Reranker。`HybridRerankRetriever` 先用 Hybrid 取 `RERANKER_TOP_N` 条候选，默认 20 条，再重排序并截断到请求的 K。只重排已经截断的 Top-5 会丢掉第 6 名以后的正确片段，所以评估没有这样做。

`rerank_score` 写在 `RerankedHit` 上。它不会覆盖 `similarity` 或 `rrf_score`。不同模型的分数范围不一样，这里不把它当成 0 到 1 的概率。分数相同按 `chunk_id` 升序。

### 和前几路的区别

| 步骤 | 做什么 | 不做什么 |
| --- | --- | --- |
| Vector Search | 查询向量和 pgvector 余弦距离 | 不看精确词面 |
| Keyword Search | 字符二元组命中数 | 不是中文分词 |
| RRF | 按两路名次融合 | 不重新读正文 |
| Reranker | 用模型对候选正文和问题打分 | 不访问数据库，不生成答案 |

### 模型配置

服务商是 SiliconFlow，接口是 `POST {base}/rerank`，鉴权是 `Authorization: Bearer`。这不是 Embedding 的 `/embeddings`。当前文档列出的文本模型是 `Qwen/Qwen3-Reranker-0.6B`、`Qwen/Qwen3-Reranker-4B`、`Qwen/Qwen3-Reranker-8B`。默认使用 `Qwen/Qwen3-Reranker-0.6B`。

环境变量：

| 变量 | 作用 |
| --- | --- |
| `RERANKER_ENABLED` | 应用是否启用。评估命令显式选择策略时单独判断 |
| `RERANKER_MODEL` | 上面三个模型之一 |
| `RERANKER_BASE_URL` | 留空则用 `SILICONFLOW_BASE_URL` |
| `RERANKER_API_KEY` | 留空则用 `SILICONFLOW_API_KEY`。不写入代码和报告 |
| `RERANKER_TIMEOUT_SECONDS` | 超时，默认 30 秒，最多重试 3 次 |
| `RERANKER_TOP_N` | 送进 Reranker 的候选数，默认 20 |

未配置 Reranker 时，原来的 Vector 和 Hybrid 命令照常运行。请求在数据库事务结束之后才发出。超时、连接失败、429 和 5xx 会重试，其它 4xx 不重试。

### 评估

在 `agent-python` 目录执行：

```bash
python -m scripts.evaluate_rag --dataset evaluation_data/enterprise_contract_eval.json --top-k 5 --output reports/rerank-day23.json --live-pgvector --search-strategy rerank
```

这次比较 Vector、Hybrid、Hybrid + Reranker。`--search-strategy all` 仍是 Day 22 的 vector、keyword、hybrid，不会调用 Reranker。K、数据集和 `expected_chunk_ids` 与前面相同。拒答样本不进入平均。

每题的 `rerank_change` 记录重排前的候选顺序、重排后的 Top-K，以及正确片段被提前、被后移、被挤出 Top-K 或新进入 Top-K。`rerank_changes` 统计发生过这些变化的题目数。

耗时按策略分开累加。Hybrid + Reranker 的总时间是查询 Embedding、向量检索、关键词检索、RRF 和 Reranker 串行相加。不要把三种策略的耗时再加总。

本次真实比较使用 `Qwen/Qwen3-Reranker-0.6B`，12 道题全部成功，2 道拒答不进入平均。报告是 `reports/rerank-day23.json`。

| 策略 | Hit Rate@5 | Recall@5 | MRR@5 |
| --- | --- | --- | --- |
| Vector | 1.0 | 0.9667 | 1.0 |
| Hybrid | 1.0 | 1.0 | 0.875 |
| Hybrid + Reranker | 1.0 | 1.0 | 0.95 |

12 道题的串行耗时合计，单位毫秒：

| 策略 | Embedding | 向量检索 | 关键词 | RRF | Reranker | 合计 |
| --- | --- | --- | --- | --- | --- | --- |
| Vector | 1249.872 | 122.804 | — | — | — | 1372.674 |
| Hybrid | 1067.891 | 108.439 | 89.016 | 1.967 | — | 1267.390 |
| Hybrid + Reranker | 1076.995 | 113.607 | 87.234 | 2.438 | 2476.912 | 3757.621 |

Reranker 把 3 道题里的正确片段提前，没有把已在 Top-5 的正确片段挤出去。MRR 从 0.875 升到 0.95，主要来自“付款想往下走，得先让谁同意？”和“合同从提交到签署的顺序是什么？”。发票片段 `...0008` 在 Hybrid 和 Rerank 里都保持第 5 名。

### 当前局限

- 分数含义由模型决定，不能和余弦相似度直接比大小。
- 候选超过 `RERANKER_TOP_N` 的部分不会送给模型。
- Reranker 只看传入文本，不能补回检索阶段被租户过滤掉的片段。
- 普通测试使用假的分数或假的 HTTP 响应，不能当成真实模型的排序结果。

## Day 24：RAG 问答接入三种检索策略

`RAGService` 不再自己做检索、RRF 或重排序。配置了策略目录时，`ask` 把问题和租户交给 `RetrievalStrategies`，再把返回的最终片段交给原来的上下文裁剪和 Chat Model。没有策略目录时，调用方式与 Day 19 相同，直接使用注入的 `VectorRetriever`。

默认策略是 `vector`。即使配置了 Reranker，也不会自动改成 Hybrid + Reranker。调用方可以在测试里显式传入 `strategy="hybrid"` 或 `strategy="hybrid_rerank"`。`tenant_id` 必须是服务端传入的 UUID，客户端不能用别的租户编号绕过过滤。

### 配置

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `RAG_RETRIEVAL_STRATEGY` | `vector` | `vector`、`hybrid`、`hybrid_rerank` |
| `RAG_RETRIEVAL_TOP_K` | 5 | 放进上下文的最终片段数，1 到 20 |
| `RAG_RETRIEVAL_CANDIDATE_K` | 20 | Hybrid 和 Reranker 的候选池，不能小于 Top-K |
| `RAG_RERANKER_FAILURE_POLICY` | `strict` | `strict` 或 `fallback` |

`strict` 在 Reranker 的接口或响应错误时直接失败，不编造重排序结果。`fallback` 使用这一次 Hybrid 已经返回的候选，按原顺序截取 Top-K，并记录失败原因和 trace id。参数错误、程序错误和租户错误不会被当成可降级错误。日志只保留错误类别和截断后的原因，不写 API Key，也不写完整片段正文。

### 上下文和引用

只有最终 Top-K 进入 Prompt。片段仍放在用户消息里，系统提示继续声明这些内容不可信、不能当指令执行。来源编号按送进模型的顺序从 1 重新编号。重排序改变顺序后，`[1]` 对应该顺序的第一条，而不是向量检索的第一条。超过 8000 字时从末尾丢掉或截断，编号只覆盖实际送出的片段。模型如果写出不存在的编号，答案原文保留，评估把它记为越界引用，不会补一条假来源。检索为空时仍返回原来的“没有检索到”拒答，并且不调用 Chat Model。检索异常和生成异常分开抛出。

### 答案层评估

在 `agent-python` 目录执行：

```bash
python -m scripts.evaluate_rag --dataset evaluation_data/enterprise_contract_eval.json --top-k 5 --output reports/rag-day24.json --live-pgvector --search-strategy answer
```

三种策略使用同一份问题、同一组 `expected_chunk_ids` 和同一个 Chat Model。`--search-strategy all` 与 `rerank` 保持 Day 22、Day 23 的检索比较，不调用 Chat Model。历史报告 `pgvector-day21.json`、`hybrid-day22.json`、`rerank-day23.json` 不覆盖。

自动 Faithfulness 没有实现。`fact_keyword_coverage` 只检查预期事实字符串是否出现在答案里。`cited_fact_support` 再要求该字符串也出现在被引用片段的正文里。两者都不是 Faithfulness。拒答是否正确使用固定短语判断，模型换一种说法说明“资料里没有”时，可能被记成未拒答。

本次真实运行使用 `Qwen/Qwen3-Embedding-0.6B`、`Qwen/Qwen3-Reranker-0.6B` 和 `Qwen/Qwen2.5-7B-Instruct`。12 道题都成功，2 道拒答题不进入 Hit Rate、Recall、MRR。报告是 `reports/rag-day24.json`。

| 策略 | Hit Rate@5 | Recall@5 | MRR@5 | 拒答准确率 | 引用合法率 | 事实覆盖 | 引用事实支持 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Vector + RAG | 1.0 | 0.9667 | 1.0 | 0.8333 | 1.0 | 0.9667 | 0.9667 |
| Hybrid + RAG | 1.0 | 1.0 | 0.875 | 0.75 | 1.0 | 1.0 | 1.0 |
| Hybrid + Reranker + RAG | 1.0 | 1.0 | 0.95 | 0.9167 | 1.0 | 1.0 | 1.0 |

`faithfulness` 为 null。引用合法率 1.0 表示答案里的 `[n]` 都落在实际提供的来源范围内。12 道题的串行耗时合计，单位毫秒：

| 策略 | 检索 | Reranker | Chat Model | 合计 | 候选数 | 上下文数 |
| --- | --- | --- | --- | --- | --- | --- |
| Vector + RAG | 1789.379 | — | 85053.447 | 86844.136 | 60 | 60 |
| Hybrid + RAG | 1683.642 | — | 34331.937 | 36017.496 | 144 | 60 |
| Hybrid + Reranker + RAG | 1600.623 | 3695.949 | 22397.636 | 27696.024 | 144 | 60 |

三种策略分开计时。Vector 的 Chat 合计里，大额付款那一题单次用了 62643.5 毫秒，所以这一列不能拿来判断 Chat Model 通常比另外两种策略慢。本语料的片段很短，最终上下文都是 5 条，没有触发 8000 字截断。发票片段 `...0008` 仍不在 Vector 的 Top-5，Hybrid 和 Hybrid + Reranker 都把它留在第 5 名。Vector 的答案因此漏掉“增值税专用发票”，并引用了另一条片段里的发票说法。

### 当前局限

- 拒答短语没有覆盖“知识片段中没有关于……的信息”这类说法，食堂和班车题的实际拒答多数没被算进拒答准确率。
- Hybrid 的盖章冲突答案写了“当前知识库资料不足”，短语把它当成拒答，尽管事实和引用都在。
- 字符串覆盖不能发现 Vector 把发票要求答成“付款后补交”。
- 降级策略只在单测里用假 Reranker 验证，这次真实评估使用 `strict`，没有发生降级。

## Day 25：答案质量评估 v2

这一天不增加检索算法，也不改写生产答案。评估拆成两层：确定性规则始终运行；Judge 只有显式调用时才运行。Judge 失败或未启用时，语义指标是 null，状态为 `not_evaluated` 或 `failed`，不记 0 分，也不记满分。

旧的短语拒答、`fact_keyword_coverage` 和 `cited_fact_support` 都保留。它们仍是字符串检查，不是 Faithfulness。

### 数据集标注

`enterprise_contract_eval.json` 仍是 12 道题。`expected_chunk_ids`、`expected_facts`、`expected_answer` 和 `should_refuse` 没有改。每题补充了 `expected_answerable`、`expected_response_type`、`expected_claims`、`expected_evidence`、`expected_conflict`、`expected_refusal_reason`。这些字段都对应语料里的原文。主集合没有“部分回答”题，部分回答放在 `evaluation_data/answer_quality_calibration.json`，避免改变检索指标的分母。

### 确定性指标

- Citation Validity：引用编号是否落在实际来源范围内。没有引用时为 null。
- Fact Coverage：预期事实字符串是否出现。说明文字写明这不是正确性。
- Answerability：答案结构类别是否等于 `expected_response_type`。类别有 answer、refusal、partial_answer、conflict、answered_despite_insufficiency。短语命中只记在 `phrase_refusal_hint`，不决定类别。
- Expected Evidence Coverage：预期证据片段有多少出现在实际上下文里。

`overall_faithfulness` 只在 Judge 成功且存在事实 Claim 时计算，等于 Supported Claim Rate。Contradicted Claim Rate 和 Insufficient Evidence Claim Rate 单独列出。没有事实 Claim 时三者都是 null。

Citation Support 看被引用的来源是否被 Judge 判为支持该 Claim。Citation Completeness 看事实 Claim 是否至少有一个支持来源。二者和 Citation Validity 分开。

### Judge

Judge 使用独立客户端，不经过 `RAGService`。默认 `RAG_EVAL_JUDGE_ENABLED=false`。模型默认 `RAG_EVAL_JUDGE_MODEL`，超时 `RAG_EVAL_JUDGE_TIMEOUT_SECONDS`，密钥沿用 `SILICONFLOW_API_KEY`。提示词要求只依据给定片段，冲突时不能选边。输出用 JSON 校验。缺字段、非法来源编号、把“仍给出具体结论”标成拒答，都记为失败。

```bash
python -m scripts.evaluate_answer_quality --output reports/answer-quality-day25.json
python -m scripts.evaluate_answer_quality --judge --output reports/answer-quality-day25.json
```

第一条不调用模型，并用 Day 24 已保存的答案重算确定性指标。第二条只把 10 条人工校准样本送给 Judge，不重新生成 36 条回答。

### 本次结果

确定性规则和 10 条人工类别的一致率是 1.0。这只说明规则覆盖了这批评注，不是生产级语义评估。

Judge 使用 `Qwen/Qwen2.5-7B-Instruct`。10 条里 6 条通过校验，成功率为 0.6。在这 6 条上，回答类别一致率是 0.6667，Claim 判定一致率是 0.5。4 条因字段类型或来源列表不合法而失败，没有计入一致率，也没有被写成 0 分。

明确分歧：

- “食堂补贴 300 元”被 Judge 当成拒答，Claim 被标成 irrelevant。人工判断这是一条没有依据的具体结论。
- “付款后可以补交发票”引用了“付款前必须取得增值税专用发票”。Judge 仍判 supported。人工判断为 contradicted。
- “财务负责人审批”的 Claim 被正确判为 contradicted，但回答类别被标成 conflict。人工类别是 answer。

Day 24 三种策略的回答没有送进 Judge。它们的 `overall_faithfulness` 仍是 null。确定性重算见下表。

| 策略 | 旧拒答准确率 | 回答类别准确率 | 引用合法率 | 事实字符串覆盖 | 预期证据覆盖 |
| --- | --- | --- | --- | --- | --- |
| Vector | 0.8333 | 0.9167 | 1.0 | 0.9667 | 0.9667 |
| Hybrid | 0.75 | 0.9167 | 1.0 | 1.0 | 1.0 |
| Hybrid + Reranker | 0.9167 | 0.9167 | 1.0 | 1.0 | 1.0 |

Vector 的大额付款题没有召回发票片段，答案写成“付款后五个工作日内补交”，并说准入“没有具体说明”。字符串覆盖能看出漏了“增值税专用发票”，但不能单独证明后半句与紧急付款片段相反。这个相反关系要靠 Judge 的 Citation Support，而本次 Judge 在同类校准题上会把相反来源判成 supported，所以不能把该模型的判断当成事实。

### 当前局限

- 10 条校准样本只能做初步对照。
- 同一 Judge 在 temperature 0 下仍会漏字段或把来源编号写成无法校验的类型。
- 确定性类别能认出拒答、冲突和“一边说资料不足、一边给结论”，不能判断发票时间是否说反。
- 没有把 Judge 接进 36 条真实回答。那些题的语义 Faithfulness 继续是未评估。

