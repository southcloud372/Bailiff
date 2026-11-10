"""LangGraph 编排层：ReAct Agent（模型 + 网关工具闭环）。

模型用 LangChain ChatOpenAI 对接 DeepSeek（OpenAI 兼容），工具统一走 Gateway，
每一次工具调用都经过鉴权/权限/限流/审计，治理不旁路。

身份传递：工具执行时的「调用方身份」不写死在 Agent 上，而是通过 contextvar 按请求
解析，用户 JWT → Principal 贯穿整个 Agent 工具链，落到网关的权限检查。

注：模型路由（router/llm.py 的 LiteLLM ModelRouter）是更低一层的「多供应商路由/
降级/成本」抽象；这里先用 LangChain 原生 chat model 以获得 tool-calling 集成，
两者共享 ModelSettings。后续可把 Agent 的模型也接到 ModelRouter（ChatLiteLLM 或
自定义 BaseChatModel）以复用降级能力。
"""

from __future__ import annotations

import contextvars
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any, Protocol

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field, SecretStr

from agent_platform.core.checkpoint import build_checkpointer
from agent_platform.gateway.config import ModelSettings
from agent_platform.gateway.errors import PermissionDenied, RateLimitExceeded, UnknownTool
from agent_platform.gateway.gateway import Gateway
from agent_platform.gateway.policy import Principal
from agent_platform.gateway.security import SandboxViolation
from agent_platform.guardrails.guards import (
    GuardViolation,
    InputGuard,
    OutputGuard,
    default_input_guard,
    default_output_guard,
)
from agent_platform.observability.tracing import TracingCallback, start_tool_span
from agent_platform.router.budget import BudgetPolicy
from agent_platform.router.cache import CachedResponse
from agent_platform.router.cost import CostCallbackHandler, CostTracker
from agent_platform.router.deepseek import DeepSeekChatModel

logger = logging.getLogger(__name__)


class ResponseCache(Protocol):
    """答复缓存的最小接口（``SemanticCache`` / ``NullCache`` 都满足）。"""

    async def get(self, query: str) -> CachedResponse | None: ...

    async def set(self, query: str, response: CachedResponse) -> None: ...

# 每个请求/会话的「当前调用方身份」，工具执行时从这里解析 principal。
current_principal: contextvars.ContextVar[Principal | None] = contextvars.ContextVar(
    "agent_principal", default=None
)


@contextmanager
def principal_context(principal: Principal) -> Iterator[None]:
    """在上下文内设置当前调用方身份，供工具执行时解析。"""
    token = current_principal.set(principal)
    try:
        yield
    finally:
        current_principal.reset(token)


class ReadFileArgs(BaseModel):
    """read_file 的参数。"""

    path: str = Field(description="根目录内的相对文件路径")


class WriteFileArgs(BaseModel):
    """write_file 的参数。"""

    path: str = Field(description="根目录内的相对文件路径")
    content: str = Field(description="要写入的文本内容")


class ListDirArgs(BaseModel):
    """list_dir 的参数。"""

    path: str = Field(default=".", description="根目录内的相对目录路径")


class SearchKnowledgeArgs(BaseModel):
    """search_knowledge 的参数。"""

    query: str = Field(description="要在企业知识库中检索的问题或关键词")


class RememberArgs(BaseModel):
    """remember 的参数。"""

    content: str = Field(description="要写入当前用户长期记忆的内容")


class RecallArgs(BaseModel):
    """recall 的参数。"""

    query: str = Field(description="要回忆的问题或关键词")


class HttpFetchArgs(BaseModel):
    """http_fetch 的参数。"""

    url: str = Field(description="要抓取的完整 URL，必须命中服务端配置的域名白名单")


def resolve_model_backend(settings: ModelSettings) -> str:
    """决定用真模型还是离线演示模型。

    ``auto`` 的语义是**有 key 就用真的，没 key 就降级**，这让「clone 即跑」成为可能，
    同时把「悄悄用假模型」的风险交给 /readyz 暴露（它会报 degraded）。
    ``deepseek`` 则严格要求 key，缺了直接抛错，避免生产环境静默降级。
    """
    if settings.llm_backend == "mock":
        return "mock"
    if settings.llm_backend == "deepseek":
        if not settings.api_key:
            raise RuntimeError(
                "AGENT_LLM_BACKEND=deepseek 但没有配置 DEEPSEEK_API_KEY"
            )
        return "deepseek"
    return "deepseek" if settings.api_key else "mock"


