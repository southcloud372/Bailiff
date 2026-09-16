from pathlib import Path

import pytest

from agent_platform.gateway.security import SandboxViolation
from agent_platform.tools.filesystem import list_dir, read_file, write_file


def test_write_and_read_roundtrip(tmp_path: Path) -> None:
    write_file(tmp_path, "notes/hello.txt", "你好，企业级 Agent")
    assert read_file(tmp_path, "notes/hello.txt") == "你好，企业级 Agent"


def test_list_dir(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b").mkdir()
    assert list_dir(tmp_path) == ["a.txt", "b"]


def test_read_rejects_traversal(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation):
        read_file(tmp_path, "../../etc/passwd")


def test_write_rejects_absolute(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolation):
        write_file(tmp_path, "/etc/evil", "pwned")


def test_list_dir_rejects_file(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("x", encoding="utf-8")
    with pytest.raises(SandboxViolation):
        list_dir(tmp_path, "f.txt")
