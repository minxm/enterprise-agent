# 本地开发

三个服务各自使用自己的依赖，不共用虚拟环境。前端只请求 Java，不请求 Python。

## 环境

本机没有 JDK 21。已安装的是 JDK 25.0.4，路径 `D:\Program Files\Java\jdk-25.0.4.1+1`。Java 工程使用 Spring Boot 3.5.16，该版本支持 Java 17 到 25。

Maven 3.9.11 路径：`D:\Programs\maven\apache-maven-3.9.11`。

Python Agent 使用 `agent-python/.venv`，解释器来自 `D:\Programs\miniconda3\python.exe`，版本 3.14.7。

`java` 和 `mvn` 默认不在 PATH 中。启动 Java 前先设置：

```powershell
$env:JAVA_HOME = "D:\Program Files\Java\jdk-25.0.4.1+1"
$env:Path = "$env:JAVA_HOME\bin;D:\Programs\maven\apache-maven-3.9.11\bin;$env:Path"
```

## 启动 PostgreSQL、Redis 和 MinIO

在仓库根目录复制 `.env.example` 为 `.env`，填写密码后再启动。密码只留在本机 `.env`。

```powershell
cd D:\enterprise-agent
docker compose --env-file .env -f deploy/docker-compose.yml up -d postgres redis minio minio-init
docker compose --env-file .env -f deploy/docker-compose.yml ps
```

PostgreSQL 镜像是 `pgvector/pgvector:0.8.7-pg16`，只监听 `127.0.0.1:5432`。首次初始化会创建 `vector` 扩展、`business` 和 `agent` schema，以及 `business_migrator`、`business_app`、`agent_migrator`、`agent_app`。这些角色不是超级用户。Java 的 Flyway 使用 `business_migrator`，业务连接使用 `business_app`。Python 使用 `agent_app`，`search_path` 只有 `agent`。

Redis 镜像是 `redis:7.4.12-alpine`，只监听 `127.0.0.1:6379`，开启 AOF。不要执行 `docker compose down -v`，那会删掉数据卷。

MinIO 服务器版本是 `RELEASE.2025-09-07T16-13-09Z`，客户端是 `RELEASE.2025-08-13T08-35-41Z`。官方仓库已不能匿名拉取，Compose 使用同一摘要的公开副本。API 监听 `127.0.0.1:9000`，控制台监听 `127.0.0.1:9001`。桶名是 `enterprise-agent`，数据在卷 `minio-data`。Java 通过环境变量读取 Root 凭据，不把它们交给前端。

Java 要连上数据库时，先把 `.env` 加载到进程环境，再使用 `dev` profile：

```powershell
Get-Content .env | ForEach-Object {
  if ($_ -match '^\s*#' -or $_ -notmatch '=') { return }
  $pair = $_ -split '=', 2
  Set-Item -Path "Env:$($pair[0].Trim())" -Value $pair[1]
}
mvn spring-boot:run "-Dspring-boot.run.profiles=dev"
```

`dev` profile 启动前，`.env` 里的 `JWT_SECRET` 必须是至少 32 个字符的签名密钥。不要把真实密钥写进源码或提交到 Git。

Python 从 `agent-python/.env` 或仓库根目录 `.env` 读取同名变量。只有 `ENTERPRISE_INFRA_ENABLED=true` 时，进程启动才会打开连接池。

## 启动前端

Node.js 24 可用于当前 Vite 6（要求 Node 18、20 或 22 及以上）。依赖只安装在 `frontend/node_modules`。

```powershell
cd D:\enterprise-agent\frontend
npm install
npm run dev
```

开发服务器是 `http://127.0.0.1:5173`。浏览器请求同源的 `/api` 和 `/actuator`，由 Vite 转发到 `http://127.0.0.1:8080`。不要同时再把 Axios 基地址设成 `http://localhost:8080`，Java 当前没有 CORS。生产构建使用相对路径，由部署网关转发这两个前缀。工作台通过 Java 展示 Python 健康状态，不直接访问 8000 端口。

## 启动 Python

```powershell
cd D:\enterprise-agent\agent-python
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

健康检查：`GET http://127.0.0.1:8000/api/v1/health`