def build_model(
    settings: ModelSettings, *, cost_tracker: CostTracker | None = None
) -> BaseChatModel:
    """构建 LangChain 聊天模型（OpenAI 兼容 → DeepSeek）。

    挂两个回调：计费（累计 token 与成本）与可观测（OTel span，未配置时 no-op）。
    未配置 API Key 时返回离线演示模型，保证整条链路（SSE/指标/审计/计费）仍可跑通。
    """
    if resolve_model_backend(settings) == "mock":
        from agent_platform.core.mock import MockChatModel

        logger.warning(
            "llm_backend=mock：未配置 DEEPSEEK_API_KEY，使用离线演示模型（不会真实推理）"
        )
        return MockChatModel()

    callbacks: list[BaseCallbackHandler] = [TracingCallback()]
    if cost_tracker is not None:
        callbacks.append(CostCallbackHandler(cost_tracker))
    # 用 DeepSeekChatModel 而非裸 ChatOpenAI：思考模型的 reasoning_content
    # 必须在工具调用链里回传，否则第二轮必 400（详见 router/deepseek.py）。
    return DeepSeekChatModel(
        model=settings.model,
        api_key=SecretStr(settings.api_key),
        base_url=settings.base_url,
        temperature=0.3,
        callbacks=callbacks,
    )


_MAX_TOOL_RESULT_CHARS = 8000
"""工具结果进入上下文前的字符上限。

为什么必须在工具出口截断：模型上下文是有限且**按 token 计费**的资源。
一个返回 200KB 日志的 read_file 会同时造成三件事，
上下文爆掉导致前文被挤走、单轮成本涨十倍、延迟从 2 秒变 20 秒。
在出口截断比在提示词里求模型「少读点」可靠得多。
"""


def _truncate_result(result: Any) -> Any:
    """截断过长的工具结果，并明确告知被截断（而不是静默丢弃）。"""
    if isinstance(result, str) and len(result) > _MAX_TOOL_RESULT_CHARS:
        kept = result[:_MAX_TOOL_RESULT_CHARS]
        return (
            f"{kept}\n\n[已截断：原始 {len(result)} 字符，"
            f"仅保留前 {_MAX_TOOL_RESULT_CHARS} 字符。如需更多请缩小范围后重试]"
        )
    return result


def _error_kind(exc: Exception) -> str:
    """把异常映射成稳定的错误类别，供日志聚合与前端提示使用。"""
    if isinstance(exc, PermissionDenied):
        return "permission_denied"
    if isinstance(exc, RateLimitExceeded):
        return "rate_limited"
    if isinstance(exc, UnknownTool):
        return "unknown_tool"
    if isinstance(exc, SandboxViolation):
        return "sandbox_denied"
    return "upstream_error"


def _tool_error(name: str, exc: Exception) -> str:
    """把工具异常转成模型可读的结构化结果。

    为什么返回文本而不是抛出：LangGraph 会把异常变成一次「失败的工具调用」，
    但不同版本对错误消息的处理不一致（有的直接中断整轮）。统一成文本结果，
    行为在任何模型/版本上都可预测，也让 Agent 有机会换一个工具重试。
    """
    detail = f"{type(exc).__name__}: {exc}"[:300]
    return json.dumps(
        {
            "ok": False,
            "tool": name,
            "kind": _error_kind(exc),
            "error": detail,
            "hint": "该工具暂时不可用，请换一种方式完成任务，或告知用户此能力已降级。",
        },
        ensure_ascii=False,
    )


