# 技术栈核查报告（核查日 2026-11-10）

对象项目：`D:\E\code\py-agent`（agent-platform，Python 3.13 / uv）。方法：PyPI 实时元数据（`pypi.org/pypi/*/json` 与 shields 版本探针）+ 官方文档页 + 本地 `.venv` 与 `uv.lock` 实测对照。凡本报告给出具体版本号，均为 2026-11-10 当日观测值；不确定项在 §5 单列，不臆造。

## 1. 结论速览

| 组件 | 最新稳定版（观测日） | 项目锁定版 | 状态判断 | 行动 |
|---|---|---|---|---|
| LangGraph | **1.2.20** | 1.2.11 | 1.x 稳定线，推荐 | 升 1.2.x，补 `langgraph-checkpoint-postgres` |
| LangChain | **1.4.9**（core 1.7.7 / openai 1.7.4） | 1.4.0 / 1.6.3 / 1.6.2 | `create_agent` 是唯一推荐入口 | 同步升 core/openai |
| LiteLLM | **1.108.4** | 1.101.0 | 仍在高频发版 | 换 `Router` + `completion_cost` |
| MCP Python SDK | **2.3.0**（1.x 线 1.30.0 仍在发） | 2.2.0 | v2 稳定，支持 2026-07-28 | 约束改 `>=2.3,<3` |
| FastMCP（独立项目） | **4.1.7** | 未用 | 非官方 SDK，v4 依赖 mcp v2 类型 | 不必引入 |
| A2A | SDK **1.3.0**，规范 v1.0 | 未引入 | 协议已 1.0 / LF 治理 | 讲解优先，接入降级 |
| OTel semconv（GenAI） | spec **1.45.0**，`gen_ai.*` 仍 `Development` | 1.44.0（api/sdk 1.45.2） | 事实标准但未 Stable | 按 `gen_ai.*` 打点 |
| Langfuse | SDK **5.2.0**，Server 自托管 v4.x（v4.73.0） | 未引入 | v4 起 OTLP 统一摄入 | 单容器 compose + OTLP |
| DeepEval | **4.3.3** | 未引入 | pytest 风格，CI 友好 | 做 CI 断言层 |
| RAGAS | **0.4.3** | 未引入 | 仍在 0.x，API 变动风险 | 仅 RAG 指标可选 |
| pgvector（Python） | **0.5.1**，扩展 **0.8.4**（0.9.0 未发布） | 0.5.0 | HNSW 为首选索引 | 补索引 DDL 与连接池 |
| PostgreSQL | **18.6**（19 RC1，GA 计划 2026-11-19） | 未锁 | 18 为生产主线 | 选 18 |
| FastAPI / Pydantic | **0.162.2 / 2.14.3** | 0.141.1 / 2.13.5 | 无破坏性迁移 | 抬升约束 |
| uvicorn / sse-starlette | **0.54.0 / 3.7.0** | 0.53.0 / 3.4.11 | sse-starlette 已跨大版本 3 | 抬升约束 |
| Redis | redis-py **8.1.0**，RedisVL 0.31.0 | 未引入 | Agent 记忆/限流/语义缓存齐备 | 按需引入 |
| uv / ruff / mypy | **0.13.7 / 0.17.4 / 2.4.1** | — / 0.16.7 / 2.3.1 | uv PEP 735 已成熟 | 统一 target py313 |
| Python | 3.13.18 / 3.14.10 / 3.15.2 | >=3.12 | 3.13 已转安全维护 | 3.13 可用，3.14 更优 |

## 2. 逐项核查

### 2.1 LangGraph

