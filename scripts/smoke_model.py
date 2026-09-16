"""模型路由冒烟：通过 ModelRouter 走 DeepSeek 真实调用。

用法：uv run python scripts/smoke_model.py
"""

from __future__ import annotations

import asyncio

from agent_platform.router.llm import build_router


async def main() -> int:
    router = build_router()
    result = await router.complete(
        [
            {"role": "system", "content": "你是校招求职 Agent 的模型路由冒烟测试。"},
            {"role": "user", "content": "用一句话介绍你自己，并说明你适合做什么。"},
        ],
        max_tokens=128,
        temperature=0.3,
    )
    print("[成功] 模型路由调用正常")
    print(f"  model             : {result.model}")
    print(f"  prompt_tokens     : {result.prompt_tokens}")
    print(f"  completion_tokens : {result.completion_tokens}")
    print(f"  total_tokens      : {result.total_tokens}")
    print(f"  reply             : {result.content}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
