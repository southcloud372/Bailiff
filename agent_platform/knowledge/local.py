"""零依赖的本地检索实现：不联网也能用 RAG。

生产上向量检索应该是 pgvector 加 bge-m3 这类模型，但那样有两个前置条件：
起 Postgres、起嵌入服务。任何一个没到位，知识库就完全不可用，而使用者
拿到的报错是 ``502``，很难意识到只是没配好基础设施。

所以这里提供一套纯标准库的降级实现，用于本地开发、CI 和演示：

- :class:`HashingEmbedder` 用字符 n-gram 哈希成固定维度向量，能处理中文，
  不依赖任何模型；语义能力弱于神经嵌入，靠的是字面重叠。
- :class:`SqliteVectorStore` 把向量存进 SQLite，检索时暴力算余弦。
  几万条以内延迟可以接受，超过就该换 pgvector。

两者都实现与生产实现相同的接口，所以上层检索、重排、引用拼装的逻辑完全复用，
换后端只是换一行构造参数。
"""

from __future__ import annotations

import hashlib
import math
import sqlite3
import struct
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from agent_platform.knowledge.store import (
    ScoredRecord,
    VectorRecord,
    _matches,
    cosine_similarity,
)


def _tokens(text: str) -> list[str]:
    """切出用于哈希的 token：中文按字 + 双字，拉丁按词。

    不引分词器是刻意的。中文分词需要词典，模型要下载，会把「零依赖」重新打破；
    字级 n-gram 对中文检索已经够用（同义改写覆盖不到，但术语、配置名、错误码
    这类真正需要精确命中的内容效果不差）。
    """
    out: list[str] = []
    buf: list[str] = []
    for ch in text.lower():
        if ch.isascii() and (ch.isalnum() or ch in "_-."):
            buf.append(ch)
            continue
        if buf:
            out.append("".join(buf))
            buf.clear()
        if "\u4e00" <= ch <= "\u9fff":
            out.append(ch)
    if buf:
        out.append("".join(buf))
    # 中文双字补一遍，提升短语匹配权重
    cjk = [t for t in out if len(t) == 1 and "\u4e00" <= t <= "\u9fff"]
    out.extend(cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1))
    return out


class HashingEmbedder:
    """确定性的哈希嵌入，无网络、无模型、跨进程稳定。"""

    def __init__(self, dimensions: int = 512) -> None:
        self._dim = dimensions

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        counts: dict[str, int] = {}
        for token in _tokens(text):
            counts[token] = counts.get(token, 0) + 1
        for token, count in counts.items():
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            value = int.from_bytes(digest, "big")
            index = value % self._dim
            sign = 1.0 if (value >> 63) & 1 else -1.0
            vec[index] += sign * (1.0 + math.log(count))
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


class SqliteVectorStore:
    """SQLite 持久化的向量存储，检索走暴力余弦。"""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vectors (
                    id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    metadata TEXT NOT NULL DEFAULT '{}'
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path, check_same_thread=False)

    async def add(self, records: Sequence[VectorRecord]) -> None:
        import json

        rows = [
            (
                r.id,
                r.text,
                struct.pack(f"<{len(r.embedding)}f", *r.embedding),
                json.dumps(r.metadata, ensure_ascii=False),
            )
            for r in records
        ]
        with self._lock, self._connect() as conn:
            conn.executemany(
                "INSERT INTO vectors (id, text, embedding, metadata) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET text=excluded.text, "
                "embedding=excluded.embedding, metadata=excluded.metadata",
                rows,
            )

    async def search(
        self,
        embedding: list[float],
        *,
        top_k: int,
        filter: dict[str, Any] | None = None,
    ) -> list[ScoredRecord]:
        import json

        with self._lock, self._connect() as conn:
            rows = conn.execute("SELECT id, text, embedding, metadata FROM vectors").fetchall()

        scored: list[ScoredRecord] = []
        for row_id, text, blob, meta in rows:
            metadata = json.loads(meta)
            if filter and not _matches(metadata, filter):
                continue
            dim = len(blob) // 4
            vector = list(struct.unpack(f"<{dim}f", blob))
            scored.append(
                ScoredRecord(
                    id=str(row_id),
                    text=str(text),
                    metadata=metadata,
                    score=cosine_similarity(embedding, vector),
                )
            )
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    async def clear(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("DELETE FROM vectors")

    async def count(self) -> int:
        with self._lock, self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0])

    async def aclose(self) -> None:
        return None
