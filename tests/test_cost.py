from agent_platform.router.cost import CostTracker, estimate_cost


def test_estimate_cost_unknown_model_is_zero() -> None:
    # 未知模型/报价缺失不应抛异常，也不影响主链路
    assert estimate_cost("definitely-not-a-real-model", 10, 5) == 0.0


def test_cost_tracker_accumulates_and_summarizes() -> None:
    tracker = CostTracker()
    tracker.record("m1", 100, 50, 0.01)
    tracker.record("m1", 100, 50, 0.01)
    tracker.record("m2", 10, 5, 0.0)

    s = tracker.summary()
    assert s.total_prompt_tokens == 210
    assert s.total_completion_tokens == 105
    assert s.total_cost == 0.02
    assert s.by_model["m1"].prompt_tokens == 200
    assert s.by_model["m1"].cost == 0.02
    assert s.by_model["m2"].completion_tokens == 5
