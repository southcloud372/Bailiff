"""网关 HTTP 传输层：把治理网关暴露成 FastAPI 服务。

端点：
- GET  /healthz       存活探针（只看进程本身，绝不查下游）
- GET  /readyz        就绪探针（并发探测下游依赖，任一 critical 失败 → 503）
- GET  /metrics       Prometheus 文本格式指标
- GET  /v1/tools      列出工具（需鉴权）
- POST /v1/call       调用工具（Bearer 鉴权 → 网关治理四件套）
- POST /v1/chat       Agent 对话（有状态，多轮记忆）
- POST /v1/ask         Agent 单轮问答（无状态，可走语义缓存）
- POST /v1/chat/stream SSE 流式对话
- GET  /v1/cache      语义缓存统计；DELETE 清空
- POST /v1/tickets    发起 HITL 工单；POST /{id}/resume 审批后恢复
- /mcp/fs/mcp         文件系统 MCP server（streamable HTTP，供外部 MCP 客户端）
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, Protocol, cast

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from agent_platform.api.middleware import RequestContextMiddleware
from agent_platform.core.workflow import TicketWorkflow
from agent_platform.gateway.auth import AuthenticationError, encode_token
from agent_platform.gateway.errors import PermissionDenied, RateLimitExceeded, UnknownTool
from agent_platform.gateway.policy import Principal
from agent_platform.gateway.security import SandboxViolation
from agent_platform.gateway.wiring import Platform, build_platform
from agent_platform.guardrails.guards import GuardViolation
from agent_platform.observability.health import Readiness, callable_probe, static_probe
from agent_platform.observability.metrics import METRICS
from agent_platform.router.budget import BudgetExceeded
from agent_platform.router.cost import CostTracker
from agent_platform.tools.filesystem import create_filesystem_server

logger = logging.getLogger(__name__)

_bearer = HTTPBearer(auto_error=False)


class ChatAgent(Protocol):
    """API 层对 Agent 的最小依赖接口（可对话），具体实现见 core.agent.Agent。

    用 Protocol 而非具体类型：API 层不直接 import langchain，测试可用鸭子类型注入。
    """

    async def chat(
        self, message: str, *, thread_id: str, principal: Principal
    ) -> str: ...

    async def ask(self, message: str, *, principal: Principal) -> str: ...

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
    # 可选会话 id：同一用户可以有多个并行会话（前端「新对话」按钮）。
    # 不传则退化为「一个用户一个会话」（旧行为）。
    thread_id: str | None = Field(default=None, max_length=64)


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


def default_readiness(platform: Platform) -> Readiness:
    """最小就绪探针：工具注册表非空 + 鉴权密钥不是示例值（按环境分级）。

    serve.py 会在此基础上追加 checkpoint / MCP / Redis 等下游探针。
    """

    def _auth_ok() -> tuple[bool, str]:
        weak = {"", "dev-secret-change-me", "dev-jwt-secret-local-only-0123456789abcdef"}
        if platform.settings.jwt_secret in weak:
            if platform.settings.env == "dev":
                # 本地开发允许用示例密钥：只告警，不阻断（否则 clone 下来就 503）
                return True, "使用开发示例密钥（仅限本地，生产必须覆盖）"
            return False, "JWT 密钥仍是示例值，禁止上线"
        return True, "ok"

    return (
        Readiness()
        .add(
            "gateway",
            static_probe(True, f"{len(platform.registry.names())} 个工具已注册"),
            critical=True,
        )
        .add("auth", callable_probe(_auth_ok), critical=True)
    )


def _thread_key(principal: Principal, requested: str | None) -> str:
    """把「用户提供的会话 id」转成**带身份隔离**的 checkpoint 键。

    为什么必须加前缀：checkpoint 是全局按 thread_id 索引的。若直接使用客户端传来的
    thread_id，用户 A 只要猜到用户 B 的 id 就能读走整段对话历史——这是一类真实发生过的
    越权（IDOR）。加 principal 前缀后，别人的线程根本不在你的命名空间里。

    不传 thread_id 时退化为 principal.id，保持「一个用户一个默认会话」的旧行为，
    这样已有部署升级后历史不会丢。
    """
    if not requested or requested == principal.id:
        # 未指定会话 → 沿用 principal.id（升级后旧历史仍可读）
        return principal.id
    prefix = f"{principal.id}#"
    if requested.startswith(prefix):
        # 已经是本用户的合成键（例如 /v1/tickets 回传的 thread_id 再传回来），保持幂等，
        # 否则同一次审批会落到两个不同的 checkpoint 上——表现为「状态凭空消失」。
        return requested
    # 用 # 而非 : 分隔：JWT sub 里可能含 :，但几乎不会含 #
    return f"{prefix}{requested}"


def _default_console_dir() -> Path | None:
    """定位前端构建产物 ``web/dist``（未构建时返回 None，由调用方降级为纯 API）。"""
    candidate = Path(__file__).resolve().parents[2] / "web" / "dist"
    return candidate if candidate.is_dir() else None


def create_app(
    platform: Platform | None = None,
    *,
    agent: ChatAgent | None = None,
    workflow: TicketWorkflow | None = None,
    cost_tracker: CostTracker | None = None,
    readiness: Readiness | None = None,
    cache: Any | None = None,
    startup_hooks: list[Any] | None = None,
    shutdown_hooks: list[Any] | None = None,
    console_dir: Path | None = None,
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
    # MCP 2026-07-28：无状态模式（无 initialize 握手 / 无 Mcp-Session-Id），
    # 每个请求自含协议版本与能力，可上 LB/Serverless。
    mcp_app = fs_server.streamable_http_app(
        streamable_http_path="/mcp", stateless_http=True
    )
    mcp_manager = fs_server.session_manager

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # 挂载进来的 MCP app 的 lifespan 不会被外层 Router 自动执行，
        # 这里在 FastAPI 的 lifespan 里手动跑它的 session manager（官方推荐的挂载方式）。
        async with mcp_manager.run():
            # 自定义启动钩子：连接外部 MCP server、初始化 Postgres checkpointer 等。
            # 顺序执行（有依赖关系时保序），任一失败由钩子自己决定是否致命。
            for hook in startup_hooks or []:
                await hook()
            try:
                yield
            finally:
                for hook in reversed(shutdown_hooks or []):
                    await hook()

    app = FastAPI(title="Agent Platform Gateway", version="0.1.0", lifespan=lifespan)
    # 最外层中间件：request id 透传/回显、结构化访问日志、HTTP 指标。
    # 必须是纯 ASGI 实现——BaseHTTPMiddleware 会破坏 SSE 的逐块下发。
    app.add_middleware(RequestContextMiddleware)
    app.state.platform = platform
    app.state.agent = agent
    app.state.workflow = workflow
    app.state.cost_tracker = cost_tracker
    app.state.readiness = readiness or default_readiness(platform)
    app.state.cache = cache
    app.mount("/mcp/fs", mcp_app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        """存活探针：只回答「进程还在不在」，不触碰任何下游。"""
        return {"status": "ok"}

    if platform.settings.env == "dev":

        class DevTokenRequest(BaseModel):
            """演示登录：前端点一下就能拿到可用的 JWT。"""

            principal_id: str = Field(default="demo", max_length=64)
            roles: list[str] = Field(default_factory=lambda: ["agent"])

        @app.post("/v1/auth/dev-token")
        async def dev_token(payload: DevTokenRequest | None = None) -> dict[str, Any]:
            """**仅 dev 环境**签发演示 token，让控制台免去手工造 JWT。

            生产环境这条路由根本不存在（不是靠鉴权挡，而是压根不注册）——这比
            「注册了再判断 env」更安全：没有可被绕过的分支。生产请接 IdP / 网关签发。
            """
            req = payload or DevTokenRequest()
            token = encode_token(
                req.principal_id,
                set(req.roles),
                platform.settings.jwt_secret,
                ttl_seconds=platform.settings.token_ttl_seconds,
            )
            return {
                "token": token,
                "principal_id": req.principal_id,
                "roles": req.roles,
                "expires_in": platform.settings.token_ttl_seconds,
            }

    @app.get("/readyz")
    async def readyz(request: Request) -> PlainTextResponse:
        """就绪探针：并发探测下游，critical 依赖失败返回 503（摘流量但不重启）。"""
        report = await cast(Readiness, request.app.state.readiness).check()
        return PlainTextResponse(
            json.dumps(report.as_dict(), ensure_ascii=False),
            status_code=200 if report.ready else status.HTTP_503_SERVICE_UNAVAILABLE,
            media_type="application/json",
        )

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        """Prometheus 文本暴露格式（运维抓取用，不带鉴权，只暴露聚合指标）。"""
        return PlainTextResponse(
            METRICS.render(), media_type="text/plain; version=0.0.4; charset=utf-8"
        )

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
            # 403 有两种成因：角色没权限，或该能力在本实例压根没注册（只读模式 /
            # 未配置白名单）。错误信息里点名两者，否则运维只能看到工具名，
            # 分不清是「配错了」还是「没权限」——这种含糊会浪费排查时间。
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"无权调用工具或该实例未启用: {exc}（先看 /v1/tools 的实际清单）",
            ) from exc
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
                payload.message,
                thread_id=_thread_key(principal, payload.thread_id),
                principal=principal
            )
        except GuardViolation as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except BudgetExceeded as exc:
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
        return {"reply": reply}

    @app.post("/v1/ask")
    async def ask(
        request: Request,
        payload: ChatRequest,
        principal: Annotated[Principal, Depends(_get_principal)],
    ) -> dict[str, str]:
        """无状态单轮问答：独立 thread、问完即清理、允许语义缓存命中。"""
        agent_obj = cast(ChatAgent | None, request.app.state.agent)
        if agent_obj is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "未配置模型")
        ask_fn = getattr(agent_obj, "ask", None)
        if ask_fn is None:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, "当前 Agent 实现不支持无状态问答"
            )
        try:
            reply = await ask_fn(payload.message, principal=principal)
        except GuardViolation as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        except BudgetExceeded as exc:
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
        return {"reply": reply}

    @app.get("/v1/cache")
    async def cache_stats(
        request: Request,
        principal: Annotated[Principal, Depends(_get_principal)],
    ) -> dict[str, Any]:
        cache_obj = request.app.state.cache
        if cache_obj is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "未启用语义缓存")
        return cast(dict[str, Any], cast(Any, cache_obj.stats()).model_dump())

    @app.delete("/v1/cache")
    async def cache_clear(
        request: Request,
        principal: Annotated[Principal, Depends(_get_principal)],
    ) -> dict[str, str]:
        cache_obj = request.app.state.cache
        if cache_obj is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "未启用语义缓存")
        cast(Any, cache_obj).clear()
        return {"status": "cleared"}

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
            # 输入护栏在首个分块前触发，这里以 error 事件表达（流已提交 200）。
            # 优先用 stream_events：能同时下发推理与工具事件，前端才不是「黑屏等待」。
            emit_events = getattr(agent_obj, "stream_events", None)
            try:
                if emit_events is not None:
                    async for event in emit_events(
                        payload.message,
                        thread_id=_thread_key(principal, payload.thread_id),
                        principal=principal
                    ):
                        # 兼容早期客户端：delta 事件同时带 "delta" 键（历史字段名）。
                        if event.get("type") == "delta":
                            event = {**event, "delta": event["text"]}
                        yield _sse(event)
                else:  # 兼容只实现 stream() 的注入对象（测试替身）
                    async for delta in agent_obj.stream(
                        payload.message,
                        thread_id=_thread_key(principal, payload.thread_id),
                        principal=principal
                    ):
                        yield _sse({"delta": delta})
                yield "data: [DONE]\n\n"
            except GuardViolation as exc:
                yield _sse({"error": str(exc)})
            except BudgetExceeded as exc:
                yield _sse({"error": str(exc)})
            except Exception as exc:
                logger.exception("chat_stream_failed")
                # dev 暴露真实原因（否则排查一次要翻整条日志）；prod 只给错误类型，
                # 避免把上游报文（可能含内部地址/密钥片段）泄露给调用方。
                detail = str(exc) if platform.settings.env == "dev" else ""
                yield _sse(
                    {"error": f"生成中断：{type(exc).__name__}", "detail": detail[:500]}
                )

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
            thread_id=_thread_key(principal, payload.thread_id),
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
            # 路径参数是「用户眼里的会话 id」，checkpoint 键要经过同样的命名空间化，
            # 否则审批会恢复到一个空状态上（表现为工单"凭空消失"）。
            _thread_key(principal, thread_id),
            principal=principal,
            approve=payload.approve,
            note=payload.note,
        )
        return run.model_dump()

    # 控制台静态托管：收敛为「一个容器、一个端口」，demo 与自托管都少一层 nginx。
    #
    # 注意不要 mount 到 "/"：静态目录会接管所有未匹配路径，导致未知的 POST 路由
    # 返回 405（Method Not Allowed）而不是 404，把「没有这个接口」伪装成「方法不对」。
    # 这里显式挂 /assets + 兜底 GET，语义清晰且不影响 API 的 404。
    console = console_dir or _default_console_dir()
    if console is not None and (console / "index.html").is_file():
        assets = console / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")
        index_file = console / "index.html"

        @app.get("/", include_in_schema=False)
        async def console_index() -> FileResponse:
            return FileResponse(index_file)

        @app.exception_handler(404)
        async def spa_fallback(request: Request, exc: Exception) -> Response:
            """前端路由兜底：非 API 的 GET 回 index.html，其余保持真正的 404。

            不用 ``@app.get("/{path:path}")`` 兜底：那样任何未知 POST 都会变成
            405（路径匹配但方法不符），把「接口不存在」伪装成「方法用错」，
            对客户端调试是误导。这里按 method + 前缀显式判断。
            """
            path = request.url.path
            is_api = path.startswith(("/v1/", "/mcp/", "/healthz", "/readyz", "/metrics"))
            if request.method in ("GET", "HEAD") and not is_api:
                return FileResponse(index_file)
            return JSONResponse({"detail": "Not Found"}, status_code=404)

    else:

        @app.get("/", include_in_schema=False)
        async def landing() -> PlainTextResponse:
            """未构建前端时的极简首页：至少让人知道服务起来了、下一步去哪。"""
            return PlainTextResponse(
                "Agent Platform is running.\n"
                "  GET  /healthz  /readyz  /metrics\n"
                "  POST /v1/chat  /v1/chat/stream\n"
                "  GET  /docs    (OpenAPI)\n"
                "  控制台：cd web && npm install && npm run build 后重启即出现在本页\n",
                media_type="text/plain; charset=utf-8",
            )

    return app
