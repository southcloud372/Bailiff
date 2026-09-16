"""成本限额：对累计模型费用 / token 用量做硬上限（token 经济学落地）。

预算检查在**每次模型调用前**执行：累计量已超限 → 直接拒绝新请求（BudgetExceeded），
不浪费 token 去生成一个注定超预算的回答。这与 CostTracker（事后累加）配合：
CostTracker 记账，BudgetPolicy 把关。0 表示该项不限制。
"""

from __future__ import annotations

from agent_platform.router.cost import CostTracker


class BudgetExceeded(Exception):
    """累计成本 / token 已超出预算。"""


class BudgetPolicy:
    """基于 CostTracker 累计量的硬预算。

    - ``cost_limit``：累计费用上限（元），0 = 不限。
    - ``token_limit``：累计 token（prompt + completion）上限，0 = 不限。
    """

    def __init__(self, *, cost_limit: float = 0.0, token_limit: int = 0) -> None:
        self._cost_limit = cost_limit
        self._token_limit = token_limit

    @property
    def active(self) -> bool:
        return self._cost_limit > 0 or self._token_limit > 0

    def check(self, tracker: CostTracker) -> None:
        """若累计量超出任一限额则抛 BudgetExceeded；未超限或未启用则静默通过。"""
        if not self.active:
            return
        summary = tracker.summary()
        if self._cost_limit > 0 and summary.total_cost >= self._cost_limit:
            raise BudgetExceeded(
                f"累计费用 {summary.total_cost:.4f} 元已达上限 {self._cost_limit} 元"
            )
        if self._token_limit > 0:
            total_tokens = summary.total_prompt_tokens + summary.total_completion_tokens
            if total_tokens >= self._token_limit:
                raise BudgetExceeded(
                    f"累计 token {total_tokens} 已达上限 {self._token_limit}"
                )