def build_tools(gateway: Gateway) -> list[StructuredTool]:
    """把网关里的工具包装成 LangChain 工具，供 Agent 调用。"""

    def _tool(name: str, description: str, schema: type[BaseModel]) -> StructuredTool:
        async def _run(**kwargs: Any) -> Any:
            principal = current_principal.get()
            if principal is None:
                raise RuntimeError("工具执行时未设置 principal（需经 Agent.chat 进入）")
            span = start_tool_span(name, principal.id)
            try:
                return _truncate_result(await gateway.call(principal, name, dict(kwargs)))
            except Exception as exc:
                # **所有**工具异常（含越权、限流、沙箱拒绝、上游故障）
                # 都转成模型可读的结构化结果，而不是向上抛。
                #
                # 原因不是「宽容」，而是协议约束：OpenAI 兼容 API 要求
                # 「带 tool_calls 的 assistant 消息后面必须有对应数量的 tool 消息」。
                # 一旦某个工具抛异常导致少一条 tool 消息，这条会话线程就**永久坏死**，
                # 后续每一轮都会 400，且错误信息与真实原因无关，极难排查。
                #
                # 治理并没有被削弱：拒绝仍发生在网关（鉴权/权限/限流），审计照样落库，
                # 只是「拒绝的结果」作为一次失败的观察交给模型，让它向用户解释。
                logger.warning(
                    "tool_failed",
                    extra={
                        "tool": name,
                        "error": f"{type(exc).__name__}: {exc}",
                        "kind": _error_kind(exc),
                    },
                )
                return _tool_error(name, exc)
            finally:
                span.end()

        return StructuredTool.from_function(
            name=name,
            description=description,
            args_schema=schema,
            coroutine=_run,
        )

    names = gateway.tool_names()

    # 按**网关实际注册情况**暴露工具，而不是写死清单。
    # 反例（曾经的写法）：直接列三个文件工具。只读模式下网关没有 write_file，
    # 但 Agent 仍会看到并调用它，模型要先浪费一次调用才知道「这个能力不存在」。
    # 正确做法是「网关注册了什么，模型才看得见什么」。
    filesystem_specs: dict[str, tuple[type[BaseModel], str]] = {
        "read_file": (ReadFileArgs, "读取根目录内的文本文件内容"),
        "write_file": (WriteFileArgs, "写入文本到根目录内的文件"),
        "list_dir": (ListDirArgs, "列出根目录内某个目录的内容"),
    }
    tools = [
        _tool(name, description, schema)
        for name, (schema, description) in filesystem_specs.items()
        if name in names
    ]

    # 知识/记忆工具同样由网关注册（serve.py 组装），按注册情况动态暴露。
    if "search_knowledge" in names:
        tools.append(
            _tool("search_knowledge", "检索企业知识库，返回带来源的上下文片段", SearchKnowledgeArgs)
        )
    if "remember" in names:
        tools.append(_tool("remember", "把内容写入当前用户的长期记忆", RememberArgs))
    if "recall" in names:
        tools.append(_tool("recall", "按语义召回当前用户的长期记忆", RecallArgs))
    if "http_fetch" in names:
        tools.append(
            _tool(
                "http_fetch",
                "抓取指定 URL 的文本内容（受域名白名单限制，返回体上限 512KB）",
                HttpFetchArgs,
            )
        )
    return tools


