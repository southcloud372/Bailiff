"""Agent 端到端冒烟：真实 DeepSeek + 网关文件系统工具。

用法：uv run python scripts/smoke_agent.py
"""

from __future__ import annotations

import asyncio

from agent_platform.core.agent import build_agent
from agent_platform.gateway.config import ModelSettings
from agent_platform.gateway.policy import Principal
from agent_platform.gateway.wiring import build_platform


async def main() -> int:
    platform = build_platform()
    principal = Principal(id="demo", roles=frozenset({"agent"}))
    agent = build_agent(ModelSettings(), platform.gateway, principal)

    reply = await agent.chat(
        "请先调用 write_file 工具，在路径 hello.txt 写入内容「你好，校招」；"
        "然后用 read_file 工具读回 hello.txt，把读到的内容告诉我。"
    )
    print("[成功] Agent 端到端调用正常")
    print("---")
    print(reply)
    print("---")
    print("审计事件数:", len(platform.audit.events))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
