"""工具注册表：网关内部路由的工具索引。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agent_platform.gateway.policy import Principal

# 工具函数统一签名：调用方身份 + 参数 → 结果。身份用于按用户分域（如长期记忆）。
ToolFn = Callable[[Principal, dict[str, Any]], Awaitable[Any]]


class ToolRegistry:
    """按名称索引工具的注册表。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolFn] = {}

    def register(self, name: str, fn: ToolFn) -> None:
        self._tools[name] = fn

    def get(self, name: str) -> ToolFn | None:
        return self._tools.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._tools)
