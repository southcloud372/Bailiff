"""健康检查：存活与就绪分开。

/healthz 只回答进程是否活着，不碰任何下游；/readyz 并发探测依赖，任一 critical
失败返回 503，让负载均衡摘流量而不是重启进程。

两者混用会把「某个依赖不可用」放大成「所有副本一起重启」。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

#: 探针函数：返回 (healthy, detail)；抛异常视为 unhealthy。
ProbeFn = Callable[[], Awaitable[tuple[bool, str]]]


@dataclass(slots=True)
class ProbeResult:
    """单个依赖的探测结果。"""

    name: str
    healthy: bool
    detail: str = ""
    latency_ms: float = 0.0
    critical: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "healthy": self.healthy,
            "critical": self.critical,
            "detail": self.detail,
            "latency_ms": round(self.latency_ms, 2),
        }


@dataclass(slots=True)
class _Probe:
    name: str
    fn: ProbeFn
    critical: bool = True


@dataclass(slots=True)
class ReadinessReport:
    """聚合结果：``ready`` 只看 critical 探针。"""

    ready: bool
    probes: list[ProbeResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "probes": [probe.as_dict() for probe in self.probes],
        }


class Readiness:
    """探针注册表：应用启动时登记依赖，``/readyz`` 时并发探测。"""

    def __init__(self, *, timeout_seconds: float = 3.0) -> None:
        self._probes: list[_Probe] = []
        self._timeout = timeout_seconds

    def add(self, name: str, fn: ProbeFn, *, critical: bool = True) -> Readiness:
        """登记一个探针。同名覆盖，便于测试替换。"""
        self._probes = [probe for probe in self._probes if probe.name != name]
        self._probes.append(_Probe(name=name, fn=fn, critical=critical))
        return self

    async def _run(self, probe: _Probe) -> ProbeResult:
        started = time.perf_counter()
        try:
            healthy, detail = await asyncio.wait_for(probe.fn(), timeout=self._timeout)
        except TimeoutError:
            healthy, detail = False, f"探针超时（>{self._timeout}s）"
        except Exception as exc:
            healthy, detail = False, f"{type(exc).__name__}: {exc}"
        return ProbeResult(
            name=probe.name,
            healthy=healthy,
            detail=detail,
            latency_ms=(time.perf_counter() - started) * 1000,
            critical=probe.critical,
        )

    async def check(self) -> ReadinessReport:
        """并发跑全部探针；任一 critical 不健康即判定未就绪。"""
        results = await asyncio.gather(*(self._run(probe) for probe in self._probes))
        ready = all(result.healthy for result in results if result.critical)
        return ReadinessReport(ready=ready, probes=list(results))

    @property
    def names(self) -> list[str]:
        return [probe.name for probe in self._probes]


# ── 常用探针工厂 ─────────────────────────────────────────────────────────────


def static_probe(healthy: bool, detail: str) -> ProbeFn:
    """固定结果的探针（配置项检查、未接入的可选依赖）。"""

    async def _probe() -> tuple[bool, str]:
        return healthy, detail

    return _probe


def callable_probe(fn: Callable[[], tuple[bool, str] | bool]) -> ProbeFn:
    """把同步判定函数包成探针（如「SQLite 文件可写」「API key 已配置」）。"""

    async def _probe() -> tuple[bool, str]:
        result = await asyncio.to_thread(fn)
        if isinstance(result, tuple):
            return result
        return result, ""

    return _probe
