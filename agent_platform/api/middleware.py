"""ASGI 中间件：请求 id 贯穿、访问日志、HTTP 指标。

为什么用「纯 ASGI 中间件」而不是 ``BaseHTTPMiddleware``？
- ``BaseHTTPMiddleware`` 会为每个请求再包一层 anyio 任务，流式响应（SSE）容易
  出现首包延迟甚至缓冲异常；纯 ASGI 中间件只是包一层 ``send``，对 SSE 零影响。
- 纯 ASGI 还能在 ``http.response.start`` 时改写响应头（回显 ``x-request-id``）。

它承担三件事，且都在最外层（任何业务逻辑之前）：
1. 生成/透传 request id —— 让日志、trace、审计、成本能按请求串起来；
2. 记录访问日志（结构化的，含耗时与路由模板）；
3. 打 HTTP 指标（按路由模板聚合，避免 ``/v1/tickets/abc123`` 造成 label 爆炸）。
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent_platform.context import current_request_id
from agent_platform.observability.metrics import HTTP_DURATION, HTTP_REQUESTS

logger = logging.getLogger("agent_platform.access")

#: 不纳入 HTTP 计数的路径（抓取指标本身不应该污染指标）。
_EXCLUDED_PATHS = frozenset({"/metrics"})

_ID_SEGMENT = re.compile(r"^[0-9a-fA-F-]{8,}$|^\d+$")


def _request_id_from(scope: Scope) -> str:
    """优先复用上游（网关/负载均衡）传入的 request id，保证跨服务可关联。"""
    for name, value in scope.get("headers", []):
        if name == b"x-request-id" and value:
            return bytes(value).decode("latin-1")
    return uuid.uuid4().hex


def route_template(scope: Scope) -> str:
    """取路由模板（如 ``/v1/tickets/{thread_id}/resume``）作为指标 label。"""
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    # 未命中路由（404）或中间件在路由之前运行时，退化为「把疑似 id 段替换掉」。
    raw = str(scope.get("path", "/"))
    return "/".join(
        "{id}" if _ID_SEGMENT.match(segment) else segment for segment in raw.split("/")
    )


class RequestContextMiddleware:
    """请求上下文 + 访问日志 + HTTP 指标，三合一的最外层中间件。"""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _request_id_from(scope)
        method = str(scope.get("method", "GET"))
        route = route_template(scope)
        started = time.perf_counter()
        status = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                headers = MutableHeaders(scope=message)
                headers["x-request-id"] = request_id
            await send(message)

        token = current_request_id.set(request_id)
        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            elapsed = time.perf_counter() - started
            route = route_template(scope)
            excluded = route in _EXCLUDED_PATHS
            if not excluded:
                HTTP_REQUESTS.inc(method=method, route=route, status=str(status))
                HTTP_DURATION.observe(elapsed, method=method, route=route)
                logger.info(
                    "%s %s %s %.1fms request_id=%s",
                    method,
                    route,
                    status,
                    elapsed * 1000,
                    request_id,
                    extra={
                        "event": "http_request",
                        "method": method,
                        "route": route,
                        "status": status,
                        "duration_ms": round(elapsed * 1000, 2),
                        "request_id": request_id,
                    },
                )
            current_request_id.reset(token)
