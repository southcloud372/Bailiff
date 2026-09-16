# ADR-0003：用 LangGraph checkpoint + interrupt 实现真实 HITL 审批

- 状态：已采纳（Accepted）
- 日期：2026-09-16
- 决策者：项目作者

## 背景（Context）

敏感操作（写文件、改权限、发邮件）不能由 Agent 自主执行，必须经过人工审批。但「加个人工」有不同实现深度：

- **假 HITL**：模型输出里带一句「需要审批」，实际流程仍全自动；
- **真 HITL**：执行节点前**暂停**，等待外部信号，审批前不落盘，审批后从断点续跑。

面试官追问「HITL 怎么实现」时，只有后者经得起深挖。

## 决策（Decision）

1. 用 LangGraph 的 `interrupt()` 在「意图识别 → 路由 → 审批 → 执行」工作流里实现**审批门**：审批节点挂起，`Command(resume=...)` 携带 approve/note 恢复。
2. 状态用 `TypedDict` + `NotRequired` 字段建模；`MemorySaver` checkpointer 按 `thread_id` 持久化断点。
3. 审批意见（approve / note）作为恢复输入写回状态，审批前不执行工具（不落盘）。

## 备选方案（Alternatives Considered）

| 方案 | 弃用原因 |
|---|---|
| 业务侧自己写「状态机 + 轮询队列」 | 断点续跑/超时/并发要自己造，复杂度与坑都大 |
| 模型输出里提示需审批 | 不阻断执行，形同虚设 |
| 全部敏感操作都自动执行 + 事后审计 | 无法满足「敏感操作二次确认」的安全诉求 |

## 后果（Consequences）

- 正面：审批门是**真实执行屏障**（审批前不落盘），断点续跑/timetravel 是 LangGraph 原生能力。
- 代价：checkpoint 目前用进程内 `MemorySaver`（重启即失），生产需换 Postgres/Redis 后端。
- 缓解：已预留 checkpointer 抽象，Phase 2 引入 pgvector 后替换为持久化后端即可。

## 参考

- [LangGraph — interrupts (HITL)](https://langchain-ai.github.io/langgraph/concepts/human_in_the_loop/)
- 实现见 `agent_platform/core/workflow.py`
