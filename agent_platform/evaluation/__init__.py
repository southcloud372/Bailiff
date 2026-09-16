"""评测层：golden dataset + LLM-as-judge + 运行器。

对应 PLAN.md Phase 3 的「评测进 CI」：用可版本化的 golden dataset 与 LLM 裁判
对 Agent 回答做可量化、可回归的质量评估。评测是「你怎么保证 Agent 质量」的完整
答案——不是「跑通就行」，而是「有 golden、有指标、能进 CI」。
"""
