"""组合根：把网关各部件装配成可运行的平台。

生产级 Agent 平台的组装逻辑集中在这里，上层（API / MCP / 编排）只消费一个
已经配置好的 Gateway，不关心内部依赖怎么连。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_platform.gateway.audit import AuditLogger
from agent_platform.gateway.auth import TokenVerifier
from agent_platform.gateway.config import Settings
from agent_platform.gateway.gateway import Gateway
from agent_platform.gateway.policy import PermissionPolicy, PermissionRule, Principal
from agent_platform.gateway.ratelimit import Limiter, RateLimiter
from agent_platform.gateway.registry import ToolFn, ToolRegistry
from agent_platform.tools.filesystem import list_dir, read_file, write_file
from agent_platform.tools.web import FetchPolicy, build_http_tools, parse_hosts


def _filesystem_tools(root: Path) -> dict[str, ToolFn]:
    """把沙箱化的文件系统操作包装成网关可调用的异步工具。"""

    async def read(principal: Principal, args: dict[str, Any]) -> str:
        return read_file(root, args["path"])

    async def write(principal: Principal, args: dict[str, Any]) -> str:
        return write_file(root, args["path"], args["content"])

    async def ls(principal: Principal, args: dict[str, Any]) -> list[str]:
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
        policy: PermissionPolicy,
    ) -> None:
        self.gateway = gateway
        self.verifier = verifier
        self.registry = registry
        self.audit = audit
        self.settings = settings
        self.policy = policy


def build_platform(
    settings: Settings | None = None,
    *,
    extra_tools: dict[str, ToolFn] | None = None,
    extra_rules: list[PermissionRule] | None = None,
    rate_limiter: Limiter | None = None,
) -> Platform:
    """组装一个完整的平台实例。

    ``extra_tools`` / ``extra_rules`` 用于在组合根之外注入额外工具（如知识库检索、
    长期记忆）及其权限规则，让这些工具同样走网关的鉴权/限流/审计。

    ``rate_limiter`` 由调用方按部署形态注入（单机内存 / 多副本 Redis）；
    不传时用设置里的令牌桶参数构造进程内实现，保证本地零依赖可跑。
    """
    settings = settings or Settings()

    registry = ToolRegistry()
    fs_tools = _filesystem_tools(settings.fs_root)
    if settings.read_only:
        # 只读模式：直接不注册写工具。Agent 的工具清单里看不到它，
        # 也就不会出现「模型反复尝试一个注定被拒的操作」这种体验。
        fs_tools.pop("write_file", None)
    for name, fn in fs_tools.items():
        registry.register(name, fn)
    for name, fn in (extra_tools or {}).items():
        registry.register(name, fn)

    # 出网能力默认关闭，配置白名单后才注册（见 tools/web.py 的安全模型）
    http_tools = build_http_tools(
        FetchPolicy(allowed_hosts=parse_hosts(settings.http_allowed_hosts))
    )
    for name, fn in http_tools.items():
        registry.register(name, fn)

    rules = [
        PermissionRule(tool=name, roles=frozenset({"agent"})) for name in fs_tools
    ]
    rules.extend(
        PermissionRule(tool=name, roles=frozenset({"agent"})) for name in http_tools
    )
    rules.extend(extra_rules or [])
    policy = PermissionPolicy(rules=rules)

    audit = AuditLogger()
    limiter = rate_limiter or RateLimiter(
        capacity=settings.rate_limit_capacity,
        refill_per_second=settings.rate_limit_refill_per_second,
    )
    gateway = Gateway(registry, policy, audit, rate_limiter=limiter)
    verifier = TokenVerifier(settings.jwt_secret, algorithm=settings.jwt_algorithm)

    return Platform(gateway, verifier, registry, audit, settings, policy)
