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

## 启动 Java

```powershell
cd D:\enterprise-agent\backend-java
mvn spring-boot:run
```

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
