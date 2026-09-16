"""API 端到端冒烟：healthz → 签发 token → 列工具 → 写/读文件 → 越权 403 → Agent 对话。

前置：先启动服务（uv run python scripts/serve.py），再跑本脚本。
用法：uv run python scripts/smoke_api.py
"""

from __future__ import annotations

import httpx

from agent_platform.gateway.auth import encode_token
from agent_platform.gateway.config import Settings

BASE = "http://127.0.0.1:8000"


def main() -> None:
    settings = Settings()

    agent = encode_token("smoke-agent", {"agent"}, settings.jwt_secret)
    guest = encode_token("smoke-guest", {"guest"}, settings.jwt_secret)
    agent_headers = {"Authorization": f"Bearer {agent}"}

    with httpx.Client(base_url=BASE, timeout=10) as client:
        r = client.get("/healthz")
        print("healthz", r.status_code, r.json())

        r = client.get("/v1/tools", headers=agent_headers)
        print("tools", r.status_code, r.json())

        r = client.post(
            "/v1/call",
            headers=agent_headers,
            json={
                "tool": "write_file",
                "arguments": {"path": "hello.txt", "content": "你好，校招"},
            },
        )
        print("write", r.status_code, r.json())

        r = client.post(
            "/v1/call",
            headers=agent_headers,
            json={"tool": "read_file", "arguments": {"path": "hello.txt"}},
        )
        print("read", r.status_code, r.json())

        r = client.post(
            "/v1/call",
            headers={"Authorization": f"Bearer {guest}"},
            json={"tool": "read_file", "arguments": {"path": "hello.txt"}},
        )
        print("denied(guest)", r.status_code, r.json())

    # Agent 对话走真实模型，耗时较长，单独放宽超时。
    with httpx.Client(base_url=BASE, timeout=120) as client:
        r = client.post(
            "/v1/chat",
            headers=agent_headers,
            json={"message": "调用 read_file 工具读取 hello.txt，把内容告诉我"},
        )
        print("chat", r.status_code, r.json())


if __name__ == "__main__":
    main()
