# ADR-0004：评测用 golden dataset + 自建 LLM-as-judge，先于重框架引入

- 状态：已采纳（Accepted）
- 日期：2026-09-16
- 决策者：项目作者

## 背景（Context）

「你怎么保证 Agent 质量」是面试必问。答案不能是「跑通了」，而要是「有 golden、有指标、能进 CI」。

候选路线：
- 直接用 **DeepEval / RAGAS**（框架现成指标，RAG 类指标需检索上下文）；
- 自建一套**轻量评测闭环**（golden dataset + LLM 结构化打分 + pytest 单测 + CLI 接 CI）。

项目当前 RAG 管道被 pgvector（需 Docker）阻塞，RAGAS 的 Faithfulness / Context Precision 等指标暂时无输入数据。

## 决策（Decision）

1. **先自建评测闭环**（`evaluation/`）：`GoldenCase` JSONL 版本化 golden dataset；`LLMJudge` 用 `with_structured_output(method="function_calling")` 让评判模型输出结构化结论（0-100 分 + 理由）；`EvaluationHarness` 逐条执行、聚合通过率与均分。
2. **judge 与 agent 解耦**：评判模型独立实例，避免「自己给自己打分」的偏向。
3. **进 CI**：`scripts/eval.py` 以退出码表达全绿/失败，作为 main 推送的独立 job（`DEEPSEEK_API_KEY` secret 门控）。

## 备选方案（Alternatives Considered）

| 方案 | 弃用原因 |
|---|---|
| 直接上 DeepEval/RAGAS | RAG 指标缺输入；重框架在没 RAG 前性价比低 |
| 只写单测，不做模型评测 | 无法验证「模型质量」，只剩工程正确性 |
| 人工逐条核对 | 不可回归、不可进 CI |

## 后果（Consequences）

- 正面：评测闭环自研，讲得清「指标怎么定义、judge 怎么避免偏向、怎么进 CI」；RAG 就绪后可平滑叠加 RAGAS 指标。
- 代价：LLM-as-judge 本身有噪声（评分非确定性），golden dataset 需维护。
- 缓解：golden 控制在约 50 条以内、版本化管理；judge 与 agent 模型解耦；阈值可调。

## 参考

- 实现见 `agent_platform/evaluation/`、`scripts/eval.py`、`.github/workflows/ci.yml`
