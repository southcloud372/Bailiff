from pathlib import Path

import pytest

from agent_platform.gateway.config import Settings
from agent_platform.gateway.errors import PermissionDenied
from agent_platform.gateway.policy import Principal
from agent_platform.gateway.wiring import build_platform


def test_build_platform_registers_filesystem_tools(tmp_path: Path) -> None:
    platform = build_platform(Settings(fs_root=tmp_path))
    assert platform.registry.names() == frozenset({"read_file", "write_file", "list_dir"})


@pytest.mark.asyncio
async def test_build_platform_end_to_end(tmp_path: Path) -> None:
    platform = build_platform(Settings(fs_root=tmp_path))
    agent = Principal(id="u1", roles=frozenset({"agent"}))
    guest = Principal(id="u2", roles=frozenset({"guest"}))

    written = await platform.gateway.call(
        agent, "write_file", {"path": "x.txt", "content": "hi"}
    )
    assert written.endswith("x.txt")
    assert await platform.gateway.call(agent, "read_file", {"path": "x.txt"}) == "hi"

    with pytest.raises(PermissionDenied):
        await platform.gateway.call(guest, "read_file", {"path": "x.txt"})
