# Enterprise Agent

企业级智能业务流程 Agent 平台。第一阶段实现企业合同智能审核与审批。

## 服务

三个服务各自管理依赖，不共用 Python 虚拟环境或 Node.js 的 `node_modules`。

| 目录 | 依赖配置 |
| --- | --- |
| frontend | `frontend/package.json` |
| backend-java | `backend-java/pom.xml` |
| agent-python | `agent-python/pyproject.toml` |

业务表在实现对应功能时再由迁移创建。初始化脚本只创建扩展、schema 和角色。

本地密钥放在仓库根目录的 `.env`，由 `.env.example` 复制得到。`.env` 不提交。

三个应用先分别在本机启动。PostgreSQL、Redis 和 MinIO 用 Docker Compose 启动：

```powershell
Copy-Item .env.example .env
# 在 .env 中填写密码，不要把真实密码写入源码

docker compose --env-file .env -f deploy/docker-compose.yml up -d postgres redis minio minio-init
```

Java 使用 `dev` profile 连接 `business` schema 和 Redis。Python 默认不在启动时连接数据库；需要连接池时把 `ENTERPRISE_INFRA_ENABLED` 设为 `true`。变量说明见根目录 `.env.example`。
