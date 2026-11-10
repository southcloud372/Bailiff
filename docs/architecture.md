# 架构说明

本文把 PLAN.md 的七层架构映射到实际代码，说明「每一层在哪、谁依赖谁、关键决策是什么」。
设计决策的来龙去脉见 [docs/adr/](./adr/)。

## 分层与代码映射

| 层 | 职责 | 代码 |
|---|---|---|
| 接入层 | REST + SSE 流式 + 会话 | `agent_platform/api/app.py` |
| 编排层 | 图状态机 + checkpoint + HITL | `agent_platform/core/agent.py`、`core/workflow.py` |
| 治理层 | 鉴权 / 权限 / 审计 / 护栏 / 成本 | `agent_platform/gateway/`、`guardrails/`、`router/budget.py` |
| 协议层 | MCP 网关（动态发现 / 命名空间 / 白名单 / 容错） | `agent_platform/gateway/mcp_client.py`、`tools/` |
| 模型层 | 多供应商路由 + 成本追踪 | `agent_platform/router/llm.py`、`router/cost.py` |
| 记忆层 | 工作记忆（checkpoint 三级后端）/ 长期记忆（pgvector） | `core/checkpoint.py` + `memory/` |
| 数据层 | PostgreSQL+pgvector / Redis | `docker-compose.yml`（Phase 0/2） |

横切：评测（`evaluation/`）、可观测（`observability/`：指标 / trace / 就绪探针）、安全（`gateway/security.py`、`guardrails/`）。

> 更完整的分层说明与逐模块精读见 [docs/learning/](./learning/README.md)。

## 关键数据流

### 对话（/v1/chat 与 /v1/chat/stream）

```
客户端 JWT → FastAPI _get_principal → Principal
    → Agent.chat/stream（输入护栏 → 预算检查 → LangGraph astream）
        → create_agent 图（ReAct：模型 ⇄ 网关工具）
            → 工具执行经 contextvar 取 current_principal → Gateway.call
                → 鉴权/权限/限流/审计 → 文件系统 MCP 工具
    → 输出护栏（非流式）→ 返回
```

身份贯穿：`principal_context`（`core/agent.py`）用 `contextvars` 把「当前调用方」注入工具执行路径，
工具层不感知 HTTP，治理不旁路。

### 工单审批（/v1/tickets）

```
POST /v1/tickets → TicketWorkflow.start
    意图识别（结构化输出 function_calling）
    → 路由：普通 → 直接执行；敏感 → 审批节点 interrupt() 挂起
    → 返回 {status:"interrupted", interrupt:{...}}
POST /v1/tickets/{thread_id}/resume {approve, note}
    → Command(resume=...) 恢复 → 通过则执行工具 → 返回 {status:"completed"}
```

审批门是**真实执行屏障**：`interrupt()` 之前工具不落盘（见 `tests/test_api.py::test_ticket_interrupt_and_resume_approve`）。

## 依赖方向

```
api ──▶ core ──▶ gateway ──▶ tools
   │        │
   │        └──▶ router（llm / cost / budget）
   └──▶ guardrails ──▶ （无）
```

- **API 层依赖倒置**：`create_app` 只依赖 `ChatAgent` Protocol，不 import LangChain 具体类型；
  Agent / workflow / cost_tracker 由 `scripts/serve.py` 组装后注入。
- **编排层最谨慎**：状态 schema 与图结构一旦定型，换框架代价高（ADR-0001）。
- **协议层最标准化**：工具全部走 MCP，换 server 只改注册。

## 为什么「企业级」落点在治理而非 Agent 数量

单 Agent 能解决 70% 场景，多 Agent 有 15x token 成本。本项目的工作流为主 + 按需拆分，
把精力花在**可靠、安全、可量化、可扩展**：鉴权/权限/审计/护栏/成本限额/评测进 CI。
