from typing import Any

import pytest

from agent_platform.gateway.audit import AuditLogger
from agent_platform.gateway.errors import PermissionDenied, RateLimitExceeded, UnknownTool
from agent_platform.gateway.gateway import Gateway
from agent_platform.gateway.policy import PermissionPolicy, PermissionRule, Principal
from agent_platform.gateway.ratelimit import RateLimiter
from agent_platform.gateway.registry import ToolRegistry


async def _echo(principal: Principal, arguments: dict[str, Any]) -> str:
    return arguments.get("value", "")


def _make_gateway() -> tuple[Gateway, AuditLogger]:
    registry = ToolRegistry()
    registry.register("echo", _echo)
    policy = PermissionPolicy(rules=[PermissionRule(tool="echo", roles=frozenset({"user"}))])
    audit = AuditLogger()
    return Gateway(registry, policy, audit), audit


@pytest.mark.asyncio
async def test_allowed_call_executes_and_audits() -> None:
    gateway, audit = _make_gateway()
    principal = Principal(id="u1", roles=frozenset({"user"}))
    result = await gateway.call(principal, "echo", {"value": "hi"})
    assert result == "hi"
    assert audit.events[-1].outcome == "allowed"


@pytest.mark.asyncio
async def test_denied_call_raises_and_audits() -> None:
    gateway, audit = _make_gateway()
    principal = Principal(id="u2", roles=frozenset({"guest"}))
    with pytest.raises(PermissionDenied):
        await gateway.call(principal, "echo", {"value": "hi"})
    assert audit.events[-1].outcome == "denied"


@pytest.mark.asyncio
async def test_unknown_tool_raises_and_audits() -> None:
    # 工具已授权但未注册（配置错误）：先过权限，再到存在性检查才报 UnknownTool
    registry = ToolRegistry()
    policy = PermissionPolicy(rules=[PermissionRule(tool="nope", roles=frozenset({"user"}))])
    audit = AuditLogger()
    gateway = Gateway(registry, policy, audit)
    principal = Principal(id="u1", roles=frozenset({"user"}))

    with pytest.raises(UnknownTool):
        await gateway.call(principal, "nope", {})
    assert audit.events[-1].outcome == "error"


@pytest.mark.asyncio
async def test_unauthorized_call_does_not_leak_tool_existence() -> None:
    gateway, _ = _make_gateway()
    principal = Principal(id="u2", roles=frozenset({"guest"}))
    # 未授权调用「不存在的工具」应得到 PermissionDenied 而非 UnknownTool（不泄露存在性）
    with pytest.raises(PermissionDenied):
        await gateway.call(principal, "secret_internal_tool", {})


@pytest.mark.asyncio
async def test_tool_error_audited_as_error() -> None:
    async def boom(principal: Principal, arguments: dict[str, Any]) -> str:
        raise ValueError("boom")

    registry = ToolRegistry()
    registry.register("boom", boom)
    policy = PermissionPolicy(rules=[PermissionRule(tool="boom", roles=frozenset({"user"}))])
    audit = AuditLogger()
    gateway = Gateway(registry, policy, audit)
    principal = Principal(id="u1", roles=frozenset({"user"}))

    with pytest.raises(ValueError):
        await gateway.call(principal, "boom", {})
    assert audit.events[-1].outcome == "error"


@pytest.mark.asyncio
async def test_rate_limit_exceeded() -> None:
    registry = ToolRegistry()
    registry.register("echo", _echo)
    policy = PermissionPolicy(rules=[PermissionRule(tool="echo", roles=frozenset({"user"}))])
    audit = AuditLogger()
    limiter = RateLimiter(capacity=1, refill_per_second=0.0)
    gateway = Gateway(registry, policy, audit, rate_limiter=limiter)
    principal = Principal(id="u1", roles=frozenset({"user"}))

    # 第一次通过
    assert await gateway.call(principal, "echo", {"value": "a"}) == "a"
    # 第二次超限
    with pytest.raises(RateLimitExceeded):
        await gateway.call(principal, "echo", {"value": "b"})
    assert audit.events[-1].outcome == "rate_limited"