- **版本与状态**：1.2.20（`requires_dist`：`langchain-core>=1.4.7`、`langgraph-checkpoint>=4.3`、`langgraph-prebuilt 1.1.x`、`langgraph-sdk 0.4.x`）。1.x 是"稳定性发布"：图原语（state/node/edge）与执行模型不变，升级成本低（[What's new in LangGraph v1](https://docs.langchain.com/oss/python/releases/langgraph-v1)）。
- **checkpoint 后端**：官方口径明确——`InMemorySaver` 只用于调试/测试；生产装 `langgraph-checkpoint-postgres`（**3.2.0**）用 `PostgresSaver` / `AsyncPostgresSaver`，并配 `AsyncPostgresStore` 做长期记忆（[Checkpoint Savers 参考](https://reference.langchain.com/python/langgraph/checkpoints)）。SQLite 仅单体 CLI/桌面；Redis 侧有 `langgraph-checkpoint-redis` **0.6.0** 与 Redis 官方 langgraph-redis 项目，属生态方案而非首选。分环境矩阵：dev→InMemory、CI→SQLite(:memory:)、staging/prod→AsyncPostgresSaver。
- **HITL/中断**：`interrupt()` + `Command(resume=...)` 仍是核心 API，未变；官方新增的是上层封装：LangChain v1 的 HITL middleware 与 Agent Server 的 resume 协议（前端发 `command: {resume: {decisions: [{type: "approve"|"edit"|"reject"}]}}`，[Human-in-the-Loop](https://docs.langchain.com/oss/python/langchain/frontend/human-in-the-loop)）。
- **`create_react_agent` 现状**：本地 `langgraph-prebuilt 1.1.0` 仍导出 `create_react_agent`（实测 `langgraph/prebuilt/__init__.py`），但它是**兼容保留**，v1 迁移文档要求改用 `langchain.agents.create_agent`。新代码不应再引入。
- **Platform/Server 定位**：LangGraph Platform 已于 2025-10 更名 **LangSmith Deployment**，其服务器叫 **Agent Server**；它是托管/许可制产品（自托管需 Postgres + Redis + license），不是开源组件，可部署任意框架的 agent（[LangSmith Deployment](https://docs.langchain.com/langsmith/deployment)、[Configure checkpointer backend](https://docs.langchain.com/langsmith/configure-checkpointer)）。对校招项目：**别把主链路依赖它**，用 OSS LangGraph + 自己的 FastAPI 服务即可，有部署需求再讲"可平移"。

### 2.2 LangChain

- **版本**：`langchain` **1.4.9**、`langchain-core` **1.7.7**、`langchain-openai` **1.7.4**。三件套必须同步升（core 是硬约束）。
- **推荐入口**：`from langchain.agents import create_agent` 是 v1 唯一推荐入口，`create_react_agent` 迁到旧路径。实测 1.4.0 的签名已很完整：`create_agent(model, tools, system_prompt, middleware, response_format, state_schema, context_schema, checkpointer, store, interrupt_before, interrupt_after, cache, transformers, name)`。
- **关键 API 变化**：① middleware 成为一等机制（工具错误处理、动态模型、HITL、摘要压缩）；② 结构化输出在主循环内生成，不再单独节点，并提供 `ToolStrategy`（伪工具调用）与 `ProviderStrategy`（provider 原生）两种策略，`from langchain.agents.structured_output import ToolStrategy, ProviderStrategy`；③ 文档建议传**未绑定工具**的模型 + 独立 tools 列表（pre-bound 会与结构化输出冲突）（[LangChain v1 迁移指南](https://docs.langchain.com/oss/python/migrate/langchain-v1)）。
- **2026 H2 用法**：`create_agent` 做单 agent 闭环，需要确定性流程时降到 LangGraph `StateGraph`；用 middleware 而不是继承/复写执行器。

### 2.3 LiteLLM

- **版本**：**1.108.4**（1.100.0 起有 Access Group Budgets、Custom Router Tiers 等，[v1.100.0 release notes](https://docs.litellm.ai/release_notes/v1.100.0/v1-100-0)）。
- **能力现状**：SDK 与 proxy 双形态；`Router(model_list=..., fallbacks=[...], num_retries=...)` 表达降级；`litellm.completion_cost()` 走内置价格表；缓存既有精确缓存（`litellm.cache = Cache(type="redis")`）也有 proxy 侧 semantic cache；虚拟 key/预算/团队花费追踪在 proxy（[docs.litellm.ai](https://docs.litellm.ai/)）。
- **项目现状与差距**：`router/llm.py` 手写 `for` 循环做 fallback，`router/cost.py` 自建 `estimate_cost` 价格表 → 等价能力已在 LiteLLM 内，自建部分会成为"价格表过期"风险点（DeepSeek 2026 年已两次调价）。建议换 `Router` + `completion_cost`，只保留"业务侧预算/配额"这层自研。

### 2.4 MCP Python SDK

- **版本**：`mcp` **2.3.0**（v2 稳定版，`pip install mcp` 即 2.x）；1.x 线仍在维护（1.30.0，2026-09-07），用于兼容 `mcp.server.fastmcp.FastMCP` 老写法（[MCP Python SDK v2](https://newreleases.io/project/github/modelcontextprotocol/python-sdk/release/v2.0.0)、[py.sdk.modelcontextprotocol.io](https://py.sdk.modelcontextprotocol.io/)）。
- **关键 API 变化**：v2 入口是 `from mcp.server import MCPServer`（v1 的 `FastMCP` 退为兼容层），协议类型拆到独立 `mcp_types` 包（`mcp.types` 仍 re-export）；底层 HTTP 依赖迁到 `httpx2`（这会影响 `respx`/`pytest-httpx` 之类的打桩，需要注入 mock transport）；`Client("http://host/mcp")` 即 Streamable HTTP。
- **协议版本**：最新稳定修订 **2026-07-28**，取消了 initialize 握手与会话（每个 POST 自带 protocol version/client info/capabilities，走 `params._meta`）、新增 `server/discover`、`subscriptions/listen`、多轮往返请求；Streamable HTTP 请求体 >4 MiB 直接 413。同一 Server 同时服务 2025-06-18 / 2025-11-25 / 2026-07-28（[规范 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)）。→ 结论：`2025-06-18` 是"仍在服务的旧修订"，`2026-07-28` 是**当前/下一个**要写的版本，不是未来传闻。
- **fastmcp 关系**：`fastmcp`（jlowin 个人项目，**4.1.7**）不是官方 SDK，而是更高层框架；v4 要求 mcp v2 类型，且有时代门控——`ctx.elicit()` 在 2026-07-28 连接上直接抛错（新协议没有 server→client 反向通道）（[Upgrading from FastMCP 3](https://gofastmcp.com/getting-started/upgrading/from-fastmcp-3)）。校招项目用官方 `mcp` 即可，不必叠 fastmcp。

### 2.5 A2A 协议

- **版本/治理**：规范 2026-07 到 **v1.0**（生产可用），由 Linux Foundation TSC 治理（AWS、Cisco、Google、IBM Research、Microsoft、Salesforce、SAP、ServiceNow）；Python SDK **a2a-sdk 1.3.0**，v1.0 主要是 Python DX 重做（AgentCard/任务生命周期/JSON-RPC+REST+SSE 三形态）（[A2A v1.0 公告](https://discuss.google.dev/t/what-s-new-in-a2a-v1-0-a-python-dx-glow-up-and-a-fresh-new-look/381896)）。
- **是否值得校招项目接入**：**中低优先级**。理由是"买方"不足：跨组织 agent 互联的真实需求集中在企业集成场景，个人项目很难演示价值；而项目内部多 agent 协作，LangGraph 子图/handoff 更直接、更可控。建议做法：把 A2A 作为**一页设计**讲清 AgentCard、task 状态机、与 MCP 的分工（MCP=agent↔工具，A2A=agent↔agent），真接入可选做一个 echo/ping agent 打通 a2a-sdk。

### 2.6 OpenTelemetry GenAI 语义约定

- **状态**：语义约定规范发布到 **1.45.0**，但 `gen_ai.*` 命名空间仍在 **Development**（稳定阶梯 `Experimental → Development → Stable`，尚未 Stable）；Python 侧 `opentelemetry-semantic-conventions` 是 **0.66b2**（beta 版本号），`opentelemetry-api/sdk` 到 **1.45.2**（[GenAI semconv](https://opentelemetry.io/docs/specs/semconv/gen-ai/)、[gen-ai 属性注册表](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)）。
- **标准属性（落地要点）**：span 用 `gen_ai.operation.name`（chat/embeddings/…）、`gen_ai.provider.name`（注意：早期文档里的 `gen_ai.system` 与新属性并存过渡，工具链两边都要兼容）、`gen_ai.request.model`、`gen_ai.usage.input_tokens` / `output_tokens`；指标 `gen_ai.client.token.usage`；评测结果有 `gen_ai.evaluation.result` 日志记录；另有独立的 `mcp.*` 属性命名空间（[实现参考](https://pkg.go.dev/github.com/docker/docker-agent/pkg/telemetry/genai)）。
- **OpenLLMetry / Traceloop**：Traceloop 于 2026-03 被 ServiceNow 收购，OpenLLMetry 仍以 Apache-2.0 开源维护（`traceloop-sdk` **0.63.0**）（[Traceloop is joining ServiceNow](https://traceloop.com/blog/traceloop-is-joining-servicenow)）。**但官方 contrib 已有 `opentelemetry-instrumentation-langchain` 0.63.0**，项目不必引 OpenLLMetry 全家桶——自研 `TracingCallback` 只保留补 `gen_ai.*` 语义的部分即可。

### 2.7 Langfuse

- **版本**：Python SDK **5.2.0**（v4→v5 为渐进升级：score 改批量 `POST /api/public/scores`，metadata 值在 span 上做 JSON 编码）；自托管 Server 线是 **v4.x**（社区模板 v4.73.0，2026-07）。
- **自托管形态**：docker compose 六件套——`langfuse-web` + `langfuse-worker` + Postgres + ClickHouse + Redis + S3（MinIO/SeaweedFS），不是"一个容器跑起来"。校招项目建议只自托管最小栈或直接用 Cloud 免费层。
- **OTLP 接入**：v4 起统一走 OTLP，endpoint `/api/public/otel`，Basic Auth 用 `public:secret` 的 base64；旧 `/api/public/ingestion` 自定义事件需迁移到 OTel span（[OTEL 集成](https://langfuse.com/integrations/native/opentelemetry)、[迁移到 v4](https://github.com/langfuse/langfuse-docs/blob/main/content/integrations/native/opentelemetry/migration-to-v4.mdx)）。
- **两个 Python 入口**：`langfuse`（`@observe`、`start_as_current_span`、`create_score`）与 `langfuse.langchain`（LangChain CallbackHandler，v5 仍在维护）。项目里 OTel 与 Langfuse 二选一时：**先 OTel 打点，再把 OTLP 指向 Langfuse**，避免两套埋点。

### 2.8 DeepEval 与 RAGAS

- **版本**：DeepEval **4.3.3**；RAGAS **0.4.3**（仍是 0.x）。
- **API 现状**：DeepEval 走 pytest 风格（`LLMTestCase` + `assert_test` + `EvaluationDataset`），天然适合 CI 阈值门禁；RAGAS 走 `evaluate(dataset, metrics=[...])`，指标生态偏 RAG，0.x 期间 API 变动频繁，**不建议作为平台级强依赖**。
- **替代/补充**：promptfoo（YAML 声明式、模型/提示对比与红队，适合 CI 侧 A/B）、OpenAI Evals（开放注册表式）、Langfuse Datasets+Experiments（trace 级回归）、Arize Phoenix（本地 trace 分析）（[评测工具全景](https://github.com/hernanda-git/ai-knowledge-library/blob/main/69-AI-Evaluation-and-LLM-Testing/04-Tools-and-Frameworks.md)）。
- **推荐组合**：DeepEval 做"每次 PR 的断言层"；Langfuse 做"数据集 + trace 回归 + 人工标注"；promptfoo 只在需要对比多家模型时引入。

### 2.9 pgvector 与 PostgreSQL

- **版本**：扩展 **0.8.4**（2026-06-30）；**0.9.0 未发布**，CHANGELOG 头部即 "0.9.0 (unreleased)"，内容含 HNSW 异步 I/O、sparsevec 插入优化、`vector_dims`、并**弃用 PG 13/14**。Python 绑定 `pgvector` **0.5.1**（[pgvector CHANGELOG](https://github.com/pgvector/pgvector/blob/master/CHANGELOG.md)）。
- **HNSW 现状**：仍是默认首选索引（`USING hnsw (embedding vector_cosine_ops)`，起步 `m=16, ef_construction=64`，查询侧 `SET hnsw.ef_search`）。0.8.x 的关键能力是 **iterative index scans**——带 `WHERE` 过滤时不再"先取 k 再过滤"导致召回塌陷；另有 `halfvec`/`bit`/`sparsevec` 降内存。
- **项目差距**：只有 `pgvector` Python 包与 `psycopg`，缺 `psycopg_pool`、SQLAlchemy/迁移工具、HNSW 索引 DDL 与 `ef_search` 调优脚本；`knowledge/store.py` 需要显式 DDL。
- **PostgreSQL**：**18.6** 是当前生产主线（2026-08-13 更新）；**19 RC1 于 2026-10-15 发布，GA 计划 2026-11-19**（[PostgreSQL 19 RC1](https://www.postgresql.org/about/news/postgresql-19-rc-1-released-3409/)）。结论：现在选 18，11 月下旬后再评估 19（向量负载对 19 的并行/planner 改进敏感）。

### 2.10 FastAPI / Pydantic / uvicorn / sse-starlette

- **版本**：FastAPI **0.162.2**、Pydantic **2.14.3**、pydantic-settings **2.16.2**、uvicorn **0.54.0**、sse-starlette **3.7.0**。
- **要点**：① 对项目无破坏性迁移需求（项目已在 Pydantic v2 + FastAPI 0.1xx 线）；② sse-starlette 已跨到 **3.x 主版本**（2.x→3.x），入口仍是 `EventSourceResponse` + async generator，注意按 3.x 文档核对 `ServerSentEvent` 字段名；③ `uvicorn[standard]`（uvloop/httptools）继续是生产默认；④ checkpoint 连接池应放 `lifespan` 初始化/关闭，而不是模块级单例。

### 2.11 Redis 在 Agent 场景的用法

- **版本**：redis-py **8.1.0**（Redis 8.x server 自带 vector set）；RedisVL **0.31.0**；LangGraph 生态 checkpoint 适配器 `langgraph-checkpoint-redis` **0.6.0**。
- **四类落点**：① **checkpoint/短期状态**（Redis 官方 langgraph-redis 项目，或 checkpoint-redis 适配器；但生产一致性仍建议 Postgres 为准）；② **限流/配额**（项目 `gateway/ratelimit.py` 目前是进程内令牌桶，多副本即失效 → 换 Redis + Lua 原子滑动窗口）；③ **语义缓存**（RedisVL `SemanticCache`，命中阈值 + 索引复用，直接砍 LLM 成本）；④ **长期记忆**（Redis Agent Memory Server：REST+MCP，working/long-term memory，带 LiteLLM provider 支持）（[Redis for AI](https://redis.io/docs/latest/develop/ai/)、[Redis AI incubator](https://redis.io/ai-incubator/)）。
- **建议**：先引入"限流 + 幂等 + 语义缓存"，checkpoint 维持 Postgres；Redis 作旁路，不做状态真相源。

### 2.12 uv / ruff / mypy（2026 推荐配置）

- **uv 0.13.7**：PEP 735 `[dependency-groups]` 已是标准做法（项目已在用 `dev`），配套 `uv sync --group dev` / `uv run --group dev pytest`；`[tool.uv] default-groups = ["dev"]` 让 `uv sync` 默认装开发组；workspace 用 `[tool.uv.workspace] members` + `[tool.uv.sources]` 做内部包依赖；CI 用 `uv lock --check` + `uv sync --frozen`；Python 版本用 `.python-version` 固定（3.13 或 3.14）。0.13.x 的持续改进集中在**大 workspace 的项目发现缓存与 resolver 内存**（[uv CHANGELOG](https://github.com/astral-sh/uv/blob/main/CHANGELOG.md)）。
- **ruff 0.17.4**：`[tool.ruff.lint]` 已是唯一配置位置（顶层 `select` 早废弃）；建议 `target-version = "py313"`，规则集在现有 `E,F,I,UP,B` 上加 `ASYNC,S,RUF,PERF`（Agent 项目异步与安全违规高发）。
- **mypy 2.4.1**：已进入 2.x 大版本，`strict = true` 保留；补 `plugins = ["pydantic.mypy"]`；Python 目标同步 3.13。ruff 管风格/lint，mypy 管类型，不要用 mypy 跑格式。

### 2.13 结构化输出 / function calling 与主力模型

**DeepSeek（本项目主模型）**

| 项 | 事实（2026-11-10） |
|---|---|
| 模型名 | `deepseek-flash` = DeepSeek-V4.1-Flash-1017；`deepseek-v4-pro` = DeepSeek-V4-Pro-0813 |
| 上下文/输出 | 1M / 最大 384K |
| 价格（元/百万 token，闲时/峰时） | Flash：命中 0.02/0.04，未命中 1/2，输出 4/8；Pro：命中 0.15/0.30，未命中 4.5/9，输出 13.5/27 |
| 功能 | Tool Calls ✅、JSON Output（`response_format={'type':'json_object'}`）✅、Responses API ✅、Anthropic 兼容 ✅、思考模式默认开启（`thinking:{type}` + `reasoning_effort` low/high/max） |
| 结构化输出 | **只有 json_object，没有官方 json_schema**；官方要求 prompt 里出现 "json" 并给样例，且非 JSON 内容会被替换为空格 |
| 已停用 | `deepseek-chat` / `deepseek-reasoner`：官方公告 2026-04-24 起"三个月后（2026-07-24）停止使用"，当前"模型&价格"页已不再列出两者；`deepseek-v4-flash` 已下线但仍被路由到 V4.1-Flash |

来源：[模型&价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing)、[更新日志](https://api-docs.deepseek.com/zh-cn/updates)、[JSON Output](https://api-docs.deepseek.com/zh-cn/guides/json_mode)。

**其他厂商主力与价格档（第三方聚合页，仅作量级参考）**

| 厂商 | 主力模型 | 价格档（USD/1M in / out） | 结构化输出 |
|---|---|---|---|
| OpenAI | GPT-5.5（2026-04），官方价目已列 GPT-5.6 | $5.00 / $30.00（cached in $0.50），1.1M ctx | 原生 json_schema / strict function calling |
| Anthropic | Claude Opus 5.5 / Sonnet 5.5（Opus 5 为 $5/$25，cache hit 为输入的 5%，fast mode $10/$50） | 中高 | 原生 tool-use 强约束 |
| Google | Gemini 4 Argon（$2/$10，2026-12-01 起 $4/$20）、gemini-3.1-pro（$2/$12）、gemini-3.8-flash（$0.75/$3.75） | 低-中 | 原生 responseSchema |
| 阿里云 | qwen3.5-max / qwen3.5-plus，qwen3-max $1.20/$6（252K） | 低 | 原生结构化输出 |

来源：[OpenAI 价目](https://help.openai.com/en/articles/20001415)、[Claude Pricing](https://platform.claude.com/docs/en/about-claude/pricing)、[Gemini 价格汇总](https://www.morphllm.com/gemini-api-pricing)、[Qwen 价格](https://qwen-ai.com/pricing/)。

**结论**：本项目应把"结构化输出"抽象成策略——DeepSeek 上走 LangChain v1 的 `ToolStrategy`（伪工具调用 + Pydantic 校验 + 失败重试）或 `json_object` + 校验兜底；只有切到 OpenAI/Gemini/Claude 时才启用 `ProviderStrategy`。这正好是 v1 提供的现成分层，写进设计文档是加分项。

### 2.14 代码沙箱

| 方案 | 隔离级 | 2026 现状 | 校招可行性 |
|---|---|---|---|
| **E2B**（SDK **2.63.0**） | Firecracker microVM（独立内核） | SDK 最成熟（Py/TS/Java），有模板/快照/暂停恢复 | ★★★ 最省事，免费额度够 demo |
| Modal | 托管容器（含 GPU） | 适合"带模型推理的执行" | ★★ 需 GPU 场景才有优势 |
| Daytona / CubeSandbox | 自托管 KVM microVM，E2B 兼容 API | 高密度、需自己维护宿主内核 | ★ 运维成本高 |
| K8s SIG Agent Sandbox + gVisor/Kata | 用户态内核/轻量 VM | 生产多租户首选路径 | ☆ 太重，只作"知道" |
| Docker + seccomp + 非 root + 资源/网络限制 | 命名空间级（弱于 VM） | 通用兜底 | ★★★ 本地可跑，讲清威胁模型 |
| Pyodide / WASM（wasmsh 等） | 浏览器 Worker 内 | 已有"shell+Python+FS 全在浏览器"的 agent 沙箱 | ★★ 演示效果极佳 |

现状判断：**企业级默认是"托管 microVM"（E2B 类）或"K8s + gVisor/Kata"两条路**，自研 Firecracker 只在超大厂（[沙箱方案选型](https://github.com/runzhliu/aik8s/blob/main/docs/ai-k8s/rag-agent/agent-sandbox-selection.md)、[Provider 全景](https://github.com/yaalalabs/agent-kernel/blob/main/docs/specs/494-sandbox-capability/research/provider-landscape.md)）。项目当前只有**进程内路径沙箱**（`resolve_within_root`），没有执行隔离——这在面试里是"会被追问"的点。建议：定义 `Sandbox` 抽象（`run(code) -> stdout/stderr/exit`），实现 `DockerSandbox`（本地）+ `E2BSandbox`（远端）两个适配器，默认 Docker；这比接一个 SDK 更能体现设计能力。

### 2.15 Agent 评测的新范式

- **单步通过率陷阱**：单步 90% × 5 步 ≈ 59%，10 步 ≈ 35%——这就是"demo 能跑、上线就废"的数学解释。
- **pass@k vs pass^k**：pass@k 衡量"k 次里至少一次成功"（能力上限），pass^k 衡量"k 次全成功"（可靠性）。Agent 上线看 **pass^k**，模型选型看 pass@k（[Agent 评测三层次](https://bbs.huaweicloud.com/blogs/509316)）。
- **trace-based / trajectory eval**：三层打分——outcome scorer（沙箱内断言最终状态）、trajectory scorer（顺序错、跳步、重复、越权、步数/token 预算）、LLM judge（用**不同于被测模型**的 grader，附 rubric）（[Task Suites, Trajectory Grading](https://safeguard.sh/resources/blog/ai-agent-evaluation-explained-task-suites-trajectory-grading-llm-judges-and-safety-tests)）。
- **simulation**：用户模拟器 + 环境模拟跑长尾（τ-bench 式），覆盖"真实用户不按剧本"的分布。
- **online eval**：线上按比例采样 trace → judge 打分 → 指标漂移告警（项目已有 `evaluation/drift.py`，可升级为 PSI/KS + 人工标注回流闭环）。

## 3. `pyproject.toml` 诊断

### 3.1 依赖对照（当前约束 → 锁定 → 最新 → 建议）

| 依赖 | 当前约束 | 锁定 | 最新 | 建议 | 备注 |
|---|---|---|---|---|---|
| langgraph | `>=1.0` | 1.2.11 | 1.2.20 | `>=1.2,<2` | 版本线没错，仅抬最小版本 |
| langchain | `>=1.4.0` | 1.4.0 | 1.4.9 | `>=1.4.9,<2` | 与 core 同步 |
| langchain-core | 未直写 | 1.6.3 | 1.7.7 | `>=1.7,<2` | 建议显式声明（LangGraph 硬依赖） |
| langchain-openai | `>=0.3` | 1.6.2 | 1.7.4 | `>=1.7,<2` | **跨大版错误**：0.3 是 2024 年线，v1 才是当前 |
| litellm | `>=1.50` | 1.101.0 | 1.108.4 | `>=1.108,<2` | 约束过松，价格表/路由都在小版本里变 |
| mcp | `>=1.0` | 2.2.0 | 2.3.0 | `>=2.3,<3` | **约束与代码不符**：代码 `import mcp.server.mcpserver` |
| fastapi | `>=0.115` | 0.141.1 | 0.162.2 | `>=0.162,<1` | 无破坏性变更 |
| pydantic | `>=2.8` | 2.13.5 | 2.14.3 | `>=2.14,<3` | — |
| pydantic-settings | `>=2.4` | 2.15.0 | 2.16.2 | `>=2.16,<3` | — |
| uvicorn[standard] | `>=0.30` | 0.53.0 | 0.54.0 | `>=0.54,<1` | — |
| sse-starlette | `>=2.0` | 3.4.11 | 3.7.0 | `>=3.7,<4` | 已跨 3.x 大版本，约束仍写 2.0 |
| psycopg[binary] | `>=3.1` | 3.3.5 | 3.3.8 | `>=3.3,<4` | 建议补 `psycopg_pool` |
| pgvector | `>=0.3` | 0.5.0 | 0.5.1 | `>=0.5,<1` | Python 包≠扩展版本（扩展 0.8.4） |
| opentelemetry-api/sdk、otlp-http | `>=1.25` | 1.44.0 | 1.45.2 | `>=1.45,<2` | 缺 instrumentation 包 |
| httpx | `>=0.27` | 0.28.1 | 0.28.x | `>=0.28,<1` | mcp v2 用 httpx2，测试打桩方式要改 |
| pytest（dev） | `>=8.0` | 9.1.1 | 9.2.0 | `>=9.2,<10` | 已跨 9 大版本，约束还写 8 |
| pytest-asyncio（dev） | `>=0.24` | — | 1.4.1 | `>=1.4,<2` | **0.x→1.x 大版本**，asyncio_mode 配置要核 |
| ruff（dev） | `>=0.6` | 0.16.7 | 0.17.4 | `>=0.17,<1` | — |
| mypy（dev） | `>=1.11` | 2.3.1 | 2.4.1 | `>=2.4,<3` | **1.x→2.x 大版本** |
| requires-python | `>=3.12` | — | 3.13.18 / 3.14.10 | `>=3.13` | 3.13 已于 2026-10-01 转安全维护 |

### 3.2 P0（不改会出问题）

1. **`mcp>=1.0` 与代码冲突**：`agent_platform/tools/filesystem.py` 用 `from mcp.server.mcpserver import MCPServer`（v2 API），1.x 里没有该模块。当前靠"无上界 + uv 取最高"侥幸装到 2.x，一旦锁文件回滚或加别的约束就会 import 失败。→ `mcp>=2.3,<3`。
2. **生产 checkpointer 缺失**：`core/agent.py:245` 与 `core/workflow.py:223` 都用 `MemorySaver()`；`interrupt()` 恢复依赖的 checkpoint 落盘只在进程内，重启即丢审批上下文。→ 加 `langgraph-checkpoint-postgres>=3.2`，改 `AsyncPostgresSaver`，`InMemorySaver` 只留给测试。
3. **默认模型名已死**：`gateway/config.py:33` `model = "deepseek-chat"`，`.env.example:4` 同值，`scripts/smoke_deepseek.py` 同名兜底。官方公告 `deepseek-chat` / `deepseek-reasoner` 自 **2026-07-24 起停止使用**，且当前"模型&价格"页只列 `deepseek-flash` / `deepseek-v4-pro` → 默认改 `deepseek-flash`，Pro 场景 `deepseek-v4-pro`，同步改测试与文档（`agent求职/*.md`、ADR-0002 里的示例）。
4. **`langchain-openai>=0.3`** 属跨大版陈旧约束（当前 1.7.4），会误导 reader/面试官，也可能是 resolver 未来降级的入口。

### 3.3 P1（技术债/一致性）

- `litellm>=1.50` 过松：自建 `estimate_cost` 的价格表随 LiteLLM 升级才能跟上 DeepSeek 两次调价（2026-08 峰谷价、2026-09 降价）。建议换 `litellm.completion_cost()` + `Router(fallbacks=...)`。
- 缺可观测依赖：`opentelemetry-instrumentation-langchain`（0.63.0）、`-fastapi`、`-httpx`；缺 `langfuse`（5.2.0）。
- 缺评测依赖：`deepeval`（4.3.3）；RAGAS（0.4.3）只在需要 RAG 指标时引入。
- 缺 Redis 相关：`redis>=8.1`（+ 可选 `redisvl`、`langgraph-checkpoint-redis`）——当前限流是进程内令牌桶，多副本部署失效。
- 缺沙箱依赖：`e2b`（2.63.0，可选 extra）。
- 配置漂移：`[tool.ruff] target-version = "py312"`、`[tool.mypy] python_version = "3.12"` 应改为 `py313` / `"3.13"`；mypy 建议加 `plugins = ["pydantic.mypy"]`。
- 缺 `[tool.pytest.ini_options]`（`asyncio_mode = "auto"` 在 pytest-asyncio 1.x 下需显式声明）。

### 3.4 建议的 pyproject 片段（可直接替换依赖段）

```toml
requires-python = ">=3.13"

dependencies = [
  "langgraph>=1.2,<2",
  "langchain>=1.4.9,<2",
  "langchain-core>=1.7,<2",
  "langchain-openai>=1.7,<2",
  "langgraph-checkpoint-postgres>=3.2,<4",
  "litellm>=1.108,<2",
  "mcp>=2.3,<3",
  "fastapi>=0.162,<1",
  "uvicorn[standard]>=0.54,<1",
  "sse-starlette>=3.7,<4",
  "pydantic>=2.14,<3",
  "pydantic-settings>=2.16,<3",
  "httpx>=0.28,<1",
  "psycopg[binary,pool]>=3.3,<4",
  "pgvector>=0.5,<1",
  "redis>=8.1,<9",
  "opentelemetry-api>=1.45,<2",
  "opentelemetry-sdk>=1.45,<2",
  "opentelemetry-exporter-otlp-proto-http>=1.45,<2",
  "opentelemetry-instrumentation-langchain>=0.63,<1",
  "opentelemetry-instrumentation-fastapi>=0.63,<1",
  "langfuse>=5.2,<6",
  "python-dotenv>=1.0",
]

[optional-dependencies]
eval = ["deepeval>=4.3,<5"]
sandbox = ["e2b>=2.63,<3"]
a2a = ["a2a-sdk>=1.3,<2"]

[dependency-groups]
dev = ["pytest>=9.2,<10", "pytest-asyncio>=1.4,<2", "ruff>=0.17,<1", "mypy>=2.4,<3"]

[tool.uv]
default-groups = ["dev"]
```

## 4. 校招项目的取舍建议

**值得做（差异化强、可讲深）**：LangGraph `StateGraph` + `AsyncPostgresSaver` + `interrupt()` 审批闭环；LiteLLM `Router` 降级 + `completion_cost` 成本口径；MCP 2.x server（streamable HTTP）与 client 双端；`gen_ai.*` OTel 打点 + Langfuse OTLP；pgvector HNSW + 混合检索 + `ef_search` 调优；DeepEval 进 CI + pass^k 可靠性指标 + trace 评测。

**降级/不做**：A2A 真接入（写一页设计即可）；LangSmith Deployment（闭源+license，只讲"可平移"）；自研 Firecracker/gVisor（改为 `Sandbox` 适配器 + Docker/E2B 双实现）；RAGAS 作为核心依赖（0.x 不稳定）。

## 5. 不确定项（明确标注，未编造）

1. **DeepSeek Responses API 是否支持 `json_schema`**：第三方驱动文档称支持、Chat Completions 明确不支持；DeepSeek 官方 "JSON Output" 页只写 `json_object`，官方功能表也只列 "Json Output"。→ 以 `json_object` 为准，`json_schema` 未获官方确认。
2. **pgvector 0.9.0 发布时间**：CHANGELOG 明确标 `unreleased`；有第三方博客把 "0.9 + PG18 标量量化" 当既成事实，属误读。
3. **GPT-5.6 / Claude 5.5 / Gemini 4 Argon 的逐条官方价格**：仅从官方价目页与聚合页交叉核对，未逐 SKU 验证；报告只给量级档位。
4. **fastmcp 4 与官方 SDK 的长期边界**：官方文档只承诺 `mcp` 包的行为，fastmcp 的路线由个人项目维护，长期兼容性不确定。
5. **`httpx2` 迁移对现有测试的影响范围**：mcp 2.3.0 依赖 `httpx2>=2.10.0`，若项目后续引入 `respx`/`pytest-httpx` 打桩，需改用 mock transport（同类问题已在 anthropic 1.x 上被社区记录）。

---

*核查日：2026-11-10。版本号均取自当日 PyPI 实时元数据与官方文档；项目内结论基于 `D:\E\code\py-agent` 的 `pyproject.toml`、`uv.lock`、`.venv` 与源码实测。*
