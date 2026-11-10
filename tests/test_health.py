"""就绪探针测试：重点验证「探针自身永不抛异常」与 critical/non-critical 语义。"""

from __future__ import annotations

import asyncio

import pytest

from agent_platform.observability.health import (
    Readiness,
    callable_probe,
    static_probe,
)


@pytest.mark.asyncio
async def test_all_healthy_is_ready() -> None:
    readiness = Readiness()
    readiness.add("db", static_probe(True, "ok"))
    readiness.add("cache", static_probe(True, "ok"), critical=False)
    report = await readiness.check()
    assert report.ready is True
    assert [probe.name for probe in report.probes] == ["db", "cache"]


@pytest.mark.asyncio
async def test_critical_failure_blocks_readiness() -> None:
    readiness = Readiness()
    readiness.add("db", static_probe(False, "down"))
    report = await readiness.check()
    assert report.ready is False
    assert report.as_dict()["probes"][0]["detail"] == "down"


@pytest.mark.asyncio
async def test_optional_dependency_does_not_block() -> None:
    """缓存挂了应该降级直连模型，而不是拒绝全部流量。"""
    readiness = Readiness()
    readiness.add("gateway", static_probe(True, "ok"))
    readiness.add("cache", static_probe(False, "redis down"), critical=False)
    report = await readiness.check()
    assert report.ready is True


@pytest.mark.asyncio
async def test_exception_becomes_unhealthy_not_crash() -> None:
    async def boom() -> tuple[bool, str]:
        raise RuntimeError("connection reset")

    readiness = Readiness()
    readiness.add("flaky", boom)
    report = await readiness.check()
    assert report.ready is False
    assert "connection reset" in report.probes[0].detail


@pytest.mark.asyncio
async def test_slow_probe_times_out() -> None:
    async def slow() -> tuple[bool, str]:
        await asyncio.sleep(1.0)
        return True, "late"

    readiness = Readiness(timeout_seconds=0.05)
    readiness.add("slow", slow)
    report = await readiness.check()
    assert report.ready is False
    assert "超时" in report.probes[0].detail


@pytest.mark.asyncio
async def test_probes_run_concurrently() -> None:
    """三个各 0.1s 的探针并发执行，总耗时应远小于串行的 0.3s。"""

    async def slow() -> tuple[bool, str]:
        await asyncio.sleep(0.1)
        return True, "ok"

    readiness = Readiness()
    for name in ("a", "b", "c"):
        readiness.add(name, slow)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await readiness.check()
    assert loop.time() - started < 0.25


@pytest.mark.asyncio
async def test_callable_probe_wraps_sync_function() -> None:
    readiness = Readiness()
    readiness.add("sync", callable_probe(lambda: (True, "fine")))
    report = await readiness.check()
    assert report.ready is True
    assert report.probes[0].detail == "fine"
