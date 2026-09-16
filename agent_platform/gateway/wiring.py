"""组合根：把网关各部件装配成可运行的平台。

企业级 Agent 平台的组装逻辑集中在这里——上层（API / MCP / 编排）只消费一个
已经配置好的 Gateway，不关心内部依赖怎么连。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from agent_platform.gateway.audit import AuditLogger
from agent_platform.gateway.auth import TokenVerifier
from agent_platform.gateway.config import Settings
from agent_platform.gateway.gateway import Gateway
from agent_platform.gateway.policy import PermissionPolicy, PermissionRule
from agent_platform.gateway.ratelimit import RateLimiter
from agent_platform.gateway.registry import ToolRegistry
from agent_platform.tools.filesystem import list_dir, read_file, write_file

ToolFn = Callable[[dict[str, Any]], Awaitable[Any]]


def _filesystem_tools(root: Path) -> dict[str, ToolFn]:
    """把沙箱化的文件系统操作包装成网关可调用的异步工具。"""

    async def read(args: dict[str, Any]) -> str:
        return read_file(root, args["path"])

    async def write(args: dict[str, Any]) -> str:
        return write_file(root, args["path"], args["content"])

    async def ls(args: dict[str, Any]) -> list[str]:
        return list_dir(root, args.get("path", "."))

    return {"read_file": read, "write_file": write, "list_dir": ls}


class Platform:
    """组合根产物：网关 + 鉴权 + 配置，供上层统一消费。"""

    def __init__(
        self,
        gateway: Gateway,
        verifier: TokenVerifier,
        registry: ToolRegistry,
        audit: AuditLogger,
        settings: Settings,
    ) -> None:
        self.gateway = gateway
        self.verifier = verifier
        self.registry = registry
        self.audit = audit
        self.settings = settings


def build_platform(settings: Settings | None = None) -> Platform:
    """组装一个完整的平台实例。"""
    settings = settings or Settings()

    registry = ToolRegistry()
    for name, fn in _filesystem_tools(settings.fs_root).items():
        registry.register(name, fn)

    policy = PermissionPolicy(
        rules=[
            PermissionRule(tool="read_file", roles=frozenset({"agent"})),
            PermissionRule(tool="write_file", roles=frozenset({"agent"})),
            PermissionRule(tool="list_dir", roles=frozenset({"agent"})),
        ]
    )

    audit = AuditLogger()
    limiter = RateLimiter(
        capacity=settings.rate_limit_capacity,
        refill_per_second=settings.rate_limit_refill_per_second,
    )
    gateway = Gateway(registry, policy, audit, rate_limiter=limiter)
    verifier = TokenVerifier(settings.jwt_secret, algorithm=settings.jwt_algorithm)

    return Platform(gateway, verifier, registry, audit, settings)
