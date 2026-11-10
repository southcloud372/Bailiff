"""HTTP 抓取工具，带 SSRF 防护。

直接给 Agent 一个 requests.get 会带来三类问题：读到云主机的元数据服务
（169.254.169.254，可拿到临时凭据）、被 302 跳到内网地址、超大响应把上下文和账单
一起撑爆。

对应的防护是：域名白名单默认拒绝、DNS 解析后校验 IP 是否公网、重定向逐跳复检、
流式读取并限制字节数。

已知局限：校验与实际连接是两次 DNS 解析，短 TTL 下存在 rebinding 窗口。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from agent_platform.gateway.policy import Principal
from agent_platform.gateway.registry import ToolFn

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_ALLOWED_CONTENT_PREFIXES = ("text/", "application/json", "application/xml", "application/xhtml")
_MAX_REDIRECTS = 3


class FetchDenied(Exception):
    """抓取被策略拒绝（不是网络错误）。消息会作为工具结果返回给模型。"""


@dataclass(frozen=True)
class FetchPolicy:
    """抓取策略。默认全部拒绝，必须显式配置才会放开。"""

    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    max_bytes: int = 512 * 1024
    timeout_s: float = 10.0
    user_agent: str = "bailiff/0.2 (+https://github.com/southcloud372/Bailiff)"

    def allows(self, host: str) -> bool:
        """精确匹配，或以 ``*.`` 开头的后缀匹配（只允许一层子域）。"""
        host = host.lower().rstrip(".")
        for pattern in self.allowed_hosts:
            pattern = pattern.lower().rstrip(".")
            if host == pattern:
                return True
            if pattern.startswith("*.") and host.endswith(pattern[1:]):
                return True
        return False


def parse_hosts(raw: str) -> frozenset[str]:
    """把 ``a.com, *.b.com`` 解析成集合（容忍空格与空项）。"""
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def _assert_public_host(host: str) -> None:
    """解析域名并确认所有地址都是公网地址。

    只检查「字符串里有没有内网 IP」是不够的：``attacker.com`` 可以解析到 ``127.0.0.1``。
    必须解析后再判断。
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise FetchDenied(f"域名无法解析: {host}") from exc

    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        if ip.version == 6 and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local  # 含 169.254.169.254 云元数据服务
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise FetchDenied(f"目标地址不是公网地址: {host} -> {ip}")


async def fetch_url(
    url: str,
    *,
    policy: FetchPolicy,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    """抓取一个 URL，返回结构化结果。策略违规抛 :class:`FetchDenied`。"""
    if not policy.allowed_hosts:
        raise FetchDenied("未配置允许抓取的域名（AGENT_HTTP_ALLOWED_HOSTS）")

    current = url
    async with httpx.AsyncClient(
        timeout=policy.timeout_s,
        follow_redirects=False,  # 手动跟随：每一跳都要重新校验
        transport=transport,
        headers={"user-agent": policy.user_agent},
    ) as client:
        for _hop in range(_MAX_REDIRECTS + 1):
            parsed = urlparse(current)
            if parsed.scheme not in _ALLOWED_SCHEMES:
                raise FetchDenied(f"不支持的协议: {parsed.scheme or '(空)'}")
            host = parsed.hostname or ""
            if not policy.allows(host):
                raise FetchDenied(f"域名不在白名单内: {host}")
            _assert_public_host(host)

            async with client.stream("GET", current) as resp:
                if resp.is_redirect:
                    location = resp.headers.get("location", "")
                    if not location:
                        raise FetchDenied("重定向缺少 Location")
                    current = str(httpx.URL(current).join(location))
                    continue

                ctype = resp.headers.get("content-type", "").lower()
                if ctype and not ctype.startswith(_ALLOWED_CONTENT_PREFIXES):
                    raise FetchDenied(f"不支持的内容类型: {ctype.split(';')[0]}")

                body = bytearray()
                truncated = False
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) >= policy.max_bytes:
                        truncated = True
                        break

                return {
                    "url": str(resp.url),
                    "status": resp.status_code,
                    "content_type": ctype.split(";")[0],
                    "bytes": len(body),
                    "truncated": truncated,
                    "text": body[: policy.max_bytes].decode("utf-8", errors="replace"),
                }

        raise FetchDenied(f"重定向超过 {_MAX_REDIRECTS} 次")


def build_http_tools(policy: FetchPolicy) -> dict[str, ToolFn]:
    """把抓取能力包装成网关工具。

    没有配置白名单时返回空字典，**能力不存在**比「存在但会拒绝」更安全：
    模型连这个工具都看不见，也就不会反复尝试。
    """

    async def http_fetch(principal: Principal, args: dict[str, Any]) -> dict[str, Any]:
        return await fetch_url(str(args["url"]), policy=policy)

    return {"http_fetch": http_fetch} if policy.allowed_hosts else {}
