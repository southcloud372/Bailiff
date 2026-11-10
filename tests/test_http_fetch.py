"""http_fetch 的安全测试。

这个工具是攻击面最集中的地方，测试重点不是「能抓到网页」，而是**抓不到不该抓的东西**：

- 白名单默认拒绝；
- 解析到内网/环回/云元数据地址必须拒绝（169.254.169.254 是最高危的一个）；
- 重定向到未授权域名必须拒绝（否则白名单形同虚设）；
- 超大响应必须截断（否则一个 URL 就能炸掉上下文与账单）。
"""

from __future__ import annotations

import socket

import httpx
import pytest

from agent_platform.tools.web import (
    FetchDenied,
    FetchPolicy,
    fetch_url,
    parse_hosts,
)


def _policy(**kw: object) -> FetchPolicy:
    return FetchPolicy(allowed_hosts=frozenset({"example.com"}), **kw)  # type: ignore[arg-type]


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ── 白名单 ────────────────────────────────────────────────────────────────
def test_parse_hosts_tolerates_spaces_and_wildcards() -> None:
    assert parse_hosts(" a.com , *.b.com,, ") == frozenset({"a.com", "*.b.com"})


def test_wildcard_matches_one_level_suffix() -> None:
    policy = FetchPolicy(allowed_hosts=frozenset({"*.python.org"}))
    assert policy.allows("docs.python.org")
    assert not policy.allows("python.org")
    assert not policy.allows("evilpython.org")


@pytest.mark.asyncio
async def test_empty_allowlist_denies_everything() -> None:
    with pytest.raises(FetchDenied, match="未配置"):
        await fetch_url("https://example.com", policy=FetchPolicy())


@pytest.mark.asyncio
async def test_host_outside_allowlist_is_denied() -> None:
    with pytest.raises(FetchDenied, match="白名单"):
        await fetch_url("https://evil.test/x", policy=_policy())


@pytest.mark.asyncio
async def test_non_http_scheme_is_denied() -> None:
    with pytest.raises(FetchDenied, match="协议"):
        await fetch_url("file:///etc/passwd", policy=_policy())


# ── SSRF：DNS 解析后的地址校验 ────────────────────────────────────────────
def _fake_dns(ip: str):
    def _resolver(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    return _resolver


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.0.0.5", "192.168.1.10", "172.16.0.1", "169.254.169.254"],
)
async def test_private_and_metadata_addresses_are_denied(
    monkeypatch: pytest.MonkeyPatch, ip: str
) -> None:
    """域名白名单挡不住「白名单域名解析到内网」，必须再校验解析结果。"""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns(ip))
    with pytest.raises(FetchDenied, match="不是公网"):
        await fetch_url("https://example.com/meta", policy=_policy())


@pytest.mark.asyncio
async def test_unresolvable_host_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*args, **kwargs):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", _fail)
    with pytest.raises(FetchDenied, match="无法解析"):
        await fetch_url("https://example.com", policy=_policy())


# ── 抓取行为（DNS 与网络都被替身接管）─────────────────────────────────────
def _allow_public(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", _fake_dns("93.184.216.34"))


@pytest.mark.asyncio
async def test_successful_fetch_returns_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_public(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "text/plain; charset=utf-8"}, text="hello"
        )

    result = await fetch_url(
        "https://example.com/a", policy=_policy(), transport=_transport(handler)
    )
    assert result["status"] == 200
    assert result["text"] == "hello"
    assert result["truncated"] is False


@pytest.mark.asyncio
async def test_oversized_body_is_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """响应炸弹防护：必须按字节上限截断，而不是先读进内存再判断。"""
    _allow_public(monkeypatch)
    big = "x" * (300 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/plain"}, text=big)

    result = await fetch_url(
        "https://example.com/big",
        policy=_policy(max_bytes=1024),
        transport=_transport(handler),
    )
    assert result["truncated"] is True
    assert len(result["text"]) <= 1024


@pytest.mark.asyncio
async def test_redirect_to_unlisted_host_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    """重定向必须逐跳复检，否则白名单可以被一个 302 绕过。"""
    _allow_public(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.test/steal"})

    with pytest.raises(FetchDenied, match="白名单"):
        await fetch_url(
            "https://example.com/go", policy=_policy(), transport=_transport(handler)
        )


@pytest.mark.asyncio
async def test_binary_content_type_is_denied(monkeypatch: pytest.MonkeyPatch) -> None:
    _allow_public(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/octet-stream"})

    with pytest.raises(FetchDenied, match="内容类型"):
        await fetch_url(
            "https://example.com/bin", policy=_policy(), transport=_transport(handler)
        )
