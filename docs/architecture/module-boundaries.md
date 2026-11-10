# Enterprise Agent 模块边界

本文定义 frontend、backend-java、agent-python 的职责、调用方式和禁止重叠的行为。系统上下文见 [系统架构](./system-architecture.md)，表结构见 [数据库设计](../database/database-design.md)。

## 1. 不变式

1. 浏览器只访问 frontend 与 backend-java，不访问 agent-python，也不访问 MinIO。
2. `business` 由 Java 用 Flyway 迁移；`agent` 由 Python 迁移。跨 schema 只保存 UUID 逻辑引用，不建跨 schema 外键。
3. 合同状态与审批结论只由 Java 修改。Agent 没有「通过」或「驳回」工具。
4. 用户可见消息在 `business.chat_message`；图的短期状态在 LangGraph checkpoint；执行审计在 `agent` 运行表；长期记忆在 `agent.memory_item`，不能覆盖业务事实。
5. 多租户采用共享库、`business` / `agent` 两个 schema、`tenant_id`、请求上下文和 RLS。运行时数据库角色不能跨 schema 读写。
6. 第一阶段不自动通过合同，不引入消息队列、独立向量库和 Prometheus。

## 2. 三个进程的职责

| 进程 | 负责 | 不负责 |
| --- | --- | --- |
| frontend | 页面、表单、登录态展示、把 Java 的 SSE 显示出来 | 鉴权裁决、合同状态机、直接调用模型和 Agent |
| backend-java | 用户、租户、权限、合同、文件登记、会话消息、审批、业务审计、把 Agent 事件转给浏览器 | 提示词、检索、切分、嵌入、LangGraph、读写 `agent` schema |
| agent-python | 审核图、解析、检索、模型、工具编排、checkpoint、执行记录、长期记忆 | 登录、角色管理、直接更新合同状态、直接写审批决定、读写 `business` schema |

第一阶段一个用户只属于一个租户。第一阶段任何通过或驳回都来自具备 `approval:decide` 的人在 frontend 上的操作。

## 3. 前端边界

现有目录：

| 路径 | 边界 |
| --- | --- |
| `frontend/src/api` | 只封装 backend-java 的 `/api/v1`。不写 agent-python 的基地址 |
| `frontend/src/stores/authStore.ts` | 内存中的当前用户和访问令牌。不把令牌写入 `localStorage` 或 `sessionStorage` |
| `frontend/src/router` | `AuthGuard` 只检查是否持有登录态。无令牌则去登录页 |
| `frontend/src/hooks/useSSE.ts` | 连接 Java 的会话事件接口 |

第一阶段页面：登录、合同列表、合同详情与审核、待办审批、条款库、用户与角色。后两项仅 `tenant_admin` 的菜单展示；接口仍由 Java 鉴权。

前端可以根据 `/api/v1/auth/me` 返回的权限码隐藏按钮。隐藏失败时，用户仍会收到 Java 的 403。前端不复制状态机。合同能否提交，以接口结果为准。

审核报告使用已依赖的 `react-markdown` 渲染 Java 返回的正文。前端不计算风险等级。

## 4. Java 边界

包根路径保持 `com.example.enterprise`。已有 `config` 与 `security` 继续承担基础设施和认证。业务按下面的逻辑模块划分，模块之间通过应用服务调用，不跨模块直接改表。

| 模块 | 职责 |
| --- | --- |
| `security` | 登录、JWT 签发与校验、从数据库加载当前用户权限 |
| `identity` | 用户、角色、用户角色。第一阶段不做部门树 |
| `tenant` | 租户主数据。请求中的租户必须与用户所属租户相同 |
| `contract` | 合同、合同文件、MinIO 写入、文件校验和，以及根据入库 HTTP 结果维护 `index_status` |
| `policy` | 条款库文件登记与 `index_status`。原文件同样进 MinIO |
| `session` | 审核会话、用户可见消息、把 Python 的 SSE 转给浏览器 |
| `review` | 接收审核草稿，并在同一事务里创建待审批任务、推进合同状态 |
| `approval` | 人工决定。条件更新保证单级任务只生效一次 |
| `audit` | 业务审计 |
| `internal` | `/internal/v1` 入站接口。校验内部令牌后，仍按 `X-User-Id` 做用户权限检查 |

