"""网关 HTTP 传输层：把治理网关暴露成 FastAPI 服务。

端点：
- GET  /healthz       存活探针
- GET  /v1/tools      列出工具（需鉴权）
- POST /v1/call       调用工具（Bearer 鉴权 → 网关治理四件套）
- /mcp/fs/mcp         文件系统 MCP server（streamable HTTP，供外部 MCP 客户端）
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, cast

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from agent_platform.gateway.auth import AuthenticationError
from agent_platform.gateway.errors import PermissionDenied, RateLimitExceeded, UnknownTool
from agent_platform.gateway.policy import Principal
from agent_platform.gateway.security import SandboxViolation
from agent_platform.gateway.wiring import Platform, build_platform
from agent_platform.tools.filesystem import create_filesystem_server

_bearer = HTTPBearer(auto_error=False)


class CallRequest(BaseModel):
    """一次工具调用请求。"""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


def _platform(request: Request) -> Platform:
    return cast(Platform, request.app.state.platform)


def _get_principal(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> Principal:
    if credentials is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 Bearer token")
    try:
        return _platform(request).verifier.verify(credentials.credentials)
    except AuthenticationError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc


def create_app(platform: Platform | None = None) -> FastAPI:
    """构建 FastAPI 应用（含 MCP mount）。"""
    platform = platform or build_platform()

    # 文件系统 MCP server：以 streamable HTTP 暴露给外部 MCP 客户端。
    # 完整端点 /mcp/fs/mcp（外层 /mcp/fs 是挂载前缀，内层 /mcp 是 MCP 自身的路由）。
    fs_server = create_filesystem_server(platform.settings.fs_root)
    mcp_app = fs_server.streamable_http_app(streamable_http_path="/mcp")
    mcp_manager = fs_server.session_manager

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # 挂载进来的 MCP app 的 lifespan 不会被外层 Router 自动执行，
        # 这里在 FastAPI 的 lifespan 里手动跑它的 session manager（官方推荐的挂载方式）。
        async with mcp_manager.run():
            yield

    app = FastAPI(title="Agent Platform Gateway", version="0.1.0", lifespan=lifespan)
    app.state.platform = platform
    app.mount("/mcp/fs", mcp_app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/tools")
    async def list_tools(
        request: Request,
        principal: Annotated[Principal, Depends(_get_principal)],
    ) -> dict[str, list[str]]:
        return {"tools": sorted(_platform(request).registry.names())}

    @app.post("/v1/call")
    async def call(
        request: Request,
        payload: CallRequest,
        principal: Annotated[Principal, Depends(_get_principal)],
    ) -> dict[str, Any]:
        try:
            result = await _platform(request).gateway.call(
                principal, payload.tool, payload.arguments
            )
        except PermissionDenied as exc:
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
        except RateLimitExceeded as exc:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc
        except UnknownTool as exc:
            raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
        except SandboxViolation as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"沙箱违规: {exc}") from exc
        return {"result": result}

    return app
