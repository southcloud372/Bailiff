"""网关安全加固：路径穿越防护。

MCP 工具的入参来自 LLM（可能被 prompt 注入污染）或外部调用，必须一律视为不可信。
2026 年对 2614 个 MCP server 的审计显示 82% 存在路径穿越、67% 存在代码注入，
网关必须在工具执行之前拦截这类风险——这正是「自建网关」的价值所在。
"""

from __future__ import annotations

from pathlib import Path


class SandboxViolation(Exception):
    """访问越过了沙箱根目录（路径穿越 / 符号链接逃逸）。"""


def resolve_within_root(root: str | Path, requested: str) -> Path:
    """把用户请求的路径解析到 ``root`` 之内，否则抛 :class:`SandboxViolation`。

    防御点：
    1. 拒绝绝对路径（``/etc/passwd``、``C:\\Windows\\...``）；
    2. 拒绝 ``..`` 穿越（``../../secret``）；
    3. ``Path.resolve()`` 展开符号链接后再判断，防止 symlink 逃逸；
    4. 拒绝空路径与 null 字节（历史绕过手法）。
    """
    if not requested or "\x00" in requested:
        raise SandboxViolation("空路径或包含 null 字节的路径")

    root_path = Path(root).resolve()
    candidate = Path(requested)

    if candidate.is_absolute():
        raise SandboxViolation(f"不允许绝对路径: {requested}")

    resolved = (root_path / candidate).resolve()

    if not resolved.is_relative_to(root_path):
        raise SandboxViolation(f"路径越过沙箱根目录: {requested}")

    return resolved