`security` 已有的过滤器负责 `/api/v1` 的 Bearer 令牌。`/internal/v1` 不接受用户 JWT，只接受 `X-Internal-Token`。两套入口分开配置。

Java 写出的业务状态只有这些枚举：

| 对象 | 状态 |
| --- | --- |
| 合同 | `draft`、`in_review`、`pending_approval`、`approved`、`rejected`、`withdrawn` |
| 审核会话 | `active`、`waiting_human`、`completed`、`failed`、`cancelled` |
| 审批任务 | `pending`、`approved`、`rejected`、`cancelled` |
| 审核草稿 | `draft`、`superseded` |
| 文件字节 `status` | `pending`、`ready`、`failed` |
| 入库投影 `index_status` | `not_indexed`、`ready`、`failed` |

`index_status` 只表示 Java 看到的入库 HTTP 结果。切片是否存在以 `agent.knowledge_document` 为准。提交审核要求文件 `status = ready` 且 `index_status = ready`。

会话进入 `waiting_human` 后，`session` 模块拒绝新的用户消息。审批意见写在审批接口，不写进对话。

Java 在启动运行和 resume 期间持有 Redis 锁，键为 `lock:review-session:{tenant_id}:{session_id}`，TTL 15 分钟，在 `finally` 中释放。Python 不再加一把运行锁。Java 的幂等缓存键为 `idem:java:{tenant_id}:{idempotency_key}`，保留 24 小时。

Flyway 目录使用 `backend-java/src/main/resources/db/migration`，连接用户是 `business_migrator`。业务连接用户是 `business_app`，`search_path` 为 `business`。

## 5. Python 边界

| 路径 | 边界 |
| --- | --- |
| `app/api/v1` | 对 Java 暴露的内部接口，不做用户登录 |
| `app/graphs/workflows` | 第一阶段只把合同审核图注册为运行入口 |
| `app/graphs/nodes` | `intent_node`、`rag_node`、`analysis_node`、`tool_node`、`human_review_node`、`response_node` |
| `app/tools` | 模型工具的白名单与入参校验 |
| `app/clients/java_backend` | 唯一的业务系统客户端 |
| `app/rag` | 解析、切分、嵌入、pgvector 读写。由 `rag_node` 和入库接口调用 |
| `app/memory` | 长期记忆的写入与读取 |
| `app/persistence` | `agent` schema 连接、迁移、checkpoint |
| `app/models` | OpenAI 兼容聊天与嵌入。供应商待确认 |
| `app/observability` | 结构化日志和运行异常。LangSmith 默认不启用 |

`app/graphs/workflows/payment_review.py` 与 `knowledge_qa.py` 不注册。`contract_query.py` 与 `approval_workflow.py` 若在实现中保留，只能作为合同审核图的内部函数，不能再对 Java 提供第二条入口。

Python 运行时使用 `agent_app`，迁移和 `PostgresSaver.setup()` 使用 `agent_migrator`。checkpoint 建在 `agent` schema。应用代码不手写 checkpoint 表的 DDL，也不在业务查询里连接这些表。

Python 幂等缓存键为 `idem:agent:{tenant_id}:{idempotency_key}`，保留 24 小时。工具调用的最终去重以 `agent.tool_invocation` 的唯一约束为准。

长期记忆第一阶段只有结构化行，没有向量列。是否向量化待确认。

## 6. Java 与 Python 的通信规范

### 6.1 方向

| 调用方 | 被调用方 | 前缀 |
| --- | --- | --- |
| frontend | backend-java | `/api/v1` |
| backend-java | agent-python | `/internal/v1` |
| agent-python | backend-java | `/internal/v1` |

