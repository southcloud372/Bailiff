import pytest
from mcp.server.mcpserver.exceptions import ToolError

from agent_platform.tools.filesystem import create_filesystem_server


def _text(result) -> str:
    return "".join(c.text for c in result.content)


@pytest.mark.asyncio
async def test_server_exposes_three_tools(tmp_path) -> None:
    server = create_filesystem_server(tmp_path)
    names = {t.name for t in await server.list_tools()}
    assert names == {"read_file", "write_file", "list_dir"}


@pytest.mark.asyncio
async def test_server_write_then_read(tmp_path) -> None:
    server = create_filesystem_server(tmp_path)
    await server.call_tool("write_file", {"path": "n/hi.txt", "content": "你好"})
    result = await server.call_tool("read_file", {"path": "n/hi.txt"})
    assert _text(result) == "你好"


@pytest.mark.asyncio
async def test_server_rejects_traversal(tmp_path) -> None:
    server = create_filesystem_server(tmp_path)
    # 沙箱违规是「预期内失败」：转成 ToolError，消息透传给客户端（而非崩溃隐藏）
    with pytest.raises(ToolError, match="沙箱违规"):
        await server.call_tool("read_file", {"path": "../../secret"})
