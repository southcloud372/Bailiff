"""网关 HTTP 传输层：把治理网关暴露成 FastAPI 服务。

端点：
- GET  /healthz       存活探针
- GET  /v1/tools      列出工具（需鉴权）
- POST /v1/call       调用工具（Bearer 鉴权 → 网关治理四件套）
- POST /v1/chat       Agent 对话（Bearer 鉴权 → principal 贯穿 Agent → 网关工具）
- /mcp/fs/mcp         文件系统 MCP server（streamable HTTP，供外部 MCP 客户端）
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, Protocol, cast

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from agent_platform.core.workflow import TicketWorkflow
from agent_platform.gateway.auth import AuthenticationError
from agent_platform.gateway.errors import PermissionDenied, RateLimitExceeded, UnknownTool
from agent_platform.gateway.policy import Principal
from agent_platform.gateway.security import SandboxViolation
from agent_platform.gateway.wiring import Platform, build_platform
from agent_platform.guardrails.guards import GuardViolation
from agent_platform.router.cost import CostTracker
from agent_platform.tools.filesystem import create_filesystem_server

_bearer = HTTPBearer(auto_error=False)


class ChatAgent(Protocol):
    """API 层对 Agent 的最小依赖接口（可对话），具体实现见 core.agent.Agent。

    用 Protocol 而非具体类型：API 层不直接 import langchain，测试可用鸭子类型注入。
    """

    async def chat(
        self, message: str, *, thread_id: str, principal: Principal
    ) -> str: ...

    def stream(
        self, message: str, *, thread_id: str, principal: Principal
    ) -> AsyncIterator[str]: ...


class CallRequest(BaseModel):
    """一次工具调用请求。"""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ChatRequest(BaseModel):
    """一次 Agent 对话请求。"""

    message: str


class TicketStartRequest(BaseModel):
    """发起工单（HITL 工作流）。"""

    message: str
    thread_id: str | None = None


class TicketResumeRequest(BaseModel):
    """审批意见，用于恢复被暂停的工单。"""

    approve: bool
    note: str = ""


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


def create_app(
    platform: Platform | None = None,
    *,
    agent: ChatAgent | None = None,
    workflow: TicketWorkflow | None = None,
    cost_tracker: CostTracker | None = None,
) -> FastAPI:
    """构建 FastAPI 应用（含 MCP mount）。

    agent / workflow / cost_tracker 由调用方组装后注入（默认不内置），未注入时
    对应端点返回 503；具体接线见 scripts/serve.py。这样 build_platform 保持
    「网关组合」的纯粹职责，不隐式引入模型依赖。
    """
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
    app.state.agent = agent
    app.state.workflow = workflow
    app.state.cost_tracker = cost_tracker
    app.mount("/mcp/fs", mcp_app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/cost")
    async def cost(
        request: Request,
        principal: Annotated[Principal, Depends(_get_principal)],
    ) -> dict[str, Any]:
        tracker = cast(CostTracker | None, request.app.state.cost_tracker)
        if tracker is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "未配置成本追踪")
        return tracker.summary().model_dump()

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

    @app.post("/v1/chat")
    async def chat(
        request: Request,
        payload: ChatRequest,
        principal: Annotated[Principal, Depends(_get_principal)],
    ) -> dict[str, str]:
        agent_obj = cast(ChatAgent | None, request.app.state.agent)
        if agent_obj is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "未配置模型")
        # thread_id 用用户 id，使同一用户的对话保持上下文连续性（checkpoint 按 thread 分片）
        try:
            reply = await agent_obj.chat(
                payload.message, thread_id=principal.id, principal=principal
            )
        except GuardViolation as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        return {"reply": reply}

    @app.post("/v1/chat/stream")
    async def chat_stream(
        request: Request,
        payload: ChatRequest,
        principal: Annotated[Principal, Depends(_get_principal)],
    ) -> StreamingResponse:
        agent_obj = cast(ChatAgent | None, request.app.state.agent)
        if agent_obj is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "未配置模型")

        def _sse(data: Any) -> str:
            return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

        async def event_stream() -> AsyncIterator[str]:
            # 输入护栏在 stream() 首个分块前触发，这里以 error 事件表达（流已提交 200）。
            try:
                async for delta in agent_obj.stream(
                    payload.message, thread_id=principal.id, principal=principal
                ):
                    yield _sse({"delta": delta})
                yield "data: [DONE]\n\n"
            except GuardViolation as exc:
                yield _sse({"error": str(exc)})

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.post("/v1/tickets")
    async def start_ticket(
        request: Request,
        payload: TicketStartRequest,
        principal: Annotated[Principal, Depends(_get_principal)],
    ) -> dict[str, Any]:
        wf = cast(TicketWorkflow | None, request.app.state.workflow)
        if wf is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "未配置工作流")
        run = await wf.start(
            payload.message,
            principal=principal,
            thread_id=payload.thread_id or principal.id,
        )
        return run.model_dump()

    @app.post("/v1/tickets/{thread_id}/resume")
    async def resume_ticket(
        request: Request,
        thread_id: str,
        payload: TicketResumeRequest,
        principal: Annotated[Principal, Depends(_get_principal)],
    ) -> dict[str, Any]:
        wf = cast(TicketWorkflow | None, request.app.state.workflow)
        if wf is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "未配置工作流")
        run = await wf.resume(
            thread_id,
            principal=principal,
            approve=payload.approve,
            note=payload.note,
        )
        return run.model_dump()

    return app
