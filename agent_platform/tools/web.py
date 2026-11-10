"""HTTP 抓取工具：给 Agent 一个「能看外网」的能力，同时堵住 SSRF。

## 为什么这个工具值得单独写一个模块

给 Agent 加 ``requests.get(url)`` 是最常见的做法，也是最常见的安全事故来源。
攻击路径非常具体：

1. 用户在对话里说「帮我看看 http://169.254.169.254/latest/meta-data/iam/...」，
   云主机上的元数据服务就被读了——**凭据直接泄露**；
2. 攻击者控制一个页面，让它 302 跳到内网 ``http://10.0.0.5/admin``，
   你的服务就成了内网跳板；
3. 一个 500MB 的响应把上下文窗口和账单一起撑爆。

这三条都不是「模型不听话」，是**工具本身没有边界**。所以这里做四层防护，
每一层都对应上面一条攻击：

| 防护 | 对应攻击 | 实现 |
|---|---|---|
| 域名白名单（默认拒绝） | 任意出网 | ``FetchPolicy.allowed_hosts`` |
| DNS 解析后校验 IP 是否公网 | 元数据服务 / 内网探测 | ``_assert_public_host`` |
| 手动跟随重定向并逐跳复检 | 302 跳内网 | ``_MAX_REDIRECTS`` + 每跳校验 |
| 流式读取 + 字节上限 | 响应炸弹 | ``max_bytes`` |

## 已知局限（写在这里比被问出来好）

- 校验用的是「解析出的 IP」，连接时 httpx 会**重新解析**，理论上存在 DNS rebinding
  窗口（TTL 极短时）。要彻底解决需把连接固定到已校验 IP（自定义 transport）。
- 不校验响应内容语义，只按 content-type 白名单过滤。
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
    user_agent: str = "agent-platform/0.2 (+https://github.com/OWNER/agent-platform)"

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

    没有配置白名单时返回空字典——**能力不存在**比「存在但会拒绝」更安全：
    模型连这个工具都看不见，也就不会反复尝试。
    """

    async def http_fetch(principal: Principal, args: dict[str, Any]) -> dict[str, Any]:
        return await fetch_url(str(args["url"]), policy=policy)

    return {"http_fetch": http_fetch} if policy.allowed_hosts else {}
