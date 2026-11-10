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


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """公开的余弦相似度（语义缓存、去重、聚类等场景复用同一口径）。"""
    return _cosine(a, b)


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
    """PostgreSQL + pgvector 存储（连接池 + HNSW 索引）。

    ### 为什么必须用连接池

    `psycopg.AsyncConnection` **不是并发安全的**：两条协程同时在一个连接上执行，
    第二条会抛 ``another command is already in progress``。FastAPI 的请求是并发的，
    单个共享连接在演示时无事，一压测就炸。连接池让每个并发请求拿到独立连接。

    ### 为什么要显式建 HNSW 索引

    pgvector 的默认行为是**顺序扫描 + 逐行算距离**。几百条数据看不出差别，几万条
    片段就是几百毫秒到几秒。``USING hnsw (embedding vector_cosine_ops)`` 把检索从
    O(n) 降到近似 O(log n)，这是 RAG 从「能用」到「能用」的分水岭。

    维度必须显式声明（``vector(1024)``），pgvector 不给无维度列建 ANN 索引。
    """

    def __init__(
        self,
        dsn: str,
        *,
        dimensions: int = 1024,
        pool_min_size: int = 1,
        pool_max_size: int = 8,
        ef_search: int = 100,
    ) -> None:
        self._dsn = dsn
        self._dimensions = dimensions
        self._pool_min = pool_min_size
        self._pool_max = pool_max_size
        self._ef_search = ef_search
        self._pool: Any = None
        self._schema_ready = False

    async def _connection_pool(self) -> Any:
        if self._pool is None:
            try:
                from psycopg_pool import AsyncConnectionPool
            except ImportError as exc:  # pragma: no cover - 取决于可选依赖
                raise RuntimeError(
                    "PgVectorStore 需要连接池：uv sync --extra prod（psycopg[pool]）"
                ) from exc

            from pgvector.psycopg import register_vector_async

            async def _configure(conn: Any) -> None:
                # 每个新连接都要注册 vector 类型，否则参数绑定会把 list 当数组。
                await register_vector_async(conn)
                # 查询侧召回参数：越大越准越慢。100 是「召回优先」的稳妥起点。
                await conn.execute(f"SET hnsw.ef_search = {int(self._ef_search)}")

            pool = AsyncConnectionPool(
                self._dsn,
                min_size=self._pool_min,
                max_size=self._pool_max,
                open=False,
                kwargs={"autocommit": True},
                configure=_configure,
            )
            await pool.open(wait=True, timeout=10)
            self._pool = pool
        await self._ensure_schema(self._pool)
        return self._pool

    async def _ensure_schema(self, pool: Any) -> None:
        """建表 + 索引（幂等）。只在进程内做一次。"""
        if self._schema_ready:
            return
        async with pool.connection() as conn:
            await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_TABLE} (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    embedding vector({self._dimensions}) NOT NULL,
                    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb
                )
                """
            )
            # 过滤很快（metadata @> ...），但「先过滤再排序」在 pgvector 0.8 之前会
            # 退化成全表扫；HNSW + 迭代扫描才是正解。m/ef_construction 是官方起点值。
            await conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {_TABLE}_embedding_hnsw
                ON {_TABLE} USING hnsw (embedding vector_cosine_ops)
                WITH (m = 16, ef_construction = 64)
                """
            )
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS {_TABLE}_metadata_gin "
                f"ON {_TABLE} USING gin (metadata jsonb_path_ops)"
            )
        self._schema_ready = True

    async def _connection(self) -> Any:
        """兼容旧调用点：返回池本身（用 ``async with`` 取连接）。"""
        return await self._connection_pool()

    async def add(self, records: Sequence[VectorRecord]) -> None:
        pool = await self._connection_pool()
        from psycopg.types.json import Jsonb

        async with pool.connection() as conn, conn.cursor() as cur:
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
        pool = await self._connection_pool()
        from psycopg.types.json import Jsonb

        async with pool.connection() as conn, conn.cursor() as cur:
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
        pool = await self._connection_pool()
        async with pool.connection() as conn:
            await conn.execute(f"DELETE FROM {_TABLE}")

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._schema_ready = False
