# 文档索引

按「你想解决什么问题」找，而不是按目录找。

## 我想接进系统

| 文档 | 内容 |
|---|---|
| [../README.md](../README.md) | 60 秒零 Key 演示、能力清单、部署开关（[中文](../README.zh-CN.md)） |
| [api.md](api.md) | 全部端点、SSE 事件类型、错误码、会话与身份语义 |
| [architecture.md](architecture.md) | 七层分层与代码映射、一条请求的完整链路、依赖方向 |
| [adr/](adr/) | 架构决策记录：为什么这样选，以及代价 |

## 我要上线运维

| 文档 | 内容 |
|---|---|
| [operations.md](operations.md) | 部署形态、配置矩阵、备份恢复、升级流程、五类故障处置、容量 |
| [security.md](security.md) | 信任边界、OWASP LLM Top 10 对照、SSRF/IDOR/DoS 防线、上线检查清单 |
| [../deploy/k8s.yaml](../deploy/k8s.yaml) | 最小 K8s 清单（单副本 + SQLite PVC，含探针语义） |

## 阅读顺序建议

```text
第一次来：README → architecture → api（挑自己关心的端点）
要上线：  operations → security → adr/0006、adr/0007（降级与默认拒绝）
要改代码：architecture 的分层表 → 对应目录的模块 docstring
```