Java 调用 Python 的路径：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/internal/v1/reviews/runs` | 启动审核，响应为 SSE |
| POST | `/internal/v1/reviews/runs/{run_id}/resume` | 审批事务提交之后恢复图 |
| POST | `/internal/v1/knowledge/ingest` | 入库合同或条款库文件 |
| GET | `/internal/v1/reviews/runs/{run_id}` | 查询运行状态 |
| GET | `/internal/v1/health` | 存活检查 |

Python 调用 Java 的路径：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/internal/v1/contracts/{contract_id}` | 读取合同 |
| GET | `/internal/v1/contracts/{contract_id}/files/{file_id}` | 读取文件元数据与对象键 |
| GET | `/internal/v1/contracts` | 按本租户检索合同主数据 |
| PUT | `/internal/v1/review-drafts` | 按 `run_id` 保存草稿并触发待审批 |
| GET | `/internal/v1/approval-tasks/{task_id}` | 读取审批任务 |
| POST | `/internal/v1/agent-callbacks/run-status` | 运行器同步会话状态 |

SSE 事件名：`run_started`、`message_delta`、`message_done`、`waiting_human`、`run_completed`、`run_failed`。只有 `message_done` 的正文写入 `chat_message`。

### 6.2 请求头

内部调用两个方向使用同一组请求头：

| 请求头 | 必填 |
| --- | --- |
| `X-Internal-Token` | 是 |
| `X-Tenant-Id` | 是 |
| `X-User-Id` | 是 |
| `X-Trace-Id` | 是 |
| `X-Idempotency-Key` | 写请求必填 |
| `X-Run-Id` | 运行已创建后必填 |

浏览器请求只使用 `Authorization: Bearer <access_token>`，不发送 `X-Internal-Token`。

`X-User-Id` 在启动审核时是提交人。resume 时仍然传该会话的提交人，因为恢复图是在为这条会话生成说明。审批人的身份记录在 Java 的审批任务上，不靠改写 `X-User-Id` 传入。

### 6.3 错误与重试

错误 JSON：

```json
{"code":"PERMISSION_DENIED","message":"无权读取该合同","trace_id":"..."}
```

允许的 `code`：`UNAUTHORIZED`、`PERMISSION_DENIED`、`TENANT_MISMATCH`、`NOT_FOUND`、`CONFLICT`、`VALIDATION_ERROR`、`IDEMPOTENCY_REPLAY`、`TOOL_FAILED`、`AGENT_FAILED`。

同一幂等键、同一成功结果的重放返回 `IDEMPOTENCY_REPLAY`，并带上第一次的业务标识。校验失败不占用成功结果，调用方修正请求后仍可使用新的幂等键。

Java 调用 Python 之前必须提交本库事务，并且不在该事务的连接上等待 Python。Python 回调 Java 时，Java 打开新事务。读接口可以重试；写接口只带着原幂等键重试。

### 6.4 入库与对象键

对象键：

- 合同：`{tenant_id}/contracts/{contract_id}/{file_id}`
- 条款库：`{tenant_id}/policies/{policy_id}/{file_id}`

桶名 `enterprise-agent`。浏览器把字节流交给 Java，Java 写入 MinIO。Python 使用内部地址读取对象。对象键可以出现在 Java 给 Python 进程的响应里，但不能进入模型可见的工具结果。

`POST /internal/v1/knowledge/ingest` 的请求包含 `source_type`（`contract_file` 或 `policy`）、`source_id`、`object_key`、`content_hash`。Python 不接收任意本地路径。

## 7. Agent 工具调用规范

### 7.1 白名单

| 工具 | Java 接口 | 成功条件 |
| --- | --- | --- |
| `get_contract` | `GET /internal/v1/contracts/{contract_id}` | 用户有 `contract:read`，合同属于当前会话和租户 |
| `get_contract_file` | `GET /internal/v1/contracts/{contract_id}/files/{file_id}` | 同上，且文件是该合同当前要审核的版本 |
| `search_contracts` | `GET /internal/v1/contracts` | 用户有 `contract:read`。租户条件来自请求头，忽略模型传入的其他租户 |
| `save_review_draft` | `PUT /internal/v1/review-drafts` | 用户有 `contract:submit`，`run_id` 属于该会话，`risk_level` 和 `findings` 非空。首次保存时会话为 `active`；同一 `run_id` 重试时会话可以已是 `waiting_human` |
| `get_approval_task` | `GET /internal/v1/approval-tasks/{task_id}` | 任务属于该会话；会话提交人可读取，具备 `approval:decide` 的用户也可读取 |

