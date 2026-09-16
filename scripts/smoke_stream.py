"""Agent 流式冒烟：真实 DeepSeek，逐 token 打印回复。

用法：uv run python scripts/smoke_stream.py
"""

from __future__ import annotations

import asyncio
import sys

from agent_platform.core.agent import build_agent
from agent_platform.gateway.config import ModelSettings
from agent_platform.gateway.policy import Principal
from agent_platform.gateway.wiring import build_platform


async def main() -> int:
    platform = build_platform()
    principal = Principal(id="demo", roles=frozenset({"agent"}))
    agent = build_agent(ModelSettings(), platform.gateway)

    print("流式输出: ", end="", flush=True)
    async for chunk in agent.stream(
        "用一句话介绍你自己，并说明你是一个什么样的 Agent 平台。",
        principal=principal,
    ):
        print(chunk, end="", flush=True)
    print("\n---")
    print("审计事件数:", len(platform.audit.events))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
