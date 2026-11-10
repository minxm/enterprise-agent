# Agent 接口契约

## 接口调用关系

| 调用方 | 被调用方 | 接口 | 用途 |
| --- | --- | --- | --- |
| React | Java | `POST /api/v1/agent/runs/stream` | 发起 Agent 任务 |
| Java | Python | `POST /internal/v1/agent/runs/stream` | 转发 Agent 任务 |
| Python | Java | 内部业务 API | 查询合同、调用业务工具 |
| React | Java | `GET /api/v1/agent/runs/{runId}` | 查询任务状态 |

## 核心约束

- React 不直接访问 Python。
- Java 负责用户认证和权限校验。
- Java 将经过校验的用户上下文传递给 Python。
- Python 不直接修改 Java 管理的业务数据库。
- Python 通过 Java 业务接口执行受控操作。

## 安全与授权

1. Python Agent 服务只能通过内部接口访问，不能暴露给普通前端用户。
2. Java 必须校验用户身份、租户权限和业务权限。
3. Python 的业务工具必须经过授权，不能绕过 Java 直接修改合同或付款数据。
4. 具有副作用的工具必须支持幂等处理；付款等高风险操作必须经过独立业务授权和审批。

开发初期可以使用服务间共享密钥保护内部接口，但不能把密钥放在前端。后续生产环境应升级为更完善的服务身份认证机制，例如签名 JWT 或 mTLS。

## 定义请求数据结构

Java 转发给 Python 的请求：
{
  "request_id": "req-20261109-001",
  "conversation_id": "conv-001",
  "workflow_type": "contract_review",
  "idempotency_key": "review-contract-1001-v1",
  "input": {
    "contract_id": "1001",
    "question": "请检查合同中的付款风险"
  }
}

其中：

|字段	|说明 |
| --- | ------- |
|request_id	|请求追踪 ID |
|conversation_id	|会话 ID |
|workflow_type	|工作流类型 |
|idempotency_key	|幂等键 |
|input	|具体业务输入 |

tenant_id、`user_id` 不应由前端随意传入并被直接信任，而应由 Java 根据登录身份和租户权限确定，再通过受信任的内部通信传递。

## 定义 SSE 事件格式

这是今天最重要的设计内容。

Agent 运行过程中，不应该只返回最终答案，还需要让前端知道执行进度。

统一事件类型：

|事件	|说明 |
| --- | ------- |
|run_started	| Agent 任务开始 |
|node_started	| LangGraph 节点开始 |
|node_completed	| 节点执行完成 |
|tool_started	| 工具调用开始 |
|tool_completed	| 工具调用完成 |
|message	| Agent 输出内容 |
|approval_required	| 需要人工审批 |
|run_completed	| 任务完成 |
|run_failed	| 任务失败 |

SSE 示例：
event: run_started
data: {"run_id":"run-001","status":"running"}

event: node_started
data: {"run_id":"run-001","node":"retrieve_knowledge"}

event: message
data: {"run_id":"run-001","content":"正在分析合同付款条款..."}

event: run_completed
data: {"run_id":"run-001","status":"completed"}

建议正式事件统一增加：

- schema_version
- event_id
- run_id
- timestamp
- payload
这样未来 LangGraph 增加新节点时，不需要频繁修改前端的基础通信逻辑。

## 定义异常响应

统一错误码：

|错误码	| 含义 |
| --- | ------- |
|AUTH_UNAUTHORIZED	| 未认证 |
|AUTH_FORBIDDEN	| 无权限 |
|AGENT_INVALID_REQUEST	| 请求参数错误 |
|AGENT_MODEL_ERROR	| 模型调用失败 |
|AGENT_TOOL_ERROR	| 工具调用失败 |
|AGENT_TIMEOUT	| Agent 执行超时 |
|AGENT_INTERNAL_ERROR	| Agent 内部错误 |

需要特别考虑 SSE 已经开始输出后发生异常的情况：此时不能简单地再返回一个普通 HTTP JSON 错误，应发送 run_failed 事件，并包含可追踪的错误信息。

## 定义人工审批交互

当 Agent 判断需要人工审批时，返回：
{
  "run_id": "run-001",
  "approval_id": "approval-001",
  "approval_type": "contract_review",
  "status": "pending",
  "description": "合同风险较高，需要人工确认"
}

此时：

1. LangGraph 暂停执行。
2. Java 创建或管理正式业务审批记录。
3. 前端展示待审批事项。
4. 用户完成审批。
5. Java 校验审批权限并记录结果。
6. Python 根据审批结果恢复或终止工作流。

注意：Agent 的暂停状态由 LangGraph 管理，正式业务审批状态由 Java 管理，两者不能混为一谈。

## 健康检查的当前实现

浏览器不直接访问 Python。Java 增加了一个只读探测接口，用来确认 Python Agent 进程是否可用：

| 调用方 | 被调用方 | 接口 | 用途 |
| --- | --- | --- | --- |
| 浏览器 | Java | `GET /api/v1/integration/agent/health` | 查询 Python 健康状态，响应为统一 `ApiResponse` |
| Java | Python | `GET {agent.service.base-url}/api/v1/health` | 复用 Python 已实现的健康检查。默认地址是 `http://localhost:8000/api/v1/health` |

Python 当前响应：

```json
{
  "status": "ok",
  "service": "agent-runtime"
}
```

Java 成功时返回：

```json
{
  "code": 200,
  "message": "success",
  "data": {
    "status": "ok",
    "service": "agent-runtime"
  }
}
```

Python 不可用时 Java 进程继续运行。连接失败返回 HTTP 503，读超时返回 HTTP 504，Python 返回非 2xx 或响应无法解析时返回 HTTP 502。响应体仍是 `ApiResponse`，不包含 Java 堆栈。

同一次调用使用同一个 `X-Request-ID`。Java 校验请求头：只接受不超过 128 位的字母、数字、连字符和下划线，否则生成新的 ID。Java 把该 ID 传给 Python，并写入自己的响应头和 `ApiResponse.requestId`。Python 沿用这个 ID，写入响应头；健康检查 JSON 额外带 `requestId`，原有 `status` 和 `service` 不变。错误响应同样返回该 ID，不返回堆栈。

地址来自 `agent.service.base-url` 和 `agent.service.health-path`，可用环境变量 `AGENT_SERVICE_BASE_URL`、`AGENT_SERVICE_HEALTH_PATH` 覆盖。

### 与原契约的差异

- 上面的任务接口仍然是 `POST /api/v1/agent/runs/stream` 和 `POST /internal/v1/agent/runs/stream`。本次没有实现任务流。
- Python 已实现并通过测试的健康检查是 `GET /api/v1/health`，不是 `/internal/v1` 前缀。Java 直接调用这条现有路径，没有改 Python。
- 这次健康检查是只读探测，还没有携带服务间共享密钥，也没有引入 Spring Security。后续业务内部接口仍按本文的安全要求补上服务身份认证。