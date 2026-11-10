"""语义缓存：把「问过的问题」在向量空间里命中，省掉一次模型调用。

### 为什么是语义缓存而不是「精确字符串缓存」

真实用户不会两次输入一模一样的话：*「年假怎么算」* 与 *「我想问下年假的计算方式」*
是一回事。精确缓存命中率通常只有个位数；语义缓存按 embedding 相近度命中，能把
重复问题的占比（企业知识助手场景常在 30%+）真正变成省下来的钱与延迟。

### 成本口径（面试可讲）

- 一次 embedding（bge-m3 本地）= 毫秒级、零边际费用；
- 一次 LLM 生成 = 秒级、按 token 计费；
- 所以「多算一次 embedding 来避免一次 LLM 调用」在任何时候都是赚的——**前提是阈值调对**：
  阈值太低会命中不相干的问题（错误回答比慢回答更糟），太高则命中率趋近精确缓存。
  生产上应该在 golden dataset 上扫阈值，用「命中率 × 正确率」选点，而不是拍脑袋 0.9。
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from threading import Lock

from pydantic import BaseModel

from agent_platform.knowledge.embedder import Embedder
from agent_platform.knowledge.store import cosine_similarity
from agent_platform.observability.metrics import CACHE_EVENTS


class CachedResponse(BaseModel):
    """缓存里存的东西：回答本体 + 产生它的模型 + 用量（便于还原成本口径）。"""

    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0


class CacheStats(BaseModel):
    """缓存统计（供 ``/v1/cache`` 与指标使用）。"""

    hits: int = 0
    misses: int = 0
    entries: int = 0
    hit_rate: float = 0.0


class SemanticCache:
    """进程内语义缓存（生产可换 Redis/RedisVL，接口不变）。"""

    def __init__(
        self,
        embedder: Embedder,
        *,
        threshold: float = 0.95,
        ttl_seconds: float = 3600.0,
        max_entries: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._embedder = embedder
        self._threshold = threshold
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: list[tuple[list[float], CachedResponse, float]] = []
        self._hits = 0
        self._misses = 0
        self._lock = Lock()

    async def get(self, query: str) -> CachedResponse | None:
        """按语义命中缓存；命中返回副本，过期条目顺带清理。"""
        embedding = (await self._embedder.embed([query]))[0]
        now = self._clock()
        with self._lock:
            self._entries = [
                entry for entry in self._entries if now - entry[2] < self._ttl
            ]
            best: tuple[float, CachedResponse] | None = None
            for cached_embedding, response, _created in self._entries:
                score = cosine_similarity(embedding, cached_embedding)
                if score >= self._threshold and (best is None or score > best[0]):
                    best = (score, response)
            if best is None:
                self._misses += 1
            else:
                self._hits += 1
        if best is None:
            CACHE_EVENTS.inc(result="miss")
            return None
        CACHE_EVENTS.inc(result="hit")
        return best[1].model_copy()

    async def set(self, query: str, response: CachedResponse) -> None:
        """写入缓存；超容量时按「最旧优先」淘汰（LRU 的近似，够用且无额外依赖）。"""
        embedding = (await self._embedder.embed([query]))[0]
        now = self._clock()
        with self._lock:
            self._entries.append((embedding, response, now))
            if len(self._entries) > self._max_entries:
                self._entries.sort(key=lambda entry: entry[2])
                self._entries = self._entries[-self._max_entries :]

    def stats(self) -> CacheStats:
        total = self._hits + self._misses
        return CacheStats(
            hits=self._hits,
            misses=self._misses,
            entries=len(self._entries),
            hit_rate=round(self._hits / total, 4) if total else 0.0,
        )

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class NullCache:
    """关闭缓存时的空实现（避免调用方到处判空）。"""

    async def get(self, query: str) -> CachedResponse | None:
        CACHE_EVENTS.inc(result="bypass")
        return None

    async def set(self, query: str, response: CachedResponse) -> None:
        return None

    def stats(self) -> CacheStats:
        return CacheStats()

    def clear(self) -> None:
        return None


def normalize_threshold(value: float) -> float:
    """把阈值夹到 (0.5, 1) 区间，防止配置成 1.5 这类无意义值。"""
    if math.isnan(value):
        return 0.95
    return min(0.999, max(0.5, value))
