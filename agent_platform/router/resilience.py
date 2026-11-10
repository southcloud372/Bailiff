"""重试与退避。

模型 API 的失败是常态：429 限流、502/503 网关抖动、长尾超时。直接抛给用户不可接受，
无脑重试又会打爆上游，所以这里用指数退避加随机抖动。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, TypeVar

from agent_platform.observability.metrics import CIRCUIT_STATE

logger = logging.getLogger(__name__)

T = TypeVar("T")

CircuitStatus = Literal["closed", "open", "half_open"]

_STATE_TO_GAUGE = {"closed": 0.0, "half_open": 1.0, "open": 2.0}


class CircuitOpenError(Exception):
    """熔断器处于打开状态，调用被快速失败。"""


class TransientError(Exception):
    """可重试的瞬时错误（上游 5xx / 超时 / 连接重置）。"""


# ── 重试 ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RetryPolicy:
    """指数退避重试策略。``max_attempts`` 含首次尝试。"""

    max_attempts: int = 3
    base_delay: float = 0.2
    max_delay: float = 2.0
    jitter: float = 0.1

    def delay_for(self, attempt: int) -> float:
        """第 ``attempt`` 次失败后的等待时间（attempt 从 1 开始）。"""
        raw = min(self.max_delay, self.base_delay * (2.0 ** (attempt - 1)))
        # 抖动：在 [raw*(1-jitter), raw*(1+jitter)] 内随机，避免同频重试。
        spread = raw * self.jitter
        return max(0.0, raw + random.uniform(-spread, spread))  # noqa: S311 - 重试抖动，非加密用途

    async def run(
        self,
        fn: Callable[[], Awaitable[T]],
        *,
        sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> T:
        """执行 ``fn`` 并在可重试异常上重试。最后一次失败原样抛出。"""
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return await fn()
            except CircuitOpenError:
                raise
            except Exception as exc:
                last = exc
                if attempt >= self.max_attempts:
                    break
                delay = self.delay_for(attempt)
                logger.warning(
                    "llm_retry",
                    extra={
                        "event": "llm_retry",
                        "attempt": attempt,
                        "delay_s": round(delay, 3),
                        "error": str(exc),
                    },
                )
                await sleeper(delay)
        assert last is not None
        raise last


# ── 熔断 ─────────────────────────────────────────────────────────────────────


class CircuitBreaker:
    """三态熔断器：closed → open → half_open → closed。

    只做**状态机**，不包装调用；由 :class:`ResilientCaller` 组合使用。
    """

    def __init__(
        self,
        target: str = "llm",
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.target = target
        self._threshold = max(1, failure_threshold)
        self._recovery = recovery_timeout
        self._clock = clock
        self._status: CircuitStatus = "closed"
        self._failures = 0
        self._opened_at = 0.0

    @property
    def status(self) -> CircuitStatus:
        return self._status

    def allow(self) -> bool:
        """是否放行本次调用（open 且未到恢复时间 → 拒绝）。"""
        if self._status == "open":
            if self._clock() - self._opened_at >= self._recovery:
                self._transition("half_open")
                return True
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._transition("closed")

    def record_failure(self) -> None:
        self._failures += 1
        if self._status == "half_open" or self._failures >= self._threshold:
            self._opened_at = self._clock()
            self._transition("open")

    def _transition(self, status: CircuitStatus) -> None:
        if status == self._status:
            return
        logger.warning(
            "circuit_state_change",
            extra={"event": "circuit_state", "target": self.target,
                   "from": self._status, "to": status},
        )
        self._status = status
        CIRCUIT_STATE.set(_STATE_TO_GAUGE[status], target=self.target)


# ── 组合 ─────────────────────────────────────────────────────────────────────


class ResilientCaller:
    """把「熔断 + 超时 + 重试」组合成一个可复用的调用器。"""

    def __init__(
        self,
        *,
        target: str = "llm",
        timeout: float = 60.0,
        retry: RetryPolicy | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.timeout = timeout
        self.retry = retry or RetryPolicy()
        self.breaker = breaker or CircuitBreaker(target)

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """执行一次受保护的调用：熔断检查 → 超时 → 重试。"""

        async def attempt() -> T:
            if not self.breaker.allow():
                raise CircuitOpenError(
                    f"熔断器打开（target={self.breaker.target}），快速失败"
                )
            try:
                result = await asyncio.wait_for(fn(), timeout=self.timeout)
            except Exception:
                self.breaker.record_failure()
                raise
            self.breaker.record_success()
            return result

        return await self.retry.run(attempt)
