# ADR-0002：模型路由用 LiteLLM 抽象，成本追踪作为一等公民

- 状态：已采纳（Accepted）
- 日期：2026-09-16
- 决策者：项目作者

## 背景（Context）

平台需要同时满足：

1. **多供应商可切换**：主用 DeepSeek，预留 Qwen / Claude 做「大模型规划、小模型抽取」的分流与降级；
2. **token 经济学**：5 亿 token 预算需要每次调用可量化成本，才能谈限额、选型与降本。

若把模型调用写死在 LangChain 的 `ChatOpenAI`，供应商即被锁死；若不追踪成本，多 Agent 成本 15x 的坑会直接吃掉预算。

## 决策（Decision）

1. **底层模型路由用 LiteLLM**（OpenAI 兼容的统一接口），自建一层 `ModelRouter` 抽象（`router/llm.py`）做多供应商选择、重试与降级。
2. **成本追踪独立于路由**（`router/cost.py`）：LangChain `BaseCallbackHandler.on_llm_end` 捕获真实 token 用量，LiteLLM 价格表估算费用；计费失败**不阻断主链路**（旁路职责）。
3. **成本限额**（`router/budget.py`）：基于累计量做硬上限，模型调用前拦截，超限直接拒绝，不浪费 token 生成注定超预算的回答。

## 备选方案（Alternatives Considered）

| 方案 | 弃用原因 |
|---|---|
| 只绑 ChatOpenAI + DeepSeek | 供应商锁死，无法展示可移植性 |
| 直接调各家原生 SDK | 每换一家重写一套，工程成本高、无统一降级 |
| 成本事后离线算（日志回放） | 无法在请求路径上做硬限额，token 经济学退化为报表 |

## 后果（Consequences）

- 正面：换供应商只改配置；成本/限额/选型三者闭环，面试可讲「我怎么控 5 亿 token 预算」。
- 代价：多一层抽象（LiteLLM + 自建 Router + 回调），需处理 provider 前缀（如 `deepseek/deepseek-chat`）等细节。
- 缓解：Router 与回调解耦；报价缺失按 0 计且不抛错。

## 参考

- [LiteLLM — cost tracking](https://docs.litellm.ai/docs/proxy/cost_tracking)
- PLAN.md 第 1 节「多 Agent 15x token 成本」
