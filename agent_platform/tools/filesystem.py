"""文件系统 MCP server：在沙箱根目录内读写文件、列目录。

演示点：
1. MCP 2.x 用 ``MCPServer`` 定义工具（``FastMCP`` 已在 2.x 改名）；
2. 所有路径经 :func:`resolve_within_root` 沙箱化，防路径穿越；
3. 核心操作与 MCP 包装分离，便于单元测试；
4. 沙箱违规作为「预期内失败」转成 ``ToolError``，消息透传给客户端。
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from agent_platform.gateway.security import SandboxViolation, resolve_within_root


def _sandbox_tool[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """把领域异常 ``SandboxViolation`` 转成预期内的 ``ToolError``。

    预期内的失败（``ToolError``）消息会透传给客户端；崩溃（``UnexpectedToolError``）不会。
    安全违规属于前者，必须让调用方看到「沙箱违规」而非一个无法诊断的崩溃。
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return fn(*args, **kwargs)
        except SandboxViolation as exc:
            raise ToolError(f"沙箱违规: {exc}") from exc

    return wrapper


def read_file(root: str | Path, path: str) -> str:
    """读取根目录内的文本文件。"""
    target = resolve_within_root(root, path)
    return target.read_text(encoding="utf-8")


def write_file(root: str | Path, path: str, content: str) -> str:
    """写入文本到根目录内的文件（自动创建父目录）。"""
    target = resolve_within_root(root, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return str(target)


def list_dir(root: str | Path, path: str = ".") -> list[str]:
    """列出根目录内某个目录下的条目名。"""
    target = resolve_within_root(root, path)
    if not target.is_dir():
        raise SandboxViolation(f"不是目录: {path}")
    return sorted(p.name for p in target.iterdir())


def create_filesystem_server(root: str | Path) -> MCPServer:
    """把文件系统操作包装成 MCP server。"""
    server = MCPServer(
        name="filesystem",
        title="文件系统工具",
        description="在受限根目录内读写文件、列目录。所有路径均被沙箱化。",
        version="0.1.0",
    )

    @server.tool(name="read_file", description="读取根目录内的文本文件内容")
    @_sandbox_tool
    def _read_file(path: str) -> str:
        return read_file(root, path)

    @server.tool(name="write_file", description="写入文本到根目录内的文件")
    @_sandbox_tool
    def _write_file(path: str, content: str) -> str:
        return write_file(root, path, content)

    @server.tool(name="list_dir", description="列出根目录内某个目录的内容")
    @_sandbox_tool
    def _list_dir(path: str = ".") -> list[str]:
        return list_dir(root, path)

    return server
