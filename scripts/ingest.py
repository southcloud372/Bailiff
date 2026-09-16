"""知识库灌库 CLI：把文档加载、切分、向量化后写入 pgvector。

用法：uv run python scripts/ingest.py [--docs-dir examples/knowledge]

前置：Docker 里已起 pgvector + Ollama，且已拉取嵌入模型（见 docs/docker-setup.md）。
"""

from __future__ import annotations

import argparse
import asyncio

from agent_platform.gateway.config import KnowledgeSettings
from agent_platform.knowledge.embedder import OllamaEmbedder
from agent_platform.knowledge.rag import RAGPipeline
from agent_platform.knowledge.store import PgVectorStore


async def _run(docs_dir: str) -> int:
    settings = KnowledgeSettings()
    embedder = OllamaEmbedder(
        base_url=settings.embedding_base_url, model=settings.embedding_model
    )
    store = PgVectorStore(settings.vector_dsn)
    rag = RAGPipeline(store, embedder)

    count = await rag.ingest(docs_dir)
    print(f"灌库完成：{count} 个片段已写入向量库（{settings.embedding_model}）")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="知识库灌库")
    parser.add_argument(
        "--docs-dir", default="examples/knowledge", help="知识库文档目录（.md/.txt）"
    )
    args = parser.parse_args()
    return asyncio.run(_run(args.docs_dir))


if __name__ == "__main__":
    raise SystemExit(main())
