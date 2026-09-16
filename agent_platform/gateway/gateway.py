"""网关编排：权限检查 → 执行 → 审计，串起治理闭环。"""

from __future__ import annotations

import time
from typing import Any, Literal

from agent_platform.gateway.audit import AuditEvent, AuditLogger
from agent_platform.gateway.errors import PermissionDenied, UnknownTool
from agent_platform.gateway.policy import PermissionPolicy, Principal
from agent_platform.gateway.registry import ToolRegistry

Outcome = Literal["allowed", "denied", "error"]


class Gateway:
    """工具调用的治理入口：默认拒绝 → 权限 → 执行 → 全程审计。"""

    def __init__(
        self,
        registry: ToolRegistry,
        policy: PermissionPolicy,
        audit: AuditLogger,
    ) -> None:
        self._registry = registry
        self._policy = policy
        self._audit = audit

    async def call(
        self, principal: Principal, tool: str, arguments: dict[str, Any]
    ) -> Any:
        start = time.perf_counter()

        # 先权限后存在性：不向未授权调用方泄露「工具是否存在」。
        if not self._policy.allows(principal, tool):
            self._record(principal, tool, arguments, "denied", start)
            raise PermissionDenied(tool)

        fn = self._registry.get(tool)
        if fn is None:
            # 已授权但未注册 = 服务端配置错误，按 error 审计
            self._record(principal, tool, arguments, "error", start)
            raise UnknownTool(tool)

        try:
            result = await fn(arguments)
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
