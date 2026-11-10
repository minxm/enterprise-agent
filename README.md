# Enterprise Agent

企业级智能业务流程 Agent 平台。第一阶段实现企业合同智能审核与审批。

## 服务

三个服务各自管理依赖，不共用 Python 虚拟环境或 Node.js 的 `node_modules`。

| 目录 | 依赖配置 |
| --- | --- |
| frontend | `frontend/package.json` |
| backend-java | `backend-java/pom.xml` |
| agent-python | `agent-python/pyproject.toml` |

业务包、页面和数据库脚本在实现对应功能时再添加。

本地密钥放在各自目录的 `.env`，由 `.env.example` 复制得到。`.env` 不提交。

三个服务先分别在本机启动。都能独立运行之后，再添加 Docker Compose。
