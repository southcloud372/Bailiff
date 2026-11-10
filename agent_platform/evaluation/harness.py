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


class CaseTrials(BaseModel):
    """单条用例多次采样（多轮）的结果。"""

    case_id: str
    scores: list[int]
    passed_any: bool
    passed_all: bool


class TrialsReport(BaseModel):
    """多轮采样报告：pass@k（能力上限）与 pass^k（部署可靠性）。"""

    total: int
    trials: int
    pass_at_k: float
    pass_k: float
    cases: list[CaseTrials] = Field(default_factory=list)


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

    async def run_trials(
        self, cases: list[GoldenCase], *, principal: Principal, trials: int = 3
    ) -> TrialsReport:
        """每条用例跑 ``trials`` 次（独立 thread_id），统计 pass@k 与 pass^k。

        - **pass@k**：至少一次通过（能力上限——答对过就算）；
        - **pass^k**：全部通过（部署可靠性——用户每次只拿到一次抽样）。

        两条指标合看：pass@k 高但 pass^k 低 = 不稳定；两者都高 = 稳定可靠。
        """
        if trials < 1:
            raise ValueError("trials 必须 >= 1")
        case_trials: list[CaseTrials] = []
        for case in cases:
            scores: list[int] = []
            for i in range(trials):
                answer = await self._agent.chat(
                    case.question, thread_id=f"eval-{case.id}-{i}", principal=principal
                )
                verdict = await self._judge(case.question, case.reference, answer)
                scores.append(verdict.score)
            case_trials.append(
                CaseTrials(
                    case_id=case.id,
                    scores=scores,
                    passed_any=any(s >= self._threshold for s in scores),
                    passed_all=all(s >= self._threshold for s in scores),
                )
            )
        total = len(case_trials)
        pass_at_k = sum(1 for c in case_trials if c.passed_any) / total if total else 0.0
        pass_k = sum(1 for c in case_trials if c.passed_all) / total if total else 0.0
        return TrialsReport(
            total=total,
            trials=trials,
            pass_at_k=round(pass_at_k, 4),
            pass_k=round(pass_k, 4),
            cases=case_trials,
        )
