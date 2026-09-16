# agent-platform · 企业级多智能体编排平台

> 面向 2026 校招（大厂 Agent 平台组方向）的旗舰项目。
> 一套参考 **2026 七层 Agent 架构**、以 **MCP** 为集成主干、带**可观测 / 评测 / 治理闭环**的企业级智能体平台，Python 为主 + 轻量 React 前端。

## 一句话定位

**证明你能做生产级 Agent 工程，而不是调 API 的 demo。** 落点永远是「可靠、安全、可量化、可扩展」，而不是 Agent 数量。

## 技术栈

`LangGraph` · 自建 `MCP 网关` · `LiteLLM` 模型路由 · `FastAPI` · `PostgreSQL + pgvector` · `Redis` · `OpenTelemetry + Langfuse` · `DeepEval / RAGAS` · `React + Vite`

## 架构（七层）

```
接入层 (FastAPI/SSE)     REST API · 流式 · 会话管理
   ↓
编排层 (LangGraph)       图状态机 · checkpoint · HITL · 子图
   ↓
治理层                   鉴权 · 权限 · 审计 · 护栏 · 成本追踪
   ↓
协议层 (MCP Gateway)     工具发现 · 白名单 · 限流 · 加固 · 审计
   ↓
模型层 (Router)          多供应商路由 · 大/小模型分工 · 重试降级
   ↓
记忆层 (Memory)          工作记忆 · 向量长期记忆 · 会话分域
   ↓
数据层                   PostgreSQL+pgvector · Redis
─────────────────────────────────────────────
横切：可观测 (OTel/Langfuse) · 评测 (DeepEval/RAGAS) · 安全
```

## 特性亮点

- **自建 MCP 网关**：鉴权(JWT) / 权限白名单 / 限流 / 审计日志 / 路径穿越与注入加固
- **LangGraph 编排**：checkpoint、断点续跑、time-travel 调试、真实 HITL 人工审批
- **模型路由**：DeepSeek 为主，大模型规划 + 小模型抽取，token 成本追踪
- **可观测**：OpenTelemetry (GenAI semconv) 全链路 trace → Langfuse（自托管）
- **评测进 CI**：golden dataset + RAGAS/DeepEval + LLM-as-judge 回归

## 落地场景

「企业知识助手 + 工单编排」：RAG 检索回答 + LangGraph 工作流（意图识别 → 信息收集 → 工具执行 → 人工审批），敏感操作走网关白名单与审计。

## 快速开始

```bash
# 1. 安装依赖（uv 管理）
uv sync

# 2. 配置环境变量
cp .env.example .env   # 填入 DEEPSEEK_API_KEY

# 3. 模型冒烟测试
uv run python scripts/smoke_deepseek.py

# 4.（可选）启动基础设施（需安装 Docker Desktop）
docker compose up -d
```

## 目录结构

见 [PLAN.md](./PLAN.md) 第 4 节；设计决策见 [docs/adr/](./docs/adr/)。

## Roadmap

- Phase 0 ✅ 骨架 + 依赖 + CI + ADR + 模型冒烟
- Phase 1 — MCP 网关 + 工具 server + 安全加固
- Phase 2 — LangGraph 编排 + 模型路由 + RAG + HITL
- Phase 3 — 可观测 + 评测进 CI
- Phase 4 — 记忆 + 治理 + 护栏
- Phase 5 — 前端 + 部署 + 面试叙事打磨
