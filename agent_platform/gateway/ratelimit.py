"""网关限流：令牌桶（单机）+ Redis 滑动窗口（多副本）。

治理四件套的第二环（限流）。为什么需要两种实现？

- 进程内令牌桶的计数存在**本进程内存**里。单副本演示没问题，但一旦水平扩容到 N 个
  副本，配额就被放大 N 倍，用户在 A 副本打满，请求被 LB 转到 B 副本，B 认为他一个
  请求都没发过。限流形同虚设。
- Redis 滑动窗口把计数放到共享存储，Lua 脚本保证「清理 + 计数 + 写入」原子执行，
  多副本之间才有一致的配额。

**故障策略**：Redis 不可用时**降级到本地令牌桶**而不是直接放行（fail-open）。
放行等于关掉治理；降级到本地至少保住单副本的防雪崩能力，同时打 warning 让运维介入。
这是可用性与安全性的取舍。
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: 滑动窗口：清理过期成员 → 统计当前窗口内请求数 → 未超限则写入本次请求。
#: KEYS[1]=桶名 ARGV: now_ms, window_ms, limit, member
_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local count = redis.call('ZCARD', key)
if count < limit then
  redis.call('ZADD', key, now, ARGV[4])
  redis.call('PEXPIRE', key, window)
  return 1
end
return 0
"""


class Limiter(Protocol):
    """限流器协议：网关只依赖这一个方法。"""

    async def aallow(self, key: str) -> bool: ...


class RateLimiter:
    """进程内令牌桶：允许突发，超过容量后按固定速率补充。"""

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

    async def aallow(self, key: str) -> bool:
        """异步门面：让网关用同一套 await 调用两种后端。"""
        return self.allow(key)

    def reset(self) -> None:
        """清空所有桶（测试与运维用）。"""
        self._buckets.clear()


class RedisRateLimiter:
    """Redis 滑动窗口限流：跨副本一致配额。

    ``capacity`` 表示窗口内允许的请求数，``window_seconds`` 是窗口长度。与令牌桶相比，
    滑动窗口没有「窗口边界双倍流量」问题（固定窗口的经典缺陷），代价是每个请求多一次
    Redis 往返（<1ms，局域网内可接受）。
    """

    def __init__(
        self,
        client: Any,
        *,
        capacity: int = 100,
        window_seconds: float = 10.0,
        prefix: str = "agent:rl",
        fallback: RateLimiter | None = None,
    ) -> None:
        self._client = client
        self._capacity = int(capacity)
        self._window_ms = int(window_seconds * 1000)
        self._prefix = prefix
        self._fallback = fallback or RateLimiter(capacity=capacity)
        self._script: Any | None = None

    async def _ensure_script(self) -> Any:
        if self._script is None:
            # register_script 只做本地注册，首次 EVALSHA 失败会自动回退 EVAL。
            self._script = self._client.register_script(_SLIDING_WINDOW_LUA)
        return self._script

    async def aallow(self, key: str) -> bool:
        now_ms = int(time.time() * 1000)
        member = f"{now_ms}-{uuid.uuid4().hex[:8]}"
        try:
            script = await self._ensure_script()
            allowed = await script(
                keys=[f"{self._prefix}:{key}"],
                args=[now_ms, self._window_ms, self._capacity, member],
            )
            return bool(int(allowed))
        except Exception as exc:
            logger.warning(
                "rate_limiter_degraded",
                extra={"event": "ratelimit_degraded", "error": str(exc)},
            )
            return await self._fallback.aallow(key)

    async def aclose(self) -> None:
        closer = getattr(self._client, "aclose", None)
        if callable(closer):
            await closer()


def build_rate_limiter(
    backend: str = "memory",
    *,
    capacity: int = 100,
    refill_per_second: float = 10.0,
    redis_url: str = "redis://localhost:6379/0",
    window_seconds: float = 10.0,
) -> Limiter:
    """按配置构造限流器。``redis`` 后端缺依赖时报出可执行的修复命令。"""
    if backend == "memory":
        return RateLimiter(capacity=capacity, refill_per_second=refill_per_second)
    if backend == "redis":
        try:
            from redis.asyncio import Redis
        except ImportError as exc:  # pragma: no cover - 取决于可选依赖
            raise RuntimeError(
                "redis 限流后端需要可选依赖：uv sync --extra prod"
            ) from exc
        client = Redis.from_url(redis_url, decode_responses=True)
        return RedisRateLimiter(
            client,
            capacity=capacity,
            window_seconds=window_seconds,
            fallback=RateLimiter(capacity=capacity, refill_per_second=refill_per_second),
        )
    raise ValueError(f"不支持的限流后端: {backend}")
