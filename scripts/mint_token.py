"""签发演示 token（本地演示用；生产由 IdP/网关签发）。

用法：
    uv run python scripts/mint_token.py demo agent
参数：principal_id [role ...]（默认 principal_id=demo、角色 agent，TTL 8 小时）。
"""

from __future__ import annotations

import sys

from agent_platform.gateway.auth import encode_token
from agent_platform.gateway.config import Settings


def main() -> int:
    principal_id = sys.argv[1] if len(sys.argv) > 1 else "demo"
    roles = set(sys.argv[2:]) or {"agent"}
    settings = Settings()
    token = encode_token(principal_id, roles, settings.jwt_secret, ttl_seconds=8 * 3600)
    print(f"principal={principal_id} roles={sorted(roles)}")
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
