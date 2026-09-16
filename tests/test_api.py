from pathlib import Path

import httpx
import pytest

from agent_platform.api.app import create_app
from agent_platform.gateway.auth import encode_token
from agent_platform.gateway.config import Settings
from agent_platform.gateway.wiring import build_platform

SECRET = "test-secret-key-for-unit-tests-0123456789"


def _make_app(tmp_path: Path, **overrides: object) -> httpx.AsyncClient:
    settings = Settings(jwt_secret=SECRET, fs_root=tmp_path, **overrides)
    app = create_app(build_platform(settings))
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    )


def _token(roles: frozenset[str] = frozenset({"agent"}), principal_id: str = "u1") -> str:
    return encode_token(principal_id, roles, SECRET)


def _auth(roles: frozenset[str] = frozenset({"agent"})) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(roles)}"}


@pytest.mark.asyncio
async def test_healthz(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_missing_token_401(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        resp = await client.get("/v1/tools")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_invalid_token_401(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        resp = await client.get("/v1/tools", headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_tools(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        resp = await client.get("/v1/tools", headers=_auth())
        assert resp.status_code == 200
        assert set(resp.json()["tools"]) == {"read_file", "write_file", "list_dir"}


@pytest.mark.asyncio
async def test_call_write_and_read(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        headers = _auth()
        w = await client.post(
            "/v1/call",
            headers=headers,
            json={"tool": "write_file", "arguments": {"path": "a.txt", "content": "hi"}},
        )
        assert w.status_code == 200
        r = await client.post(
            "/v1/call",
            headers=headers,
            json={"tool": "read_file", "arguments": {"path": "a.txt"}},
        )
        assert r.status_code == 200
        assert r.json()["result"] == "hi"


@pytest.mark.asyncio
async def test_permission_denied_403(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        resp = await client.post(
            "/v1/call",
            headers=_auth(frozenset({"guest"})),
            json={"tool": "read_file", "arguments": {"path": "a.txt"}},
        )
        assert resp.status_code == 403


@pytest.mark.asyncio
async def test_sandbox_violation_400(tmp_path: Path) -> None:
    async with _make_app(tmp_path) as client:
        resp = await client.post(
            "/v1/call",
            headers=_auth(),
            json={"tool": "read_file", "arguments": {"path": "../../etc/passwd"}},
        )
        assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rate_limit_429(tmp_path: Path) -> None:
    async with _make_app(
        tmp_path, rate_limit_capacity=1, rate_limit_refill_per_second=0.0
    ) as client:
        headers = _auth()
        body = {"tool": "list_dir", "arguments": {}}
        r1 = await client.post("/v1/call", headers=headers, json=body)
        assert r1.status_code == 200
        r2 = await client.post("/v1/call", headers=headers, json=body)
        assert r2.status_code == 429


def test_mcp_filesystem_endpoint_mounted(tmp_path: Path) -> None:
    from starlette.routing import Mount

    app = create_app(build_platform(Settings(jwt_secret=SECRET, fs_root=tmp_path)))
    # MCP streamable HTTP 需要 lifespan 初始化 task group，单元测试不触达处理器，
    # 这里只验证挂载存在（真实行为由 scripts/smoke_api.py 跑 uvicorn 覆盖）。
    mounts = [r for r in app.routes if isinstance(r, Mount)]
    assert any(m.path == "/mcp/fs" for m in mounts)
