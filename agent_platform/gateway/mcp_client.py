"""MCP 客户端聚合：把外部 MCP server 接进网关，形成真正的「MCP 网关」。

### 为什么这块决定项目的可信度

很多「MCP 网关」项目其实是「给几个 Python 函数套了个权限装饰器」——面试官问一句
「你的网关怎么发现工具、怎么连一个第三方 MCP server」就穿了。真正的网关必须：

1. **作为 MCP 客户端**连上去（stdio 子进程 / Streamable HTTP），而不是假设工具就是本地函数；
2. 用 ``tools/list`` **动态发现**工具与 JSON Schema，而不是硬编码工具清单；
3. 把发现的工具**转成网关统一签名** ``(principal, args) -> result``，从而复用鉴权/限流/
   审计/护栏——治理不旁路；
4. 名字加 **server 前缀**避免冲突（``filesystem__read_file``），并在权限策略里
   自动补上「默认拒绝 + 显式放行」规则；
5. 单个 server 挂掉**不影响其他 server**（部分可用优于整体不可用）。

### 传输方式

- ``stdio``：网关拉起子进程，走 stdin/stdout（本地/容器内工具最常见）；
- ``http``：连远端 ``/mcp`` 端点（MCP 2026-07-28 的无状态 Streamable HTTP，
  每个请求自包含协议版本，可直接跑在 LB / Serverless 后面）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Sequence
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Literal

from mcp import Client, StdioServerParameters
from mcp import types as mcp_types
from pydantic import BaseModel, Field

from agent_platform.gateway.policy import PermissionPolicy, PermissionRule, Principal
from agent_platform.gateway.registry import ToolFn, ToolRegistry

logger = logging.getLogger(__name__)

TransportKind = Literal["stdio", "http"]


# ── 配置模型 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MCPServerSpec:
    """一个待接入的 MCP server 的描述。"""

    name: str
    transport: TransportKind = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    timeout_seconds: float = 30.0

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> MCPServerSpec:
        """从环境变量里的 JSON 描述构造（便于容器化部署时外部注入）。"""
        transport = str(raw.get("transport", "stdio"))
        if transport not in ("stdio", "http"):
            raise ValueError(f"不支持的 MCP 传输方式: {transport}")
        return cls(
            name=str(raw["name"]),
            transport=transport,  # type: ignore[arg-type]
            command=raw.get("command"),
            args=tuple(raw.get("args", ())),
            env=dict(raw.get("env", {})),
            url=raw.get("url"),
            timeout_seconds=float(raw.get("timeout_seconds", 30.0)),
        )

    def target(self) -> Any:
        """转换成官方 SDK ``Client`` 能接受的目标对象。"""
        if self.transport == "http":
            if not self.url:
                raise ValueError(f"MCP server {self.name} 缺少 url")
            return self.url
        if not self.command:
            raise ValueError(f"MCP server {self.name} 缺少 command")
        return StdioServerParameters(
            command=self.command,
            args=list(self.args),
            env=self.env or None,
        )


class MCPToolDescriptor(BaseModel):
    """从 ``tools/list`` 发现的一个工具。"""

    server: str
    name: str
    qualified_name: str
    description: str
    input_schema: dict[str, Any] = Field(default_factory=dict)


class MCPToolError(Exception):
    """远端工具返回了错误结果（``isError=true``）。"""


# ── 单连接 ───────────────────────────────────────────────────────────────────


class MCPClient:
    """与一个 MCP server 的长连接（进程生命周期内复用，避免每次调用重连）。"""

    def __init__(
        self,
        name: str,
        target: Any,
        *,
        timeout_seconds: float = 30.0,
        tool_prefix: bool = True,
    ) -> None:
        self.name = name
        self._target = target
        self._timeout = timeout_seconds
        self._tool_prefix = tool_prefix
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._tools: list[MCPToolDescriptor] = []
        self.last_error: str | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def tools(self) -> list[MCPToolDescriptor]:
        return list(self._tools)

    async def start(self) -> None:
        """建立连接并拉取工具清单；失败只记录，不抛出（降级为「该 server 不可用」）。"""
        stack = AsyncExitStack()
        try:
            client = await stack.enter_async_context(
                Client(self._target, read_timeout_seconds=self._timeout)
            )
            listing = await client.list_tools()
            self._client = client
            self._stack = stack
            self._tools = [
                MCPToolDescriptor(
                    server=self.name,
                    name=tool.name,
                    qualified_name=self._qualify(tool.name),
                    description=tool.description or "",
                    input_schema=dict(tool.input_schema or {}),
                )
                for tool in listing.tools
            ]
            self.last_error = None
            logger.info(
                "mcp_server_connected",
                extra={"event": "mcp_connected", "server": self.name,
                       "tools": len(self._tools)},
            )
        except Exception as exc:  # 单个 server 失败不应该拖垮整个平台
            self.last_error = str(exc)
            await stack.aclose()
            logger.warning(
                "mcp_server_unavailable",
                extra={"event": "mcp_unavailable", "server": self.name,
                       "error": str(exc)},
            )

    async def stop(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = None
        self._client = None

    def _qualify(self, tool_name: str) -> str:
        return f"{self.name}__{tool_name}" if self._tool_prefix else tool_name

    async def call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """调用远端工具，并把 MCP 结果规范化成网关可返回的字符串。"""
        if self._client is None:
            raise RuntimeError(f"MCP server {self.name} 未连接")
        result = await self._client.call_tool(tool_name, arguments)
        rendered = render_tool_result(result)
        if result.is_error:
            raise MCPToolError(rendered or f"工具 {tool_name} 执行失败")
        return rendered


def render_tool_result(result: mcp_types.CallToolResult) -> str:
    """把 ``CallToolResult`` 压成字符串：优先 structuredContent，其次文本块。"""
    if result.structured_content is not None:
        return json.dumps(result.structured_content, ensure_ascii=False)
    parts: list[str] = []
    for block in result.content or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
        else:
            dumped = getattr(block, "model_dump", None)
            parts.append(
                json.dumps(dumped(), ensure_ascii=False) if callable(dumped) else str(block)
            )
    return "\n".join(parts)


# ── 聚合网关 ─────────────────────────────────────────────────────────────────


class MCPGateway:
    """管理多个 MCP server 的生命周期，并把它们的工具注册进网关。"""

    def __init__(
        self,
        specs: Sequence[MCPServerSpec] = (),
        *,
        auto_allow_roles: frozenset[str] = frozenset({"agent"}),
    ) -> None:
        self._specs = list(specs)
        self._clients: list[MCPClient] = []
        self._auto_allow_roles = auto_allow_roles
        self._started = False

    @property
    def specs(self) -> list[MCPServerSpec]:
        return list(self._specs)

    @property
    def clients(self) -> list[MCPClient]:
        return list(self._clients)

    async def start(self) -> None:
        """并发连接所有 server。任何一个失败都不影响其余（部分可用优先）。"""
        if self._started:
            return
        self._clients = [
            MCPClient(
                spec.name,
                spec.target(),
                timeout_seconds=spec.timeout_seconds,
            )
            for spec in self._specs
        ]
        await asyncio.gather(*(client.start() for client in self._clients))
        self._started = True

    async def stop(self) -> None:
        await asyncio.gather(
            *(client.stop() for client in self._clients), return_exceptions=True
        )
        self._clients = []
        self._started = False

    def health(self) -> dict[str, dict[str, Any]]:
        """各 server 的连接状态（供 ``/readyz`` 与运维面板展示）。"""
        return {
            client.name: {
                "connected": client.connected,
                "tools": len(client.tools),
                "error": client.last_error,
            }
            for client in self._clients
        }

    def register_into(
        self, registry: ToolRegistry, policy: PermissionPolicy
    ) -> list[MCPToolDescriptor]:
        """把已连接 server 的工具注册进网关，并补上默认拒绝策略下的放行规则。

        返回值是被注册的工具描述列表，便于日志/自检（``GET /v1/tools`` 也能看到）。
        """
        registered: list[MCPToolDescriptor] = []
        for client in self._clients:
            if not client.connected:
                continue
            for descriptor in client.tools:
                registry.register(
                    descriptor.qualified_name, _make_tool_fn(client, descriptor)
                )
                policy.rules.append(
                    PermissionRule(
                        tool=descriptor.qualified_name, roles=self._auto_allow_roles
                    )
                )
                registered.append(descriptor)
        return registered


def _make_tool_fn(client: MCPClient, descriptor: MCPToolDescriptor) -> ToolFn:
    """闭包陷阱提醒：循环里创建函数必须用默认参数/工厂绑定当前值。"""

    async def _call(principal: Principal, args: dict[str, Any]) -> str:
        return await client.call(descriptor.name, args)

    return _call


def mcp_tools_as_langchain(  # 预留：给「不用网关、直接挂 MCP 工具」的场景
    descriptors: Sequence[MCPToolDescriptor],
) -> list[dict[str, Any]]:
    """把 MCP 工具描述转成 OpenAI function-calling 的 tools 格式（供 Agent 提示词层）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": d.qualified_name,
                "description": d.description,
                "parameters": d.input_schema or {"type": "object", "properties": {}},
            },
        }
        for d in descriptors
    ]
