# ADR-0006：运行时后端可插拔 + 按环境分级的「优雅降级」

- 状态：已采纳（Accepted）
- 日期：2026-11-10
- 决策者：项目作者

## 背景（Context）

校招项目最尴尬的处境是：**代码写得越"企业级"，越容易跑不起来**。
一上来要求 Postgres + Redis + Ollama + 模型 key，面试官 clone 下来 30 分钟没跑通，
再好的架构也只剩「听说过」。

但反过来，只做一个能在笔记本跑的版本，又回答不了「多副本怎么办」「限流怎么共享」
「重启会不会丢状态」——这些恰恰是区分「会调 API」和「会做工程」的问题。

同时，某些安全配置在本地和生产应有不同严格度。例如 JWT 用示例密钥：
本地 clone 就该能跑，生产则必须直接拒绝上线。

## 决策（Decision）

### 1. 运行时后端可插拔（`RuntimeSettings`）

把「基础设施形态」从代码里抽成配置，所有后端都有**无需外部服务**的默认实现：

| 能力 | 默认（本地） | 生产 | 切换开关 |
|---|---|---|---|
| 图状态 | SQLite 文件 | PostgreSQL 连接池 | `AGENT_CHECKPOINT_BACKEND` |
| 限流 | 进程内令牌桶 | Redis 滑动窗口（Lua 原子） | `AGENT_RATE_LIMIT_BACKEND` |
| 语义缓存 | 关闭 | 开启（`/v1/ask` 生效） | `AGENT_CACHE_ENABLED` |
| 可观测 | no-op | OTLP → Langfuse | `OTEL_EXPORTER_OTLP_ENDPOINT` |
| 知识库 | 惰性连接（不连不报错） | pgvector + Ollama | `AGENT_VECTOR_DSN` |

关键点：**未配置的能力不阻止启动**，只在被调用时报错，并由 `/readyz` 如实暴露。

### 2. 限流故障时降级而非放行

`RedisRateLimiter` 在 Redis 不可用时切到本地令牌桶（fail-degraded），而不是 fail-open。
放行等于关掉治理；降级至少保住单副本防雪崩，同时打 warning 让运维介入。

### 3. 就绪判定按环境分级（`AGENT_ENV`）

示例 JWT 密钥：
- `dev`：`/readyz` 通过，但探针 detail 写「使用开发示例密钥（仅限本地）」；
- `staging` / `prod`：`/readyz` 直接 503，拒绝上线。

同一份判断逻辑，严格度由环境决定，避免「本地一切正常、上线才发现」。

## 备选方案（Alternatives Considered）

| 方案 | 弃用原因 |
|---|---|
| 强制要求全部基础设施 | clone 门槛过高，校招演示场景不可接受 |
| 全部内存实现，不做生产后端 | 回答不了多副本/一致性/持久化，失去差异化 |
| 用 Docker Compose 一次性拉起全部 | 仍需 Docker；且 CI 里跑真实依赖慢且脆 |
| 熔断/限流故障时 fail-open | 治理失效比短暂不可用更危险（尤其对写操作） |

## 后果（Consequences）

- 正面：`uv sync && serve.py` 三步可跑；切生产只改环境变量；CI 无需外部依赖，20 秒跑完。
- 代价：分支变多，每加一个后端都要有对应测试与探针；「本地能跑 ≠ 生产能跑」仍需
  部署文档兜底（见 `docs/operations.md`）。
- 缓解：默认值集中在一处（`RuntimeSettings`），CI 用 `AGENT_CHECKPOINT_BACKEND=memory`
  跑纯逻辑测试；`tests/test_api_ops.py` 覆盖 `dev` / `prod` 两套就绪语义。

## 参考

- 配置：`agent_platform/gateway/config.py`
- 限流：`agent_platform/gateway/ratelimit.py`
- 就绪：`agent_platform/observability/health.py`、`agent_platform/api/app.py`
- 接线：`scripts/serve.py`
