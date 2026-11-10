"""评测漂移监控：把线上采样跑同一评测管线，对比基线通过率，发现漂移即告警。

与 golden 回归共用同一 judge 与 threshold，保证「评测口径一致」；基线通过率来自上次
全量评测（如 golden 的 pass_rate），容差容忍采样噪声。这是把评测从「改 prompt 回归」
延伸到「线上持续监控」的桥。
"""

from __future__ import annotations

from pydantic import BaseModel

from agent_platform.evaluation.harness import JudgeFn


class DriftSample(BaseModel):
    """一条线上采样：问题 + 参考答案 + Agent 实际回答。"""

    question: str
    reference: str
    answer: str


class DriftReport(BaseModel):
    """漂移检查结果。"""

    sampled: int
    pass_rate: float
    baseline_pass_rate: float
    delta: float  # 基线 - 当前，正数表示变差
    drifted: bool
    reason: str


class DriftMonitor:
    """用同一评测管线给线上采样打分，通过率显著低于基线即判为漂移。"""

    def __init__(
        self,
        judge: JudgeFn,
        *,
        baseline_pass_rate: float,
        tolerance: float = 0.1,
        threshold: int = 60,
    ) -> None:
        self._judge = judge
        self._baseline_pass_rate = baseline_pass_rate
        self._tolerance = tolerance
        self._threshold = threshold

    async def check(self, samples: list[DriftSample]) -> DriftReport:
        """打分并对比基线。无采样时返回未漂移（避免空样本误报）。"""
        if not samples:
            return DriftReport(
                sampled=0,
                pass_rate=0.0,
                baseline_pass_rate=self._baseline_pass_rate,
                delta=0.0,
                drifted=False,
                reason="无采样",
            )
        passed = 0
        for sample in samples:
            verdict = await self._judge(sample.question, sample.reference, sample.answer)
            if verdict.score >= self._threshold:
                passed += 1
        pass_rate = passed / len(samples)
        delta = self._baseline_pass_rate - pass_rate
        drifted = delta > self._tolerance
        if drifted:
            reason = (
                f"通过率从 {self._baseline_pass_rate:.0%} 降至 {pass_rate:.0%}，"
                f"跌幅 {delta:.0%} 超过容差 {self._tolerance:.0%}"
            )
        else:
            reason = "通过率稳定，未检测到漂移"
        return DriftReport(
            sampled=len(samples),
            pass_rate=round(pass_rate, 4),
            baseline_pass_rate=self._baseline_pass_rate,
            delta=round(delta, 4),
            drifted=drifted,
            reason=reason,
        )
