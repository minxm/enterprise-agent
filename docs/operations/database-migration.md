# 数据库迁移

正式环境把数据库结构变更从业务进程里拆出来。Java 的 `business` schema 仍由现有 Flyway 脚本管理，Python 的 `agent` schema 仍由现有 `app/persistence/migrate.py`、`agent.schema_migrations` 和原来的 advisory lock 管理。不另建一套迁移目录。

业务进程如果持有 migrator 口令，每个实例都能改表，多实例同时启动也可能同时进入迁移。独立任务只在发布时运行一次，正式应用只使用 `business_app` 或 `agent_app`。

## 环境变量

迁移任务和应用使用不同的口令。不要把真实口令写进示例文件或启动日志。

Java 迁移任务需要：

- `POSTGRES_HOST`
- `POSTGRES_PORT`
- `POSTGRES_DB`
- `BUSINESS_MIGRATOR_PASSWORD`

它固定连接 `business_migrator`，不使用 `business_app`，也不使用 PostgreSQL 超级用户。

Java prod 应用需要 `BUSINESS_APP_PASSWORD`、`JWT_SECRET`、Redis、MinIO、`AGENT_INTERNAL_TOKEN` 和 Agent 地址。它不读取 `BUSINESS_MIGRATOR_PASSWORD`。

Python 迁移任务需要：

- `POSTGRES_HOST`
- `POSTGRES_PORT`
- `POSTGRES_DB`
- `AGENT_MIGRATOR_PASSWORD`

它固定连接 `agent_migrator`。`AGENT_MIGRATOR_USER` 只能是 `agent_migrator`。

Python prod 应用需要 `POSTGRES_HOST`、`POSTGRES_DB`、`AGENT_APP_PASSWORD`、`REDIS_HOST`、`REDIS_PASSWORD`、`AGENT_INTERNAL_TOKEN` 和 `JAVA_BASE_URL`。它不读取 `AGENT_MIGRATOR_PASSWORD`。

开发环境仍保持原来的便利：`dev` profile 的 Java 在启动时执行 Flyway；Python 在 `APP_ENV=dev` 且启用基础设施时，仍在启动时执行 agent 迁移。这两种开发启动都需要对应的 migrator 口令。

## 执行顺序

1. 确认 PostgreSQL 已经可连接，并且现有数据卷还在。
2. 执行 Java business schema 迁移。
3. 执行 Python agent schema 迁移。
4. 确认两个任务的进程退出码都是 0。
5. 再启动 Java prod 和 Python prod。

Java：

```powershell
cd D:\enterprise-agent\backend-java
mvn -DskipTests spring-boot:run "-Dspring-boot.run.profiles=migrate"
```

已经打好的 jar 也可以：

```powershell
java -jar backend-java.jar --spring.profiles.active=migrate
```

Python：

```powershell
cd D:\enterprise-agent\agent-python
.\.venv\Scripts\python.exe -m app.persistence.migrate
```

没有待执行脚本时，任务打印“没有待执行的迁移”并退出 0。重复执行不会再次修改已经记入 `business.flyway_schema_history` 或 `agent.schema_migrations` 的版本。Flyway 使用自己的迁移锁；Python 继续使用 advisory lock `16160016016`。并发的迁移任务会排队，不会各自再执行同一版本。

Python 迁移任务每次都会收回 `agent_app` 对 `agent.schema_migrations` 的写入权限，并保留 SELECT。生产应用用这一个只读权限核对版本。`agent_app` 不能插入、更新、删除或清空版本行，也不能删除 `memory_audit`。

## 迁移历史表的权限边界

四个登录角色都不是超级用户，也没有互相继承。应用进程只使用 `business_app` 或 `agent_app`。迁移进程只使用对应的 migrator。

| 账号 | 身份 | 迁移历史表 | 允许 | 不允许 |
| --- | --- | --- | --- | --- |
| `business_app` | Java 业务连接，`rolsuper = false` | `business.flyway_schema_history` | `SELECT` | `INSERT`、`UPDATE`、`DELETE`、`TRUNCATE`、`ALTER`、`DROP`、`CREATE` |
| `business_migrator` | Java Flyway，表所有者 | 同上 | 读取并执行 Flyway，包括写入历史 | 不作为业务连接 |
| `agent_app` | Python 业务连接，`rolsuper = false` | `agent.schema_migrations` | `SELECT` | `INSERT`、`UPDATE`、`DELETE`、`TRUNCATE`、`ALTER`、`DROP`、`CREATE` |
| `agent_migrator` | Python 迁移，表所有者 | 同上 | 读取并执行迁移脚本，包括写入版本行 | 不作为业务连接 |

