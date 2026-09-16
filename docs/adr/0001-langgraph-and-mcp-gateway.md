# ADR-0001：编排层选 LangGraph，工具层自建 MCP 网关

- 状态：已采纳（Accepted）
- 日期：2026-09-16
- 决策者：项目作者

## 背景（Context）

构建一个面向校招的企业级 Agent 平台，需要在两个维度做关键技术选型：

1. **编排层**：用什么框架组织 Agent 的控制流、状态与人工介入。
2. **工具层**：用什么协议/方式让 Agent 调用外部系统（数据库、文件、检索、邮件等）。

2026 下半年框架格局已收敛：

- AutoGen 已进入 maintenance 模式，并入 Microsoft Agent Framework；
- LangGraph 是生产度最高的编排框架（图状态机 + checkpoint + HITL）；
- MCP 已成为 Agent 到工具的**事实标准**（月下载约 9700 万、1 万+ 公开 server、已捐 Linux Foundation）。

## 决策（Decision）

1. **编排层用 LangGraph**（图状态机 + checkpoint + interrupt 实现 HITL）。
2. **工具层自建轻量 MCP 网关**，而非直接依赖某个托管/商业网关产品；工具本身用官方 `mcp` SDK 实现为 MCP server。

## 备选方案（Alternatives Considered）

| 方案 | 弃用原因 |
|---|---|
| CrewAI | 上手快但角色抽象难以承载复杂/受监管的确定控制流 |
| Microsoft Agent Framework | 深度绑定 Azure / .NET 生态，与本项目 Python + 中立云定位不符 |
| OpenAI Agents SDK | 供应商锁定，不利于展示模型可移植性 |
| Google ADK 2.0 | 生态较新，A2A 为主、工具生态不如 MCP 成熟 |
| 直接购买/托管 MCP 网关产品 | 不可自托管、治理逻辑不可定制，且失去「我为什么自建」的差异化叙事 |

## 后果（Consequences）

- 正面：checkpoint/断点续跑/time-travel 调试、原生 HITL、全链路可控可观测；自建网关可展示鉴权/限流/审计/加固等 2026 最稀缺的工程能力。
- 代价：编排层锁定后更换代价高（需重写状态 schema 与节点），因此此处决策最谨慎；自建网关需自行承担安全防护，但这正是差异化亮点与学习价值所在。
- 缓解：工具层全部走 MCP，保持可替换；编排层不轻易引入第二个框架。

## 参考

- [State of AI Agents — March 2026](https://github.com/zzhiyuann/state-of-ai-agents)
- [2026 开源 Agent 工具包选型指南 — 阿里云](https://developer.aliyun.com/article/1740959)
