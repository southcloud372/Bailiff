import pytest

from agent_platform.router.budget import BudgetExceeded, BudgetPolicy
from agent_platform.router.cost import CostTracker


def test_budget_inactive_passes() -> None:
    policy = BudgetPolicy()  # 全 0 = 不限制
    tracker = CostTracker()
    tracker.record("m", 1000, 1000, 100.0)
    policy.check(tracker)  # 不应抛异常


def test_budget_cost_limit_exceeded() -> None:
    tracker = CostTracker()
    tracker.record("m", 1000, 1000, 2.0)
    policy = BudgetPolicy(cost_limit=1.0)
    with pytest.raises(BudgetExceeded):
        policy.check(tracker)


def test_budget_cost_limit_not_reached() -> None:
    tracker = CostTracker()
    tracker.record("m", 100, 100, 0.5)
    BudgetPolicy(cost_limit=1.0).check(tracker)  # 未超限


def test_budget_token_limit_exceeded() -> None:
    tracker = CostTracker()
    tracker.record("m", 600, 400, 0.0)
    policy = BudgetPolicy(token_limit=999)
    with pytest.raises(BudgetExceeded):
        policy.check(tracker)


def test_budget_token_limit_not_reached() -> None:
    tracker = CostTracker()
    tracker.record("m", 600, 398, 0.0)
    BudgetPolicy(token_limit=999).check(tracker)