`save_review_draft` 不接收「是否需要审批」参数。Java 只要保存了有效草稿，就在同一事务里创建或复用该会话唯一的 `pending` 任务，把合同改为 `pending_approval`，并把会话改为 `waiting_human`。同一 `run_id` 再次保存只更新草稿，不把会话改回 `active`。

草稿中的单条原文摘录建议不超过 500 字，待确认。摘录随草稿存在 `business`，这样界面不必访问向量库。证据切片 ID 是逻辑引用。

### 7.2 调用方式

1. 图代码从注册表取出工具。注册表以外的名字不可调用。
2. Pydantic 校验参数。校验失败记为工具失败，不发 HTTP。
3. 客户端附上第 6.2 节的请求头。工具参数里不允许出现租户 ID、令牌或对象键覆盖值。
4. Java 返回业务错误时，工具把 `code` 交还图。`PERMISSION_DENIED` 与 `TENANT_MISMATCH` 终止本次运行。
5. 每次实际发出的调用写入 `tool_invocation`。未发出的参数校验失败也要留下失败记录。

`get_contract_file` 的模型可见结果是抽取后的文本或分页文本，加上文件名和版本号。模型不可见结果才包含对象键，并只存在于进程内存。

检索不走工具白名单。`rag_node` 直接调用 `app/rag`，过滤条件固定为当前 `tenant_id`，合同切片还要加上当前 `source_id`。模型不能提交一段自行拼接的过滤表达式。

### 7.3 人工中断

`human_review_node` 只在 `save_review_draft` 返回 `approval_task_id` 后中断。恢复请求的正文只包含这个任务 ID 和 Java 已经保存的决定摘要。Python 必须再读一次 `get_approval_task`。读到的状态仍是 `pending` 时，不得生成「已通过」或「已驳回」的结束说明，运行记为 `failed`。

resume 失败不影响已经提交的审批事务。Java 将会话标为 `failed` 后可以再次 resume。

### 7.4 执行记录与幂等

一次 HTTP 启动对应一个 `agent_run`。`thread_id` 使用 `review_session.id`，不单独存列。运行幂等键由 Java 生成并写入会话；失败后的用户重试使用新的运行幂等键和新的 `run_id`。同一次 HTTP 超时重试沿用原键，Python 返回已有运行，不新建图。

工具幂等键为 `{run_id}:{tool_name}:{step}`。

运行器在状态变化时调用 `POST /internal/v1/agent-callbacks/run-status`。该回调禁止携带合同状态和审批决定。草稿尚未保存时，回调可以把会话改为 `failed`。审批任务仍是 `pending` 时，回调不能把 `waiting_human` 改成 `active` 或 `failed`。审批已经决定后，resume 成功把会话改为 `completed`；resume 失败把会话改为 `failed`，合同状态保持不变，并允许用同一条已决定任务再次 resume。

## 8. 权限和安全边界

### 8.1 权限码

| 权限 | 谁拥有 | 保护的动作 |
| --- | --- | --- |
| `tenant:manage` | 租户管理员 | 用户与角色 |
| `contract:read` | 管理员、合同经理、审批人 | 读合同、读会话、检索合同 |
| `contract:write` | 管理员、合同经理 | 创建合同、上传文件、在终态前撤回 |
| `contract:submit` | 管理员、合同经理 | 提交审核、保存审核草稿 |
| `approval:decide` | 管理员、审批人 | 通过或驳回 |
| `policy:manage` | 租户管理员 | 维护条款库 |

JWT 只含用户 ID 和租户 ID。权限每次从数据库加载。第一阶段不把权限放进 Redis。

内部接口的检查顺序：

