"""按配置装配检索栈（store + embedder）。

服务和灌库脚本共用这里，避免两边各自判断后端，出现「灌进去了但服务连的是另一个库」
这类最难查的问题。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_platform.gateway.config import KnowledgeSettings
from agent_platform.knowledge.embedder import Embedder, OllamaEmbedder
from agent_platform.knowledge.local import HashingEmbedder, SqliteVectorStore


@dataclass(frozen=True)
class KnowledgeStack:
    backend: str
    store: Any
    embedder: Embedder

    @property
    def is_local(self) -> bool:
        return self.backend == "local"


def build_knowledge_stack(settings: KnowledgeSettings) -> KnowledgeStack:
    """``postgres`` 走 pgvector + Ollama 嵌入；其余一律走零依赖本地实现。"""
    if settings.rag_backend == "postgres":
        from agent_platform.knowledge.store import PgVectorStore

        return KnowledgeStack(
            backend="postgres",
            store=PgVectorStore(
                settings.vector_dsn, dimensions=settings.embedding_dimensions
            ),
            embedder=OllamaEmbedder(
                base_url=settings.embedding_base_url, model=settings.embedding_model
            ),
        )
    return KnowledgeStack(
        backend="local",
        store=SqliteVectorStore(settings.local_db_path),
        embedder=HashingEmbedder(settings.local_embedding_dimensions),
    )
