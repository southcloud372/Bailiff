from pathlib import Path
from typing import Any

import pytest

from agent_platform.core.workflow import (
    ClassifyResult,
    build_ticket_workflow,
)
from agent_platform.gateway.config import Settings
from agent_platform.gateway.policy import Principal
from agent_platform.gateway.wiring import build_platform


def _principal() -> Principal:
    return Principal(id="agent", roles=frozenset({"agent"}))


def _wf(tmp_path: Path, classify, answer):
    platform = build_platform(Settings(fs_root=tmp_path))
    wf = build_ticket_workflow(platform.gateway, classify=classify, answer=answer)
    return wf, platform


@pytest.mark.asyncio
async def test_query_intent_runs_tool_without_interrupt(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hi", encoding="utf-8")

    async def classify(request: str) -> ClassifyResult:
        return ClassifyResult(intent="query", tool_name="read_file", tool_args={"path": "a.txt"})

    async def answer(request: str, result: Any) -> str:
        return f"内容：{result}"

    wf, platform = _wf(tmp_path, classify, answer)
    run = await wf.start("读 a.txt", principal=_principal(), thread_id="t1")

    assert run.status == "completed"
    assert run.interrupt is None
    assert run.reply == "内容：hi"
    # 普通查询：直接执行工具，审计有记录
    assert len(platform.audit.events) == 1


@pytest.mark.asyncio
async def test_sensitive_intent_interrupts_then_approve_executes(tmp_path: Path) -> None:
    async def classify(request: str) -> ClassifyResult:
        return ClassifyResult(
            intent="sensitive",
            tool_name="write_file",
            tool_args={"path": "b.txt", "content": "上线配置"},
        )

    async def answer(request: str, result: Any) -> str:
        return f"已写入 {result}"

    wf, platform = _wf(tmp_path, classify, answer)

    run1 = await wf.start("写上线配置", principal=_principal(), thread_id="t2")
    assert run1.status == "interrupted"
    assert run1.interrupt is not None
    assert run1.interrupt["type"] == "approval"
    assert run1.interrupt["action"] == "write_file"
    # 审批前绝不落盘
    assert not (tmp_path / "b.txt").exists()
    assert len(platform.audit.events) == 0

    run2 = await wf.resume("t2", principal=_principal(), approve=True, note="同意")
    assert run2.status == "completed"
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "上线配置"
    assert len(platform.audit.events) == 1


@pytest.mark.asyncio
async def test_sensitive_intent_reject_skips_execution(tmp_path: Path) -> None:
    async def classify(request: str) -> ClassifyResult:
        return ClassifyResult(
            intent="sensitive",
            tool_name="write_file",
            tool_args={"path": "c.txt", "content": "不该写"},
        )

    async def answer(request: str, result: Any) -> str:
        return f"不应被调用的 answer：{result}"

    wf, platform = _wf(tmp_path, classify, answer)

    run1 = await wf.start("写敏感文件", principal=_principal(), thread_id="t3")
    assert run1.status == "interrupted"

    run2 = await wf.resume("t3", principal=_principal(), approve=False, note="驳回")
    assert run2.status == "completed"
    assert "拒绝" in (run2.reply or "")
    # 驳回：工具未执行、未落盘、无审计
    assert not (tmp_path / "c.txt").exists()
    assert len(platform.audit.events) == 0
