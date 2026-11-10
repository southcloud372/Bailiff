"""就绪探针：区分「进程活着」与「能接流量」。

### 为什么 /healthz 不够

K8s 的两类探针语义完全不同：

- **liveness（/healthz）**：进程是否还活着。失败 → 重启容器。只能检查**本地**状态，
  绝不能依赖数据库/模型 API——否则上游抖动会把整批 Pod 一起重启，变成级联故障。
- **readiness（/readyz）**：现在能不能接流量。失败 → 从 Service 摘除，但不重启。
  这里才该检查下游依赖。

很多项目把两者合成一个「什么都查」的探针，结果上游一挂，K8s 把全部副本重启，
故障从「部分不可用」放大成「全部不可用」。

### 本模块的取舍

- 探针并发执行（``asyncio.gather``），单个慢依赖不会串行拖长整体超时；
- 每个探针**自己吞异常**并转成 unhealthy，绝不让一个探针把 /readyz 打成 500；
- ``critical=False`` 的探针（如语义缓存）只报告状态，不改变整体判定——缓存挂了应该
  降级直连模型，而不是拒绝服务；
- 不调用付费模型接口：探针跑得比业务还贵就本末倒置了。模型可用性只查配置完整性。
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