1. 内部令牌错误则 `UNAUTHORIZED`。
2. `X-Tenant-Id` 与用户所属租户不一致则 `TENANT_MISMATCH`。
3. 资源上的 `tenant_id` 与请求头不一致则 `TENANT_MISMATCH`。
4. 缺少权限或资源不属于当前会话则 `PERMISSION_DENIED` 或 `NOT_FOUND`。跨租户资源按 `NOT_FOUND` 返回，避免探测其他租户的 ID。

数据库再做一层 RLS。应用漏掉 `tenant_id` 条件时，数据库也不返回其他租户的行。RLS 使用的租户来自当前事务设置的 `app.tenant_id`，由连接打开时写入，值与 `X-Tenant-Id` 相同。

### 8.2 高风险动作

下面的动作只有 Java 的对浏览器接口可以做，并且必须先通过权限检查：

| 动作 | 权限 | 附加规则 |
| --- | --- | --- |
| 通过或驳回合同 | `approval:decide` | 任务当前是 `pending`；合同当前是 `pending_approval` |
| 撤回合同 | `contract:write` | 仅创建人或租户管理员；终态不可撤回 |
| 删除条款库文件 | `policy:manage` | 第一阶段可以不做物理删除，待确认 |
| 修改用户角色 | `tenant:manage` | Agent 不能调用 |

Agent 不能修改用户、角色、租户，不能删除文件，不能把合同写成 `approved` 或 `rejected`。

审批接口与提交审核接口都写 `audit_log`。工具读操作不写业务审计。`save_review_draft` 写一条 `review.draft_saved`。

### 8.3 秘密与日志

| 秘密 | 存放 |
| --- | --- |
| 用户访问令牌 | 浏览器内存，以及 Java 的签名密钥环境变量 |
| `X-Internal-Token` | 仅 Java 与 Python 的环境变量 |
| MinIO 访问密钥 | 仅 Java 与 Python 的环境变量 |
| 数据库口令 | 仅对应进程的环境变量 |

日志字段限于 `service`、`trace_id`、`tenant_id`、`user_id`、`run_id`、错误码和节点名。不记录令牌、对象存储签名、文件正文和完整提示词。

agent-python 不映射宿主机端口。MinIO 控制台不对公网开放。

## 9. 禁止出现的职责重复

| 禁止 | 唯一归属 |
| --- | --- |
| 第二套登录或第二套角色判断 | Java `security` 与 `identity` |
| 前端或 Python 改合同状态 | Java `contract`、`review`、`approval` |
| 模型工具直接通过或驳回 | 不存在该工具。决定只在 `POST /api/v1/approval-tasks/{id}/decision` |
| Python 连接 `business`，或 Java 连接 `agent` | 各自的数据库角色 |
| 为 checkpoint 再抄一份业务消息表 | 用户可见消息只在 `chat_message`；短期状态只在 checkpoint |
| 把审批结论同步进长期记忆当事实源 | 审批结论只在 `approval_task` 与合同状态 |
| `rag_node` 之外再注册一个可由模型随意改过滤条件的检索工具 | 检索只在 `app/rag` |
| Java 与 Python 各拿一把会话运行锁 | 只有 Java 的 `lock:review-session:{tenant_id}:{session_id}` |
| 用 Redis 保存合同正文、审批结论或会话全文 | Redis 只保留幂等缓存和会话锁 |
| frontend 配置 agent-python 或 MinIO 地址 | 浏览器只认识 Java |
| 第一阶段注册付款图或通用问答图 | 只有合同审核图 |
| 在 Java 中调用模型或拼装审核提示词 | 只在 Python |
| 跨 schema 外键或跨库同步任务 | UUID 逻辑引用，用 `trace_id` 对账 |

两边都可以出现的只有三类无业务裁决的数据：租户 UUID、用户 UUID、运行 UUID。它们是引用，不是各自的一份主数据。用户和租户的主数据只在 `business`。

## 10. 与其他文档的对齐

调用方向、状态枚举、工具白名单、请求头、SSE 事件和对象键与 [系统架构](./system-architecture.md) 第 4、5、6 节一致。这些标识对应的表、唯一约束和 RLS 与 [数据库设计](../database/database-design.md) 一致。