class Agent:
    """对编译好的 LangGraph agent 的高层封装。"""

    def __init__(
        self,
        graph: CompiledStateGraph[Any, Any, Any, Any],
        *,
        input_guard: InputGuard | None = None,
        output_guard: OutputGuard | None = None,
        budget: BudgetPolicy | None = None,
        cost_tracker: CostTracker | None = None,
        cache: ResponseCache | None = None,
        model_name: str = "",
    ) -> None:
        self._graph = graph
        self._input_guard = input_guard or default_input_guard
        self._output_guard = output_guard or default_output_guard
        self._budget = budget
        self._cost_tracker = cost_tracker
        self._cache = cache
        self._model_name = model_name

    def _check_budget(self) -> None:
        """模型调用前检查预算：仅在同时配置了预算与成本追踪时生效。"""
        if self._budget is not None and self._cost_tracker is not None:
            self._budget.check(self._cost_tracker)

    async def chat(
        self,
        message: str,
        *,
        thread_id: str = "default",
        principal: Principal,
        use_cache: bool = False,
    ) -> str:
        """以指定身份运行一轮对话，返回最终回复文本。

        输入护栏挡在模型之前（拦截注入），输出护栏挡在返回之前（脱敏）。

        ``use_cache`` 只对**无状态**问答开启：语义缓存按问题文本命中，若同一
        thread 里有历史上下文，缓存会绕过上下文导致答非所问。需要多轮记忆的
        场景必须留在 ``/v1/chat``。
        """
        check = self._input_guard.check(message)
        if not check.allowed:
            raise GuardViolation(check.reason)
        self._check_budget()

        if use_cache and self._cache is not None:
            hit = await self._cache.get(message)
            if hit is not None:
                return hit.content

        with principal_context(principal):
            result = await self._graph.ainvoke(
                {"messages": [HumanMessage(content=message)]},
                config={"configurable": {"thread_id": thread_id}},
            )
        reply = self._output_guard.mask(str(result["messages"][-1].content))

        if use_cache and self._cache is not None:
            await self._cache.set(
                message, CachedResponse(content=reply, model=self._model_name)
            )
        return reply

    async def ask(self, message: str, *, principal: Principal) -> str:
        """**无状态**单轮问答：每次用独立 thread，问完即清理。

        这是知识助手最常用的形态（一问一答），也是唯一能安全开语义缓存的形态：
        没有历史上下文，同义问题命中缓存不会导致答非所问。用完立即删除该 thread 的
        checkpoint，避免「一问一答」把 checkpoint 表撑爆（这是无状态接口最容易漏的运维债）。
        """
        thread_id = f"ask-{uuid.uuid4().hex}"
        try:
            return await self.chat(
                message, thread_id=thread_id, principal=principal, use_cache=True
            )
        finally:
            self._drop_thread(thread_id)

    def _drop_thread(self, thread_id: str) -> None:
        """尽力删除 thread 的 checkpoint（后端不支持时静默跳过）。"""
        checkpointer = getattr(self._graph, "checkpointer", None)
        deleter = getattr(checkpointer, "delete_thread", None)
        if not callable(deleter):
            return
        try:
            deleter(thread_id)
        except Exception:
            return

    async def stream_events(
        self, message: str, *, thread_id: str = "default", principal: Principal
    ) -> AsyncIterator[dict[str, Any]]:
        """流式产出**结构化事件**：推理 / 正文 / 工具调用 / 工具结果。

        为什么不能只吐正文：deepseek-flash 这类推理模型会先产出 ``reasoning_content``
        再产出正文。若只转发正文，用户在「思考的十几秒」里看到的是空白页面，
        会误以为服务挂了。把推理单独成事件，前端才能显示「正在思考」并折叠展示。

        工具事件同理：一个 agent 花 8 秒调工具，用户需要知道它在做什么，
        否则体验等同于卡死。事件类型：
        - ``reasoning`` 推理片段（可折叠）
        - ``delta``     正文片段
        - ``tool_call`` 发起工具调用（含名称与参数）
        - ``tool_result`` 工具返回（截断到 2KB，避免撑爆 SSE）
        """
        check = self._input_guard.check(message)
        if not check.allowed:
            raise GuardViolation(check.reason)
        self._check_budget()
        with principal_context(principal):
            async for chunk, _metadata in self._graph.astream(
                {"messages": [HumanMessage(content=message)]},
                config={"configurable": {"thread_id": thread_id}},
                stream_mode="messages",
            ):
                if isinstance(chunk, ToolMessage):
                    yield {
                        "type": "tool_result",
                        "name": chunk.name or "tool",
                        "text": str(chunk.content)[:2000],
                    }
                    continue
                if not isinstance(chunk, (AIMessage, AIMessageChunk)):
                    continue
                reasoning = chunk.additional_kwargs.get("reasoning_content")
                if isinstance(reasoning, str) and reasoning:
                    yield {"type": "reasoning", "text": reasoning}
                for call in getattr(chunk, "tool_call_chunks", None) or []:
                    if call.get("name"):
                        yield {
                            "type": "tool_call",
                            "name": call["name"],
                            "args": call.get("args") or "",
                        }
                text = chunk.content
                if isinstance(text, str) and text:
                    yield {"type": "delta", "text": text}

    async def stream(
        self, message: str, *, thread_id: str = "default", principal: Principal
    ) -> AsyncIterator[str]:
        """流式产出回复正文（token 级）。推理与工具事件被丢弃，见 ``stream_events``。

        安全权衡：流式场景下，输出脱敏无法对「已发送的分块」撤回，
        因此这里**只做输入护栏**，输出脱敏不逐块应用，分块是原始文本。敏感内容的
        脱敏由非流式 /v1/chat 与审计落库兜底；生产上可在流式缓冲上做「延迟窗口脱敏」，
        以「晚一点、但完整」换取安全。这是流式体验与安全的经典取舍。
        """
        async for event in self.stream_events(
            message, thread_id=thread_id, principal=principal
        ):
            if event["type"] == "delta":
                yield event["text"]


def build_agent(
    settings: ModelSettings,
    gateway: Gateway,
    *,
    model: BaseChatModel | None = None,
    cost_tracker: CostTracker | None = None,
    input_guard: InputGuard | None = None,
    output_guard: OutputGuard | None = None,
    budget: BudgetPolicy | None = None,
    checkpointer: Any | None = None,
    cache: ResponseCache | None = None,
) -> Agent:
    """组装 Agent：默认用 DeepSeek，测试可注入假模型。护栏默认开启。

    预算：显式传入 budget 优先；否则若 settings 配置了 cost_limit / token_limit，
    自动构造 BudgetPolicy。预算需配合 cost_tracker（记账）才生效。

    checkpointer：调用方传入（serve.py 按 ``RuntimeSettings`` 选择 sqlite/postgres）。
    不传时退化到进程内存，只适用于单测。
    """
    model = model or build_model(settings, cost_tracker=cost_tracker)
    tools = build_tools(gateway)
    saver = checkpointer if checkpointer is not None else build_checkpointer("memory")
    graph = create_agent(model, tools, checkpointer=saver)
    if budget is None and (settings.cost_limit > 0 or settings.token_limit > 0):
        budget = BudgetPolicy(
            cost_limit=settings.cost_limit, token_limit=settings.token_limit
        )
    return Agent(
        graph,
        input_guard=input_guard,
        output_guard=output_guard,
        budget=budget,
        cost_tracker=cost_tracker,
        cache=cache,
        model_name=settings.model,
    )
