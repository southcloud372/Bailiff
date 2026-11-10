# ADR-0005：checkpoint 三级后端（memory / sqlite / postgres），本地零依赖、生产可水平扩展

- 状态：已采纳（Accepted）
- 日期：2026-11-10
- 决策者：项目作者

## 背景（Context）

LangGraph 的 checkpoint 决定「图状态存在哪」。它直接决定三件事能不能成立：

1. **HITL 审批**：工单停在 `interrupt()` 等人点同意。人可能几小时后才回来——
   进程早就重启了，内存里的审批上下文必须还在。
2. **多轮对话**：`thread_id` 对应一段会话历史，不能因为换了个副本就「失忆」。
3. **水平扩展**：两个 Pod 各自持有半份状态，LB 把下一轮打到另一台就断片。

原实现统一用 `MemorySaver()`，在单机 demo 能跑，但上面三件事一件都保证不了。
面试官只要问一句「你服务重启后，等待审批的工单还在吗」，就穿了。

候选路线：

- 全量上 `AsyncPostgresSaver`（官方生产推荐）；
- 只上 SQLite；
- **按环境分三档**：memory / sqlite / postgres。

## 决策（Decision）

引入 `build_checkpointer(backend, ...)` 工厂（`agent_platform/core/checkpoint.py`），
三档后端由 `AGENT_CHECKPOINT_BACKEND` 切换，**同一份代码**通吃：

| 档位 | 实现 | 适用 | 代价 |
|---|---|---|---|
| `memory` | `InMemorySaver` | 单测、CI | 重启即丢 |
| `sqlite` | 自研 `SqliteCheckpointSaver`（写穿） | 本地开发、单机部署 | 单机，不能多副本 |
| `postgres` | `AsyncPostgresSaver` + `AsyncConnectionPool` | 生产多副本 | 需要 PG + 可选依赖 |

配套细节：

1. **写穿而非重写协议**：`SqliteCheckpointSaver` 继承官方 `InMemorySaver`，在
   `put` / `put_writes` / `delete_thread` 后把变化落盘，读时按 `thread_id` 惰性 hydrate。
   这样复用官方对 delta channel 的复杂语义，而不是自己实现一套容易写错的增量机制。
2. **单文件单图**：Agent 图与工单图各用一个 sqlite 文件，避免两张图共用 saver 时
   `thread_id` 撞车。
3. **表结构初始化时机**：`compile()` 是同步的，而 `AsyncPostgresSaver.setup()` 是异步的，
   所以 Postgres 分支返回一个 `_PostgresCheckpointer` 门面，由 FastAPI `lifespan`
   里的 startup hook 调 `setup()`（幂等建表）。DDL 失败只记 error，不阻止服务启动——
   `/readyz` 会如实报不就绪。
4. **可选依赖**：`langgraph-checkpoint-postgres` 放在 `[project.optional-dependencies].prod`，
   未安装时抛带修复命令的 `RuntimeError`，而不是裸 `ImportError`。

## 备选方案（Alternatives Considered）

| 方案 | 弃用原因 |
|---|---|
| 只上 Postgres | 违反「clone 下来就能跑」：本地必须先起 Docker 才能对话 |
| 只上 SQLite | 多副本部署直接失效，且 SQLite 在网络文件系统上不可靠 |
| 用 `langgraph-checkpoint-redis` | Redis 适合 TTL/短状态，长期一致性仍建议 PG 为准；且多一个状态真相源 |
| 自己实现完整 `BaseCheckpointSaver` | 增量 channel 语义极易写错，收益不抵风险 |

## 后果（Consequences）

- 正面：本地零依赖可跑；生产切一个环境变量即可水平扩展；HITL 真正闭环。
- 代价：SQLite saver 是「内存一级缓存 + 落盘」模型，多进程同时写同一文件不安全
  （文档已明确「单机」定位）；Postgres 分支多一次 `setup()` 生命周期管理。
- 缓解：`/readyz` 暴露 `checkpoint` 探针；`thread_has_checkpoint` 提供运维排查入口；
  无状态接口 `/v1/ask` 用完即 `delete_thread`，避免 checkpoint 表被一问一答撑爆。

## 参考

- 实现：`agent_platform/core/checkpoint.py`
- 接线：`scripts/serve.py`（`_make_saver`、`_setup_postgres_guarded`）
- 测试：`tests/test_checkpoint.py`（重启后状态仍在、线程隔离、删除幂等）
