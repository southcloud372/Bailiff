import sys
from pathlib import Path

import pytest

from agent_platform.gateway.security import SandboxViolation, resolve_within_root


def test_resolve_within_root_ok(tmp_path: Path) -> None:
    resolved = resolve_within_root(tmp_path, "a/b.txt")
    assert resolved == (tmp_path / "a" / "b.txt").resolve()


def test_absolute_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation):
        resolve_within_root(tmp_path, "/etc/passwd")


def test_parent_traversal_rejected(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation):
        resolve_within_root(tmp_path, "../../secret.txt")


def test_windows_absolute_rejected(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation):
        resolve_within_root(tmp_path, "C:\\Windows\\System32\\config\\SAM")


def test_null_byte_rejected(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation):
        resolve_within_root(tmp_path, "a.txt\x00.jpg")


def test_empty_path_rejected(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation):
        resolve_within_root(tmp_path, "")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows 上创建 symlink 需要管理员权限或开发者模式；CI(Linux) 会覆盖此用例",
)
def test_symlink_escape_rejected(tmp_path: Path) -> None:
    outside = tmp_path.parent / "secret.txt"
    outside.write_text("secret", encoding="utf-8")
    (tmp_path / "link").symlink_to(outside)
    with pytest.raises(SandboxViolation):
        resolve_within_root(tmp_path, "link")
