"""网关权限模型：默认拒绝（deny-by-default）+ 显式白名单。

原则：MCP 工具可执行高风险操作（读写文件、发邮件、改数据），绝不能默认放行。
每条规则只做「允许」，命中才放行，否则拒绝。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Principal(BaseModel):
    """调用方身份。JWT 落地前先用 id + roles 表示。"""

    id: str
    roles: frozenset[str] = Field(default_factory=frozenset)


class PermissionRule(BaseModel):
    """单条白名单规则：某工具对某些角色/主体放行。"""

    tool: str
    roles: frozenset[str] = Field(default_factory=frozenset)
    principals: frozenset[str] = Field(default_factory=frozenset)


class PermissionPolicy(BaseModel):
    """默认拒绝的权限策略。"""

    rules: list[PermissionRule] = Field(default_factory=list)

    def allows(self, principal: Principal, tool: str) -> bool:
        """命中任一规则即放行，否则拒绝。"""
        for rule in self.rules:
            if rule.tool != tool:
                continue
            if principal.id in rule.principals:
                return True
            if principal.roles & rule.roles:
                return True
        return False
