"""工单审批流端到端冒烟：真实 DeepSeek 意图识别 + HITL 审批门。

用法：uv run python scripts/smoke_workflow.py
"""

from __future__ import annotations

import asyncio

from agent_platform.core.workflow import build_ticket_workflow
from agent_platform.gateway.policy import Principal
from agent_platform.gateway.wiring import build_platform


async def main() -> int:
    platform = build_platform()
    principal = Principal(id="demo", roles=frozenset({"agent"}))
    wf = build_ticket_workflow(platform.gateway)
    fs_root = platform.settings.fs_root

    run1 = await wf.start(
        "请把上线版本号「2.0」写入 release.txt（这是敏感写操作）",
        principal=principal,
        thread_id="ticket-1",
    )
    print("[1] 发起工单:", run1.status)
    if run1.interrupt:
        print("    待审批:", run1.interrupt)
    print("    审批前是否已写盘(应为 False):", (fs_root / "release.txt").exists())

    run2 = await wf.resume("ticket-1", principal=principal, approve=True, note="同意上线")
    print("[2] 审批通过:", run2.status)
    print("    答复:", run2.reply)
    print("    审批后是否已写盘(应为 True):", (fs_root / "release.txt").exists())
    print("    审计事件数:", len(platform.audit.events))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
