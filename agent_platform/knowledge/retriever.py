"""检索：语义检索 + 词面重排（混合检索的轻量实现）。

先用向量相似度召回候选，再用「字符级词面重叠」做微调重排，纯语义检索对中文
术语/编号容易失准，词面信号能拉回「精确命中」的片段。字符级重叠对中英文都适用。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agent_platform.knowledge.embedder import Embedder
from agent_platform.knowledge.store import ScoredRecord, VectorStore


def _lexical_overlap(query: str, text: str) -> float:
    q_chars = set(query.lower())
    t_chars = set(text.lower())
    if not q_chars:
        return 0.0
    return len(q_chars & t_chars) / len(q_chars)


def rerank(
    hits: Sequence[ScoredRecord],
    query: str,
    *,
    lexical_weight: float = 0.3,
) -> list[ScoredRecord]:
    """语义分 + 词面加权，重新排序（不改变原列表）。"""
    scored: list[tuple[float, ScoredRecord]] = []
    for hit in hits:
        new_score = hit.score + lexical_weight * _lexical_overlap(query, hit.text)
        scored.append((new_score, hit))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [hit for _, hit in scored]


async def retrieve(
    store: VectorStore,
    embedder: Embedder,
    query: str,
    *,
    top_k: int = 4,
    filter: dict[str, Any] | None = None,
) -> list[ScoredRecord]:
    """嵌入查询 → 向量检索 → 重排。召回量翻倍再重排后截断，给重排留空间。"""
    query_vec = (await embedder.embed([query]))[0]
    candidates = await store.search(query_vec, top_k=max(top_k * 2, top_k), filter=filter)
    return rerank(candidates, query)[:top_k]
