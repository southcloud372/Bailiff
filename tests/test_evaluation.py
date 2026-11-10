from pathlib import Path

import pytest

from agent_platform.evaluation.dataset import GoldenCase, load_dataset
from agent_platform.evaluation.drift import DriftMonitor, DriftSample
from agent_platform.evaluation.harness import EvaluationHarness
from agent_platform.evaluation.judge import JudgeVerdict
from agent_platform.gateway.policy import Principal

GOLDEN = Path(__file__).resolve().parents[1] / "agent_platform/evaluation/golden.jsonl"


class _FakeAgent:
    """按脚本依次返回答案的假 Agent（避免真实模型调用）。"""

    def __init__(self, replies: list[str]) -> None:
        self._replies = list(replies)

    async def chat(
        self, message: str, *, thread_id: str, principal: Principal
    ) -> str:
        return self._replies.pop(0)


async def _judge(question: str, reference: str, answer: str) -> JudgeVerdict:
    """确定性规则裁判：回答含参考答案关键词即满分，否则 0 分。"""
    if reference in answer:
        return JudgeVerdict(score=100, reason="命中参考答案")
    return JudgeVerdict(score=0, reason="未命中参考答案")


@pytest.mark.asyncio
async def test_harness_aggregates_pass_fail() -> None:
    agent = _FakeAgent(["北京", "错误答案"])
    cases = [
        GoldenCase(id="q1", question="首都？", reference="北京"),
        GoldenCase(id="q2", question="1+1？", reference="2"),
    ]
    harness = EvaluationHarness(agent, _judge, threshold=60)
    principal = Principal(id="eval", roles=frozenset({"agent"}))

    report = await harness.run(cases, principal=principal)

    assert report.total == 2
    assert report.passed == 1
    assert report.avg_score == 50.0
    assert report.cases[0].passed is True
    assert report.cases[0].answer == "北京"
    assert report.cases[1].passed is False


@pytest.mark.asyncio
async def test_harness_isolates_thread_per_case() -> None:
    seen_threads: list[str] = []

    class _RecordingAgent:
        async def chat(
            self, message: str, *, thread_id: str, principal: Principal
        ) -> str:
            seen_threads.append(thread_id)
            return "ok"

    agent = _RecordingAgent()
    cases = [GoldenCase(id="a", question="q", reference="r") for _ in range(3)]
    harness = EvaluationHarness(agent, _judge)
    principal = Principal(id="eval", roles=frozenset({"agent"}))

    await harness.run(cases, principal=principal)

    assert seen_threads == ["eval-a", "eval-a", "eval-a"]


def test_load_dataset_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "golden.jsonl"
    path.write_text(
        '{"id":"a","question":"q","reference":"r"}\n'
        '{"id":"b","question":"q2","reference":"r2","category":"code"}\n',
        encoding="utf-8",
    )
    cases = load_dataset(path)
    assert [c.id for c in cases] == ["a", "b"]
    assert cases[1].category == "code"


def test_golden_dataset_is_versioned_and_nonempty() -> None:
    cases = load_dataset(GOLDEN)
    assert len(cases) >= 5
    assert all(c.reference for c in cases)
    assert len({c.id for c in cases}) == len(cases)  # id 唯一


@pytest.mark.asyncio
async def test_run_trials_pass_at_k_vs_pass_k() -> None:
    # q1：第一次失败、第二次成功 → passed_any=True, passed_all=False
    # q2：两次都成功 → passed_any=True, passed_all=True
    agent = _FakeAgent(["错误", "北京", "北京", "北京"])
    cases = [
        GoldenCase(id="q1", question="首都？", reference="北京"),
        GoldenCase(id="q2", question="首都？", reference="北京"),
    ]
    harness = EvaluationHarness(agent, _judge, threshold=60)
    principal = Principal(id="eval", roles=frozenset({"agent"}))

    report = await harness.run_trials(cases, principal=principal, trials=2)

    assert report.total == 2
    assert report.trials == 2
    assert report.pass_at_k == 1.0  # 两条都至少一次通过
    assert report.pass_k == 0.5  # 只有 q2 全部通过
    assert report.cases[0].passed_any is True
    assert report.cases[0].passed_all is False
    assert report.cases[1].passed_all is True


@pytest.mark.asyncio
async def test_run_trials_isolates_trials_per_case() -> None:
    seen: list[str] = []

    class _RecordingAgent:
        async def chat(
            self, message: str, *, thread_id: str, principal: Principal
        ) -> str:
            seen.append(thread_id)
            return "ok"

    harness = EvaluationHarness(_RecordingAgent(), _judge)
    principal = Principal(id="eval", roles=frozenset({"agent"}))
    await harness.run_trials(
        [GoldenCase(id="a", question="q", reference="r")], principal=principal, trials=2
    )
    assert seen == ["eval-a-0", "eval-a-1"]


@pytest.mark.asyncio
async def test_drift_monitor_flags_decline() -> None:
    monitor = DriftMonitor(_judge, baseline_pass_rate=1.0, tolerance=0.1)
    samples = [
        DriftSample(question="首都？", reference="北京", answer="北京"),
        DriftSample(question="首都？", reference="北京", answer="错误"),
    ]
    report = await monitor.check(samples)
    assert report.sampled == 2
    assert report.pass_rate == 0.5
    assert report.delta == 0.5
    assert report.drifted is True


@pytest.mark.asyncio
async def test_drift_monitor_stable_within_tolerance() -> None:
    monitor = DriftMonitor(_judge, baseline_pass_rate=0.5, tolerance=0.1)
    samples = [DriftSample(question="首都？", reference="北京", answer="北京")]
    report = await monitor.check(samples)
    assert report.pass_rate == 1.0
    assert report.delta == -0.5
    assert report.drifted is False


@pytest.mark.asyncio
async def test_drift_monitor_empty_no_false_alarm() -> None:
    monitor = DriftMonitor(_judge, baseline_pass_rate=1.0)
    report = await monitor.check([])
    assert report.sampled == 0
    assert report.drifted is False
