"""网关限流：令牌桶。

治理四件套的第二环（限流）。默认内存实现（每 key 一个桶），接口可替换为 Redis 后端。
时钟可注入，便于确定性测试。
"""

from __future__ import annotations

import time
from collections.abc import Callable


class RateLimiter:
    """令牌桶限流器：允许突发，超过容量后按固定速率补充。"""

    def __init__(
        self,
        capacity: int = 100,
        refill_per_second: float = 10.0,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = float(capacity)
        self._refill = refill_per_second
        self._clock = clock
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last)

    def allow(self, key: str) -> bool:
        now = self._clock()
        tokens, last = self._buckets.get(key, (self._capacity, now))
        tokens = min(self._capacity, tokens + (now - last) * self._refill)
        if tokens >= 1.0:
            self._buckets[key] = (tokens - 1.0, now)
            return True
        self._buckets[key] = (tokens, now)
        return False
