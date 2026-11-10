# 学习文档总索引

这套文档分两条线，**交叉读**才有效：

- **A 线（01-04）**：吃透这个项目本身。面试官问的每个问题，都应该能落回某个文件。
- **B 线（10-19）**：从零补齐 Agent 技术栈。看代码卡住时回来查原理。

## 01-04：项目线

| 文档 | 解决什么问题 | 什么时候读 |
|---|---|---|
| [01-项目全景与架构](./01-项目全景与架构.md) | 这个项目到底是什么、七层怎么落成代码、一次请求怎么流过去 | 第一天 |
| [02-代码地图与逐层精读](./02-代码地图与逐层精读.md) | 每个模块负责什么、关键函数在哪、为什么这么写 | 第 2-3 天，配合源码 |
| [03-跑通与调试手册](./03-跑通与调试手册.md) | 三条启动路径（零依赖 / 全量 / 生产）、常见报错、怎么加自己的功能 | 动手时随时查 |
| [04-面试追问手册](./04-面试追问手册.md) | 30 个真会被问的问题 + 答题要点 + 对应的代码位置 | 面试前两周反复过 |

## 10-19：技术栈线（从零）

| 文档 | 内容 | 前置 |
|---|---|---|
| [10-Python与工程基础](./10-Python与工程基础.md) | 类型注解、async、contextvars、Pydantic、uv、pytest | 无 |
| [11-LLM原理与提示工程](./11-LLM原理与提示工程.md) | token、上下文、结构化输出、注入攻击 | 10 |
| [12-Agent核心循环与FunctionCalling](./12-Agent核心循环与FunctionCalling.md) | ReAct、工具 schema、手写 30 行 Agent 循环 | 11 |
| [13-LangChain与LangGraph](./13-LangChain与LangGraph.md) | create_agent、StateGraph、checkpoint、interrupt | 12 |
| [14-MCP协议与工具生态](./14-MCP协议与工具生态.md) | MCP 架构、三种传输、2026-07-28 修订、网关设计 | 12 |
| [15-RAG与向量检索](./15-RAG与向量检索.md) | 切分、嵌入、HNSW、混合检索、8 个失败模式 | 11 |
| [16-记忆与上下文工程](./16-记忆与上下文工程.md) | 三层记忆、摘要压缩、分域、什么时候不该有记忆 | 13 |
| [17-评测与可观测](./17-评测与可观测.md) | pass@k/pass^k、LLM-judge、trace/metric、CI 门禁 | 13 |
| [18-生产化与部署治理](./18-生产化与部署治理.md) | 鉴权/限流/审计/熔断、探针、容器化 | 13、17 |
| [19-16周学习计划](./19-16周学习计划.md) | 周计划、验收标准、简历写法 | 全部 |

## 三条推荐路径

**路径一：先能跑，再能讲（推荐，适合时间紧）**
```
01 → 03（跑起来）→ 12 → 13 → 02（挑 core/ 和 gateway/ 精读）→ 04
```

**路径二：从零系统学（适合基础薄弱、有 3-4 个月）**
```
10 → 11 → 12 → 13 → 14 → 15 → 16 → 17 → 18
期间穿插：01 → 02 → 03
最后：19 排期 + 04 模拟面试
```

**路径三：面试冲刺（只剩 2-3 周）**
```
04（先看问题，标记不会的）→ 02（按问题定位代码）→ 13 + 17（最高频两块）→ 04 再刷一遍
```

## 怎么用这套文档

1. **不要只读**。每读完一节，去 `agent_platform/` 里找到对应文件，改一行、跑一次、看结果。
2. **每个「为什么」都要能自己复述**。这个项目的注释写得比大多数开源项目详细，很多答案就在 docstring 里。
3. **准备一个「问题 → 文件」映射表**。面试时被问到任何设计，能立刻说出「这块在 `gateway/gateway.py`，顺序是限流→权限→存在性→执行」。这比背概念有说服力得多。

## 一页速查：关键代码位置

| 想讲的能力 | 打开这个文件 | 核心函数/类 |
|---|---|---|
| Agent 主循环 | `core/agent.py` | `build_agent` / `Agent.chat` |
| 人工审批（HITL） | `core/workflow.py` | `approval_node`（`interrupt()`） |
| 状态持久化 | `core/checkpoint.py` | `build_checkpointer` / `SqliteCheckpointSaver` |
| 工具治理 | `gateway/gateway.py` | `Gateway.call` |
| MCP 真网关 | `gateway/mcp_client.py` | `MCPGateway.register_into` |
| 权限模型 | `gateway/policy.py` | `PermissionPolicy.allows` |
| 限流 | `gateway/ratelimit.py` | `RedisRateLimiter.aallow` |
| 审计 | `gateway/audit.py` | `AuditLogger.log` |
| 路径沙箱 | `gateway/security.py` | `resolve_within_root` |
| 模型路由/成本 | `router/cost.py`、`router/llm.py` | `CostTracker` / `ModelRouter` |
| 语义缓存 | `router/cache.py` | `SemanticCache` |
| 重试/熔断 | `router/resilience.py` | `RetryPolicy` / `CircuitBreaker` |
| 护栏 | `guardrails/guards.py` | `default_input_guard` / `mask` |
| RAG | `knowledge/rag.py`、`store.py` | `RAGPipeline` / `PgVectorStore` |
| 长期记忆 | `memory/service.py` | `MemoryService` |
| 评测 | `evaluation/harness.py` | `run_trials`（pass@k/pass^k） |
| 指标 | `observability/metrics.py` | `METRICS.render` |
| 就绪探针 | `observability/health.py` | `Readiness.check` |
| 接入层 | `api/app.py`、`middleware.py` | `create_app` / `RequestContextMiddleware` |
| 组装根 | `scripts/serve.py` | `main()` |

## 另一个入口：把它当成开源项目来读

如果你不只是想学技术，还想把这个仓库变成简历/作品集：

- 项目定位与对外叙述：仓库根目录 `README.md`（英文主文档）/ `README.zh-CN.md`
- 两个真实生产故障的复盘：[`03-跑通与调试手册.md`](./03-跑通与调试手册.md) 文末附录
- 开源冷启动与推广节奏：[`../launch-playbook.md`](../launch-playbook.md)
- 架构取舍与决策记录：[`../adr/`](../adr/)

读代码的顺序不用变，但每读完一层，可以顺手回答一个问题：
**"这一层如果去掉，线上会出什么事？"** 能把答案说清楚，就说明这一层真的读懂了。
