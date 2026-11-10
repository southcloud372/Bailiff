"""运维端点测试：/readyz、/metrics、request-id 透传、语义缓存管理。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_platform.api.app import create_app
from agent_platform.gateway.auth import encode_token
from agent_platform.gateway.config import Settings
from agent_platform.gateway.policy import Principal
from agent_platform.gateway.wiring import build_platform
from agent_platform.observability.health import Readiness, static_probe
from agent_platform.router.cache import CacheStats

SECRET = "ops-test-secret-key-0123456789abcdef"


class _FakeAgent:
    """最小 Agent 替身：只实现 API 层依赖的三个方法。"""

    async def chat(self, message: str, *, thread_id: str, principal: Principal) -> str:
        return f"chat:{message}"

    async def ask(self, message: str, *, principal: Principal) -> str:
        return f"ask:{message}"

    def stream(
        self, message: str, *, thread_id: str, principal: Principal
    ) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            yield "hi"

        return _gen()


class _FakeCache:
    def __init__(self) -> None:
        self.cleared = False

    def stats(self) -> CacheStats:
        return CacheStats(hits=3, misses=1, entries=2, hit_rate=0.75)

    def clear(self) -> None:
        self.cleared = True


def _client(
    tmp_path: Path, **kwargs: Any
) -> httpx.AsyncClient:
    settings = Settings(jwt_secret=SECRET, fs_root=tmp_path)
    app = create_app(build_platform(settings), **kwargs)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    )


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {encode_token('u1', frozenset({'agent'}), SECRET)}"}


@pytest.mark.asyncio
async def test_readyz_ok(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/readyz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["ready"] is True
        assert {probe["name"] for probe in body["probes"]} >= {"gateway", "auth"}


@pytest.mark.asyncio
async def test_readyz_503_when_critical_dependency_down(tmp_path: Path) -> None:
    readiness = Readiness()
    readiness.add("postgres", static_probe(False, "connection refused"))
    async with _client(tmp_path, readiness=readiness) as client:
        resp = await client.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["ready"] is False


@pytest.mark.asyncio
async def test_healthz_does_not_touch_dependencies(tmp_path: Path) -> None:
    """存活探针即使下游全挂也必须 200，否则 K8s 会把整批 Pod 重启。"""
    readiness = Readiness()
    readiness.add("postgres", static_probe(False, "down"))
    async with _client(tmp_path, readiness=readiness) as client:
        resp = await client.get("/healthz")
        assert resp.status_code == 200


@pytest.mark.asyncio
async def test_metrics_exposes_prometheus_text(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        await client.get("/healthz")
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/plain")
        assert "# TYPE agent_http_requests_total counter" in resp.text
        assert "agent_http_requests_total" in resp.text


@pytest.mark.asyncio
async def test_request_id_is_generated_and_echoed(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/healthz")
        assert resp.headers.get("x-request-id")


@pytest.mark.asyncio
async def test_request_id_from_upstream_is_reused(tmp_path: Path) -> None:
    """跨服务关联：LB/上游传进来的 x-request-id 必须透传，不能重新生成。"""
    async with _client(tmp_path) as client:
        resp = await client.get("/healthz", headers={"x-request-id": "trace-abc-123"})
        assert resp.headers["x-request-id"] == "trace-abc-123"


@pytest.mark.asyncio
async def test_cache_endpoint_503_when_disabled(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.get("/v1/cache", headers=_auth())
        assert resp.status_code == 503


@pytest.mark.asyncio
async def test_cache_stats_and_clear(tmp_path: Path) -> None:
    cache = _FakeCache()
    async with _client(tmp_path, cache=cache) as client:
        stats = await client.get("/v1/cache", headers=_auth())
        assert stats.status_code == 200
        assert stats.json()["hit_rate"] == 0.75

        cleared = await client.delete("/v1/cache", headers=_auth())
        assert cleared.status_code == 200
        assert cache.cleared is True


@pytest.mark.asyncio
async def test_ask_uses_stateless_path(tmp_path: Path) -> None:
    async with _client(tmp_path, agent=_FakeAgent()) as client:
        resp = await client.post(
            "/v1/ask", headers=_auth(), json={"message": "年假怎么算"}
        )
        assert resp.status_code == 200
        assert resp.json()["reply"] == "ask:年假怎么算"


@pytest.mark.asyncio
async def test_ask_503_when_agent_missing(tmp_path: Path) -> None:
    async with _client(tmp_path) as client:
        resp = await client.post("/v1/ask", headers=_auth(), json={"message": "hi"})
        assert resp.status_code == 503


@pytest.mark.asyncio
async def test_weak_secret_blocks_readiness_only_outside_dev() -> None:
    """同一份代码：dev 下告警放行，prod 下拒绝上线。"""
    from agent_platform.api.app import default_readiness

    dev = build_platform(Settings(jwt_secret="dev-secret-change-me", env="dev"))
    assert (await default_readiness(dev).check()).ready is True

    prod = build_platform(Settings(jwt_secret="dev-secret-change-me", env="prod"))
    report = await default_readiness(prod).check()
    assert report.ready is False
    assert any("示例值" in probe.detail for probe in report.probes)


def test_dev_token_only_exists_in_dev() -> None:
    """演示登录端点按环境注册：prod 下根本不存在这条路由，而不是靠鉴权挡。"""
    dev = TestClient(create_app(build_platform(Settings(jwt_secret="x" * 40, env="dev"))))
    res = dev.post("/v1/auth/dev-token", json={"principal_id": "demo", "roles": ["agent"]})
    assert res.status_code == 200
    assert res.json()["token"]

    prod = TestClient(create_app(build_platform(Settings(jwt_secret="x" * 40, env="prod"))))
    assert prod.post("/v1/auth/dev-token", json={}).status_code == 404


def test_dev_token_grants_working_access() -> None:
    """拿到的 token 必须能真的调通受保护端点（否则演示流程是假的）。"""
    client = TestClient(create_app(build_platform(Settings(jwt_secret="x" * 40, env="dev"))))
    token = client.post("/v1/auth/dev-token", json={"principal_id": "demo"}).json()["token"]
    res = client.get("/v1/tools", headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 200
    assert res.json()["tools"]


def test_thread_key_namespaces_per_principal() -> None:
    """会话键必须带用户命名空间，否则猜到别人的 thread_id 就能读走历史（IDOR）。"""
    from agent_platform.api.app import _thread_key

    alice = Principal(id="alice", roles=frozenset({"agent"}))
    bob = Principal(id="bob", roles=frozenset({"agent"}))

    assert _thread_key(alice, "t1") != _thread_key(bob, "t1")
    assert _thread_key(alice, "t1") == "alice#t1"


def test_thread_key_is_idempotent() -> None:
    """同一个键再传回来必须原样返回，否则审批会落到空状态上。"""
    from agent_platform.api.app import _thread_key

    principal = Principal(id="alice", roles=frozenset({"agent"}))
    once = _thread_key(principal, "t1")
    assert _thread_key(principal, once) == once
    # 未指定会话时沿用 principal.id（升级后旧历史可读）
    assert _thread_key(principal, None) == "alice"
    assert _thread_key(principal, "alice") == "alice"


def test_dev_token_respects_requested_principal() -> None:
    """演示登录必须按请求签名，而不是永远签成默认身份。

    回归背景：该请求模型曾定义在 create_app 内部，配合延迟注解导致 FastAPI
    解析不到类型、静默忽略请求体，所有 token 都变成 demo——多身份演示直接失效。
    """
    client = TestClient(create_app(build_platform(Settings(jwt_secret="x" * 40, env="dev"))))
    res = client.post("/v1/auth/dev-token", json={"principal_id": "alice", "roles": ["guest"]})
    assert res.status_code == 200
    assert res.json()["principal_id"] == "alice"

    from agent_platform.gateway.auth import TokenVerifier

    principal = TokenVerifier("x" * 40).verify(res.json()["token"])
    assert principal.id == "alice"
    assert principal.roles == frozenset({"guest"})
