"""评测运行器：把 Agent 跑过 golden dataset，用 LLM-as-judge 打分并聚合。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import BaseModel, Field

from agent_platform.evaluation.dataset import GoldenCase
from agent_platform.evaluation.judge import JudgeVerdict
from agent_platform.gateway.policy import Principal

# 评判函数：问题 + 参考答案 + 实际回答 → 结构化结论。
JudgeFn = Callable[[str, str, str], Awaitable[JudgeVerdict]]


class AgentLike(Protocol):
    """评测对 Agent 的最小依赖（可对话）。"""

    async def chat(
        self, message: str, *, thread_id: str, principal: Principal
    ) -> str: ...


class CaseResult(BaseModel):
    """单条用例的评测结果。"""

    case_id: str
    question: str
    answer: str
    score: int
    reason: str
    passed: bool


class EvalReport(BaseModel):
    """评测报告：逐条结果 + 聚合指标。"""

    total: int
    passed: int
    avg_score: float
    cases: list[CaseResult] = Field(default_factory=list)


class EvaluationHarness:
    """评测运行器：agent 与 judge 均可注入（测试用假实现，线上用真模型）。"""

    def __init__(self, agent: AgentLike, judge: JudgeFn, *, threshold: int = 60) -> None:
        self._agent = agent
        self._judge = judge
        self._threshold = threshold

    async def run(
        self, cases: list[GoldenCase], *, principal: Principal
    ) -> EvalReport:
        """逐条执行并聚合。每条用例用独立 thread_id（eval-<id>）避免上下文串扰。"""
        results: list[CaseResult] = []
        for case in cases:
            answer = await self._agent.chat(
                case.question, thread_id=f"eval-{case.id}", principal=principal
            )
            verdict = await self._judge(case.question, case.reference, answer)
            results.append(
                CaseResult(
                    case_id=case.id,
                    question=case.question,
                    answer=answer,
                    score=verdict.score,
                    reason=verdict.reason,
                    passed=verdict.score >= self._threshold,
                )
            )
        passed = sum(1 for r in results if r.passed)
        avg = sum(r.score for r in results) / len(results) if results else 0.0
        return EvalReport(
            total=len(results), passed=passed, avg_score=round(avg, 2), cases=results
        )
