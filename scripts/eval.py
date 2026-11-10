"""评测 CLI：对真实 DeepSeek Agent 跑 golden dataset，输出报告。

用法：uv run python scripts/eval.py [--threshold 60]
退出码：全部通过为 0，否则 1（可直接接入 CI 的评测回归 job）。
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from agent_platform.core.agent import build_agent, build_model
from agent_platform.evaluation.dataset import load_dataset
from agent_platform.evaluation.harness import EvaluationHarness
from agent_platform.evaluation.judge import LLMJudge
from agent_platform.gateway.config import ModelSettings
from agent_platform.gateway.policy import Principal
from agent_platform.gateway.wiring import build_platform

DATASET = Path("agent_platform/evaluation/golden.jsonl")


async def _run(threshold: int, trials: int) -> int:
    platform = build_platform()
    settings = ModelSettings()
    agent = build_agent(settings, platform.gateway)
    # 评判模型独立实例（同样走 DeepSeek），与被测 Agent 解耦。
    judge = LLMJudge(build_model(settings))
    principal = Principal(id="eval", roles=frozenset({"agent"}))

    harness = EvaluationHarness(agent, judge.evaluate, threshold=threshold)
    cases = load_dataset(DATASET)
    report = await harness.run(cases, principal=principal)

    print(f"评测结果: {report.passed}/{report.total} 通过，平均分 {report.avg_score}")
    for case in report.cases:
        mark = "✓" if case.passed else "✗"
        print(f"  [{mark}] {case.case_id}: {case.score} 分 — {case.reason}")

    if trials > 1:
        trials_report = await harness.run_trials(cases, principal=principal, trials=trials)
        print(
            f"多轮采样 k={trials}: "
            f"pass@{trials}={trials_report.pass_at_k:.0%}, "
            f"pass^{trials}={trials_report.pass_k:.0%}"
        )
    return 0 if report.passed == report.total else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Agent 平台 golden dataset 评测")
    parser.add_argument("--threshold", type=int, default=60, help="通过分数线（0-100）")
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="每条用例的采样次数（>1 时输出 pass@k / pass^k）",
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.threshold, args.trials))


if __name__ == "__main__":
    raise SystemExit(main())