Swagger：`http://127.0.0.1:8000/docs`

## Python 生产启动

生产配置使用现有环境变量，不另建配置文件。在加载仓库根目录 `.env` 后设置：

```powershell
$env:APP_ENV = "prod"
$env:ENTERPRISE_INFRA_ENABLED = "true"
cd D:\enterprise-agent\agent-python
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

不要加 `--reload`。进程只监听 `127.0.0.1`，不直接对公网暴露。`APP_ENV=prod` 时必须启用基础设施，业务库账号是 `agent_app`，迁移账号是 `agent_migrator`，两者不能相同，也不能是 PostgreSQL 超级用户。`AGENT_INTERNAL_TOKEN` 必须与 Java 使用同一个非空值。模型密钥可以暂缺，服务可以启动，但健康响应里的模型状态是 `unavailable`，不会为了检查密钥去调用外部模型。

`/api/v1/health` 表示进程存活，Java 现有健康检查继续读取 `status=ok`。`/api/v1/ready` 表示 PostgreSQL、Redis、Checkpointer 和 MemoryService 是否可用；数据库不可用时返回 503。生产模式关闭 `/docs`、`/redoc` 和 `/openapi.json`。

当前版本不支持安全的多 Worker 横向扩容。`ThreadLocks` 只保护同一个进程里的同一个对话线程。多个 Worker 或多个实例不共享这把锁。PostgreSQL Checkpointer 可以跨进程保存状态，但这不表示并发写入自动安全。Python 的 Redis 连接仍只用于启动和就绪检查。Java 用同一个 Redis 实例共享 Actor Ticket：键是 `actor-ticket:v1:` 加票据的 SHA-256，值只有租户和用户标识，TTL 180 秒。签发是 `SET NX EX`，消费是 `GETDEL`，撤销是 `DEL`。Redis 不可用时这些操作失败关闭，不回退到进程内 Map。现有 `REDIS_HOST`、`REDIS_PORT` 和 `REDIS_PASSWORD` 继续使用，命令超时是 `spring.data.redis.timeout`（2 秒）。单节点 AOF `everysec` 在崩溃或主从切换时可能短暂重放已删除的票据。生产应用启动时不再执行迁移，只核对 `agent.schema_migrations`。开发环境在基础设施启用时仍会于启动时迁移。独立迁移命令和顺序见 [数据库迁移](../operations/database-migration.md)。

## 启动 Java

```powershell
cd D:\enterprise-agent\backend-java
mvn spring-boot:run
```

这是开发启动，会在进程内执行 Flyway。正式 prod 不在启动时迁移，要先运行独立迁移任务。命令和失败处理见 [数据库迁移](../operations/database-migration.md)。

默认端口 8080。Python 地址写在 `src/main/resources/application.yml`：

```yaml
agent:
  service:
    base-url: http://localhost:8000
    health-path: /api/v1/health
```

临时改地址可以在启动前设置环境变量，例如：

```powershell
$env:AGENT_SERVICE_BASE_URL = "http://127.0.0.1:8000"
```

## 验证 Java 调用 Python

先启动 Python，再启动 Java，然后请求：

```powershell
Invoke-RestMethod http://127.0.0.1:8080/api/v1/integration/agent/health
```

Python 正常时，`code` 为 200，`data.status` 为 `ok`，`data.service` 为 `agent-runtime`。

停掉 Python 后再请求同一地址。Java 进程应继续运行，并返回 HTTP 503，消息为「Agent 服务不可用」。

自动化测试不依赖真实 Python：

```powershell
cd D:\enterprise-agent\backend-java
mvn test
```

## 常见连接问题

- 浏览器不要直接访问 8000 端口来完成业务调用。健康检查页面可以在本机打开，业务请求走 Java 的 8080 端口。
- 8000 端口已被占用时，Python 会启动失败。换端口后，同时修改 `AGENT_SERVICE_BASE_URL`。
- Java 返回 503 通常是 Python 没启动，或 `base-url` 写错。
- Java 返回 504 是 Python 在读超时时间内没有返回。默认读超时是 5 秒。
- Java 返回 502 是 Python 返回了非成功状态码，或响应不是约定的 JSON。
