"""长期记忆服务单元测试（FakeEmbedder + InMemoryVectorStore，不依赖 Docker）。"""

import pytest

from agent_platform.gateway.policy import Principal
from agent_platform.knowledge.embedder import FakeEmbedder
from agent_platform.knowledge.rag import RAGPipeline
from agent_platform.knowledge.store import InMemoryVectorStore
from agent_platform.knowledge.tools import build_knowledge_tools
from agent_platform.memory.service import MemoryService


@pytest.mark.asyncio
async def test_remember_and_recall_scoped_by_user() -> None:
    store = InMemoryVectorStore()
    memory = MemoryService(store, FakeEmbedder(dim=64))

    await memory.remember("我住在北京，做后端", user_id="u1")
    await memory.remember("我喜欢咖啡", user_id="u2")

    u1 = await memory.recall("用户住在哪里", user_id="u1")
    u2 = await memory.recall("用户喜欢什么", user_id="u2")

    assert u1, "u1 应召回自己的记忆"
    assert u2, "u2 应召回自己的记忆"
    # 分域：u1 不应看到 u2 的记忆
    assert all(h.metadata["user_id"] == "u1" for h in u1)
    assert all(h.metadata["user_id"] == "u2" for h in u2)


@pytest.mark.asyncio
async def test_remember_returns_unique_id() -> None:
    memory = MemoryService(InMemoryVectorStore(), FakeEmbedder(dim=64))
    first = await memory.remember("同一句话", user_id="u1")
    second = await memory.remember("同一句话", user_id="u1")
    assert first != second
    assert first.startswith("mem:u1:")


@pytest.mark.asyncio
async def test_knowledge_tools_use_principal_identity() -> None:
    store = InMemoryVectorStore()
    memory = MemoryService(store, FakeEmbedder(dim=64))
    rag = RAGPipeline(store, FakeEmbedder(dim=64))
    tools = build_knowledge_tools(rag, memory)
    assert set(tools) == {"search_knowledge", "remember", "recall"}

    principal = Principal(id="u9", roles=frozenset({"agent"}))
    # remember 使用 principal.id 作为 user_id
    result = await tools["remember"](principal, {"content": "我偏好用 Rust"})
    assert "mem:u9:" in result

    recalled = await tools["recall"](principal, {"query": "用户偏好什么语言"})
    assert "Rust" in recalled
