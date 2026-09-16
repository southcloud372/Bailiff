"""长期记忆服务：按用户分域的写入与召回。

- ``remember``：把一条文本写入长期记忆（带 user_id / session_id 元数据）；
- ``recall``：按语义召回当前用户的记忆。

分域通过向量库的 metadata 过滤实现（``user_id`` 精确匹配），底层复用
knowledge 的向量存储与嵌入，避免为记忆单独维护一套基础设施。
"""

from __future__ import annotations

import uuid

from agent_platform.knowledge.embedder import Embedder
from agent_platform.knowledge.retriever import retrieve
from agent_platform.knowledge.store import ScoredRecord, VectorRecord, VectorStore

KIND = "memory"


class MemoryService:
    """向量化长期记忆。"""

    def __init__(self, store: VectorStore, embedder: Embedder, *, top_k: int = 4) -> None:
        self._store = store
        self._embedder = embedder
        self._top_k = top_k

    async def remember(
        self, text: str, *, user_id: str, session_id: str = ""
    ) -> str:
        """写入一条长期记忆，返回记忆 id。"""
        record_id = f"mem:{user_id}:{uuid.uuid4().hex[:12]}"
        embedding = (await self._embedder.embed([text]))[0]
        await self._store.add(
            [
                VectorRecord(
                    id=record_id,
                    text=text,
                    embedding=embedding,
                    metadata={"kind": KIND, "user_id": user_id, "session_id": session_id},
                )
            ]
        )
        return record_id

    async def recall(
        self, query: str, *, user_id: str, top_k: int | None = None
    ) -> list[ScoredRecord]:
        """按语义召回当前用户的记忆。"""
        return await retrieve(
            self._store,
            self._embedder,
            query,
            top_k=top_k or self._top_k,
            filter={"kind": KIND, "user_id": user_id},
        )
