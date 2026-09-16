"""网关编排：限流 → 权限检查 → 执行 → 审计，串起治理闭环。"""

from __future__ import annotations

import time
from typing import Any

from agent_platform.gateway.audit import AuditEvent, AuditLogger, Outcome
from agent_platform.gateway.errors import PermissionDenied, RateLimitExceeded, UnknownTool
from agent_platform.gateway.policy import PermissionPolicy, Principal
from agent_platform.gateway.ratelimit import RateLimiter
from agent_platform.gateway.registry import ToolRegistry


class Gateway:
    """工具调用的治理入口：限流 → 权限 → 执行 → 全程审计。"""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        audit: AuditLogger,
        *,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._audit = audit
        self._rate_limiter = rate_limiter

    def tool_names(self) -> frozenset[str]:
        """已注册工具名（供编排层做工具发现/提示词构造）。"""
        return self._registry.names()

    async def call(
        self, principal: Principal, tool: str, arguments: dict[str, Any]
    ) -> Any:
        start = time.perf_counter()

        # 1. 限流（最早，防 DoS）
        if self._rate_limiter is not None and not self._rate_limiter.allow(principal.id):
            self._record(principal, tool, arguments, "rate_limited", start)
            raise RateLimitExceeded(principal.id)

        # 2. 权限（先权限后存在性：不向未授权调用方泄露「工具是否存在」）
        if not self._policy.allows(principal, tool):
            self._record(principal, tool, arguments, "denied", start)
            raise PermissionDenied(tool)

        # 3. 存在性（已授权但未注册 = 配置错误）
        fn = self._registry.get(tool)
        if fn is None:
            self._record(principal, tool, arguments, "error", start)
            raise UnknownTool(tool)

        # 4. 执行（把调用方身份传给工具，供按用户分域的工具使用）
        try:
            result = await fn(principal, arguments)
        except Exception:
            self._record(principal, tool, arguments, "error", start)
            raise

        self._record(principal, tool, arguments, "allowed", start)
        return result

    def _record(
        self,
        principal: Principal,
        tool: str,
        arguments: dict[str, Any],
        outcome: Outcome,
        start: float,
    ) -> None:
        self._audit.log(
            AuditEvent(
                principal_id=principal.id,
                tool=tool,
                arguments=arguments,
                outcome=outcome,
                duration_ms=(time.perf_counter() - start) * 1000,
            )
        )
