"""JWT 鉴权：把 token 解析为 Principal。

治理四件套的第一环（身份认证）。token 来自 API 边界，这里只负责验签 + 提取身份。
"""

from __future__ import annotations

import time

import jwt
from jwt.exceptions import InvalidTokenError

from agent_platform.gateway.policy import Principal


class AuthenticationError(Exception):
    """token 缺失、无效或过期。"""


def encode_token(
    principal_id: str,
    roles: set[str] | frozenset[str],
    secret: str,
    *,
    ttl_seconds: int = 3600,
) -> str:
    """签发一个 HS256 token（测试 / 演示用）。"""
    now = int(time.time())
    payload = {
        "sub": principal_id,
        "roles": sorted(roles),
        "iat": now,
        "exp": now + ttl_seconds,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


class TokenVerifier:
    """用共享密钥验签 JWT，并提取 Principal。"""

    def __init__(self, secret: str, algorithm: str = "HS256") -> None:
        self._secret = secret
        self._algorithm = algorithm

    def verify(self, token: str) -> Principal:
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[self._algorithm],
                options={"require": ["sub", "exp"]},
            )
        except InvalidTokenError as exc:
            raise AuthenticationError("token 无效或已过期") from exc

        sub = claims.get("sub")
        if not isinstance(sub, str):
            raise AuthenticationError("token 缺少有效的 sub 声明")

        raw_roles = claims.get("roles", [])
        if not isinstance(raw_roles, list) or not all(isinstance(r, str) for r in raw_roles):
            raise AuthenticationError("token 的 roles 声明无效")

        return Principal(id=sub, roles=frozenset(raw_roles))
