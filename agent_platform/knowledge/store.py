"""向量存储：语义检索的落盘层。

提供 ``VectorStore`` 协议与两个实现：
- ``InMemoryVectorStore``：进程内实现（余弦相似度），供单元测试；
- ``PgVectorStore``：PostgreSQL + pgvector（数据层），生产默认。

表结构单一，用 ``metadata``（JSONB）区分知识/记忆并按用户分域，
``metadata @> %s`` 做等值过滤（含子集匹配）。
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Protocol

from pydantic import BaseModel, Field

_TABLE = "embeddings"


class VectorRecord(BaseModel):
    """一条待写入的向量记录。"""

    id: str
    text: str
    embedding: list[float]
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScoredRecord(BaseModel):
    """一条检索命中（含相似度分）。"""

    id: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    score: float


class VectorStore(Protocol):
    """向量存储的最小接口。"""

    async def add(self, records: Sequence[VectorRecord]) -> None: ...

    async def search(
        self,
        embedding: list[float],
        *,
        top_k: int,
        filter: dict[str, Any] | None = None,
    ) -> list[ScoredRecord]: ...

    async def clear(self) -> None: ...


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _matches(metadata: dict[str, Any], filter: dict[str, Any]) -> bool:
    return all(metadata.get(k) == v for k, v in filter.items())


class InMemoryVectorStore:
    """进程内向量存储：测试与降级。"""

    def __init__(self) -> None:
        self._records: list[VectorRecord] = []

    async def add(self, records: Sequence[VectorRecord]) -> None:
        self._records.extend(records)

    async def search(
        self,
        embedding: list[float],
        *,
        top_k: int,
        filter: dict[str, Any] | None = None,
    ) -> list[ScoredRecord]:
        hits = [
            ScoredRecord(
                id=r.id,
                text=r.text,
                metadata=r.metadata,
                score=_cosine(embedding, r.embedding),
            )
            for r in self._records
            if filter is None or _matches(r.metadata, filter)
        ]
        hits.sort(key=lambda s: s.score, reverse=True)
        return hits[:top_k]

    async def clear(self) -> None:
        self._records.clear()


class PgVectorStore:
    """PostgreSQL + pgvector 存储。连接惰性建立，失败时异常上抛（由调用方处理）。"""

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._conn: Any = None

    async def _connection(self) -> Any:
        if self._conn is None or self._conn.closed:
            import psycopg
            from pgvector.psycopg import register_vector_async

            self._conn = await psycopg.AsyncConnection.connect(self._dsn, autocommit=True)
            await register_vector_async(self._conn)
            await self._conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await self._conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    embedding vector NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb
                )
                """
            )
        return self._conn

    async def add(self, records: Sequence[VectorRecord]) -> None:
        conn = await self._connection()
        from psycopg.types.json import Jsonb

        async with conn.cursor() as cur:
            for r in records:
                await cur.execute(
                    f"INSERT INTO {_TABLE} (id, text, embedding, metadata) "
                    "VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text, "
                    "embedding = EXCLUDED.embedding, metadata = EXCLUDED.metadata",
                    (
                        r.id,
                        r.text,
                        r.embedding,
                        Jsonb(r.metadata),
                    ),
                )

    async def search(
        self,
        embedding: list[float],
        *,
        top_k: int,
        filter: dict[str, Any] | None = None,
    ) -> list[ScoredRecord]:
        from psycopg.types.json import Jsonb

        conn = await self._connection()
        async with conn.cursor() as cur:
            if filter:
                await cur.execute(
                    f"SELECT id, text, metadata, 1 - (embedding <=> %s) AS score "
                    f"FROM {_TABLE} WHERE metadata @> %s "
                    f"ORDER BY embedding <=> %s LIMIT %s",
                    (
                        embedding,
                        Jsonb(filter),
                        embedding,
                        top_k,
                    ),
                )
            else:
                await cur.execute(
                    f"SELECT id, text, metadata, 1 - (embedding <=> %s) AS score "
                    f"FROM {_TABLE} ORDER BY embedding <=> %s LIMIT %s",
                    (embedding, embedding, top_k),
                )
            rows = await cur.fetchall()
        return [
            ScoredRecord(
                id=str(row[0]),
                text=str(row[1]),
                metadata=dict(row[2]),
                score=float(row[3]),
            )
            for row in rows
        ]

    async def clear(self) -> None:
        conn = await self._connection()
        await conn.execute(f"DELETE FROM {_TABLE}")