`business_app` 对 Flyway 历史表曾经拥有 `SELECT, INSERT, UPDATE, DELETE`。来源是初始化时为 `business_migrator` 设置的默认表权限：它新建的表会把这四项授给 `business_app`。Flyway 创建历史表时走的就是这条路径。角色继承没有把所有者权限带给 `business_app`。`PUBLIC` 也没有这张表的权限。

这次只收回历史表上的写权限。普通业务表继续使用原来的默认 DML，历史 Flyway 脚本不改。权限脚本在 `database/postgres/privileges/business_flyway_history.sql`，由 `business_migrator` 执行。不要把它放进 `db/migration`，也不要用 `business_app` 或超级用户代替。Java 的 `migrate` profile，以及开发环境启动时的 Flyway，在迁移结束后以 `business_migrator` 再执行脚本中的这三条语句：

```sql
REVOKE ALL ON TABLE business.flyway_schema_history FROM business_app;
REVOKE ALL ON TABLE business.flyway_schema_history FROM PUBLIC;
GRANT SELECT ON TABLE business.flyway_schema_history TO business_app;
```

因此正常的 Java 迁移命令结束后，`business_app` 只剩 SELECT。若有人绕开本仓库的迁移任务、直接用 Flyway 在新库建表，需要再用 `business_migrator` 执行上述脚本。已有数据卷不会重跑初始化脚本，默认权限也不会追溯改掉已经存在的表；对当前库执行一次脚本即可，重复执行不会改历史行。

Python 侧不另加脚本。`agent_migrator` 每次迁移都会把 `agent.schema_migrations` 收成 `agent_app` 的 SELECT。

用应用账号自己的连接核对，不使用超级用户：

```sql
SELECT current_user, rolsuper
FROM pg_roles
WHERE rolname = current_user;
```

然后对当前账号检查 `has_table_privilege`。`business_app` 对 `business.flyway_schema_history`、`agent_app` 对 `agent.schema_migrations` 都只应能 SELECT。把 `INSERT`、`UPDATE`、`DELETE` 放进事务里执行，预期 SQLState 是 `42501`，随后回滚。回滚后历史表的版本行数和校验和不变。`CREATE TABLE` 与 `ALTER TABLE` 同样因权限不足失败。Schema Guard 和 `assert_schema_ready` 只执行版本查询，不需要写权限。

## 怎样算成功

- 进程退出码是 0。
- 输出写明没有待执行的迁移，或写明新执行的版本数量。
- `business.flyway_schema_history` 里已有版本仍由 `business_migrator` 记录。
- `agent.schema_migrations` 的已有版本行还在，行数不因重复执行增加。

迁移失败时进程退出码不是 0。Java 失败日志只保留异常类型或去掉口令后的说明，不打印连接串。Python 失败输出会把环境中的口令替换掉。失败的 Python 脚本在自己的事务里回滚，已记录的旧版本不会被改写。连接在结束时关闭。

失败后先看退出码和最后一条错误，修配置或脚本，再重新执行同一个迁移命令。不要改已经成功执行过的历史脚本，也不要删掉历史表来“重来”。

## 应用启动不做迁移

Java prod 不创建 Flyway Bean，也不连接 `business_migrator`，启动不需要 `BUSINESS_MIGRATOR_PASSWORD`。启动时用 `business_app` 读取 `business.flyway_schema_history`，和 `classpath:db/migration` 里的版本对照。日志是“business schema 兼容，未执行迁移”。版本缺失或历史表不可读时，进程失败，消息是“数据库未完成迁移”。

Python prod 不调用 `apply_migrations`。启动时用 `agent_app` 读取 `agent.schema_migrations`。日志是“agent schema 兼容，未执行迁移”。读不到版本或版本不全时，进程失败，消息同样是“数据库未完成迁移”。

这两种检查都不执行 DDL，也不临时换成 migrator。`business_app` 和 `agent_app` 执行 `CREATE TABLE` 会得到权限不足。

确认应用没有改结构时，可以同时看：

- 启动日志没有 Flyway 的 migrate 成功记录，也没有“新执行 N 个版本”。
- 应用连接的 `current_user` 分别是 `business_app` 和 `agent_app`，`rolsuper` 为 false。
- 应用启动前后，`flyway_schema_history` 和 `schema_migrations` 的版本集合不变。

## 回退应用和回退数据库不是同一件事

把应用包退回到旧版本，只改变进程里的代码。已经写入数据库的 migration 还会留在库里。旧代码可能读不懂新列、新约束或已经转换过的数据。

因此不能把“重新部署上一版应用”当成数据库回退。涉及表结构或数据转换的 migration 一旦执行，就要单独准备恢复办法，例如事先备份、可逆的数据修复脚本，或在维护窗口从备份恢复。本仓库不提供自动向下迁移。已经执行过的 `V1`–`V6` 和 `001`–`005` 不要改内容，也不要删除。
