"""治理模式的测试：只读模式、工具结果截断。

这两条都属于「上线前才想起来要加」的能力，但都必须有测试——因为它们的失效方式
是静默的：只读模式失效会让人以为没事，截断失效会在某个大文件上突然爆掉。
"""

from __future__ import annotations

from pathlib import Path

from agent_platform.core.agent import (
    _MAX_TOOL_RESULT_CHARS,
    _truncate_result,
    build_tools,
)
from agent_platform.gateway.config import Settings
from agent_platform.gateway.wiring import build_platform


def test_read_only_mode_removes_write_capability(tmp_path: Path) -> None:
    """只读模式下写入工具**不存在**，而不是「存在但拒绝」。

    差异很重要：能力不存在时，模型在工具清单里看不到它，不会反复尝试；
    而「注册后拒绝」会让 Agent 陷入 try→denied→try 的循环，既费钱又难解释。
    """
    platform = build_platform(Settings(fs_root=tmp_path, read_only=True))
    names = platform.registry.names()
    assert "read_file" in names
    assert "list_dir" in names
    assert "write_file" not in names

    agent_tools = {t.name for t in build_tools(platform.gateway)}
    assert "write_file" not in agent_tools


def test_write_mode_registers_write_capability(tmp_path: Path) -> None:
    platform = build_platform(Settings(fs_root=tmp_path, read_only=False))
    assert "write_file" in platform.registry.names()


def test_http_fetch_absent_without_allowlist(tmp_path: Path) -> None:
    """默认不出网：没配白名单时连工具都不注册。"""
    assert "http_fetch" not in build_platform(Settings(fs_root=tmp_path)).registry.names()


def test_http_fetch_registered_with_allowlist(tmp_path: Path) -> None:
    platform = build_platform(
        Settings(fs_root=tmp_path, http_allowed_hosts="example.com")
    )
    assert "http_fetch" in platform.registry.names()
    assert "http_fetch" in {t.name for t in build_tools(platform.gateway)}


def test_short_tool_result_passes_through() -> None:
    assert _truncate_result("ok") == "ok"


def test_long_tool_result_is_truncated_with_notice() -> None:
    """截断必须**告诉模型被截断了**，否则它会以为这就是全部内容。"""
    huge = "x" * (_MAX_TOOL_RESULT_CHARS + 500)
    out = _truncate_result(huge)
    assert isinstance(out, str)
    assert out.startswith("x" * _MAX_TOOL_RESULT_CHARS)
    assert "已截断" in out
    assert str(len(huge)) in out


def test_non_string_results_untouched() -> None:
    payload = ["a", "b"]
    assert _truncate_result(payload) is payload
