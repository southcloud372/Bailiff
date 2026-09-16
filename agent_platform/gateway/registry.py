"""工具注册表：网关内部路由的工具索引。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

ToolFn = Callable[[dict[str, Any]], Awaitable[Any]]


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
