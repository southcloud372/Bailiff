# 文档索引

按「你想解决什么问题」找，而不是按目录找。

## 我想跑起来

| 文档 | 内容 |
|---|---|
| [../README.md](../README.md) | 60 秒零 Key 演示、能力清单、部署开关（[中文](../README.zh-CN.md)） |
| [demo.md](demo.md) | 面试/演示用的执行脚本（按顺序跑，不翻车） |

## 我想接进系统

| 文档 | 内容 |
|---|---|
| [api.md](api.md) | 全部端点、SSE 事件类型、错误码、会话与身份语义 |
| [architecture.md](architecture.md) | 七层分层与代码映射、一条请求的完整链路、依赖方向 |
| [adr/](adr/) | 架构决策记录：为什么这样选，以及代价 |

## 我要上线运维

| 文档 | 内容 |
|---|---|
| [operations.md](operations.md) | 部署形态、配置矩阵、备份恢复、升级流程、五类故障处置、容量 |
| [security.md](security.md) | 信任边界、OWASP LLM Top 10 对照、SSRF/IDOR/DoS 防线、上线检查清单 |

## 我想学 Agent 开发

| 文档 | 内容 |
|---|---|
| [learning/](learning/README.md) | 15 篇从零开始的技术栈教程（Python → LLM → Agent 循环 → LangGraph → MCP → RAG → 记忆 → 评测 → 生产化） |
| [learning/03-跑通与调试手册.md](learning/03-跑通与调试手册.md) | 含两个真实生产故障的完整复盘 |

## 我想把它变成开源项目

| 文档 | 内容 |
|---|---|
| [launch-playbook.md](launch-playbook.md) | 冷启动方案：渠道、节奏、内容选题、转化率常识 |
| [stack-verification-2026-11.md](stack-verification-2026-11.md) | 技术栈版本核查（含已停用模型与替代方案） |

## 阅读顺序建议

```text
第一次来：README → architecture → api（挑自己关心的端点）→ learning/13（编排）→ learning/14（MCP）
要上线：  operations → security → adr/0006、adr/0007（降级与默认拒绝）
要面试：  README「两个真实坑」→ learning/03 附录 → learning/04
```
