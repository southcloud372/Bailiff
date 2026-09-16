"""知识库 / 长期记忆基础设施冒烟：真实 pgvector + Ollama 嵌入，不依赖模型 key。

用法：uv run python scripts/smoke_knowledge.py
前置：docker compose up -d && docker exec -it agent-ollama ollama pull bge-m3
      && uv run python scripts/ingest.py --docs-dir examples/knowledge
"""

from __future__ import annotations

import asyncio

from agent_platform.gateway.config import KnowledgeSettings
from agent_platform.knowledge.embedder import OllamaEmbedder
from agent_platform.knowledge.rag import RAGPipeline
from agent_platform.knowledge.store import PgVectorStore
from agent_platform.memory.service import MemoryService


async def main() -> int:
    settings = KnowledgeSettings()
    embedder = OllamaEmbedder(
        base_url=settings.embedding_base_url, model=settings.embedding_model
    )
    store = PgVectorStore(settings.vector_dsn)
    rag = RAGPipeline(store, embedder)
    memory = MemoryService(store, embedder)

    print("知识检索（请假流程）：")
    hits = await rag.search("请假需要什么流程")
    print(rag.format_context(hits))

    await memory.remember("演示用户偏好 Python 后端方向", user_id="smoke")
    print("\n记忆召回（用户偏好）：")
    recalled = await memory.recall("用户偏好什么方向", user_id="smoke")
    print(rag.format_context(recalled))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
