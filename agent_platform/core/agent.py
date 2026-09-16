"""LangGraph 编排层：ReAct Agent（模型 + 网关工具闭环）。

模型用 LangChain ChatOpenAI 对接 DeepSeek（OpenAI 兼容），工具统一走 Gateway——
每一次工具调用都经过鉴权/权限/限流/审计，治理不旁路。

身份传递：工具执行时的「调用方身份」不写死在 Agent 上，而是通过 contextvar 按请求
解析——用户 JWT → Principal 贯穿整个 Agent 工具链，落到网关的权限检查。

注：模型路由（router/llm.py 的 LiteLLM ModelRouter）是更低一层的「多供应商路由/
降级/成本」抽象；这里先用 LangChain 原生 chat model 以获得 tool-calling 集成，
两者共享 ModelSettings。后续可把 Agent 的模型也接到 ModelRouter（ChatLiteLLM 或
自定义 BaseChatModel）以复用降级能力。
"""

from __future__ import annotations

import contextvars
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from typing import Any

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field, SecretStr

from agent_platform.gateway.config import ModelSettings
from agent_platform.gateway.gateway import Gateway
from agent_platform.gateway.policy import Principal
from agent_platform.guardrails.guards import (
    GuardViolation,
    InputGuard,
    OutputGuard,
    default_input_guard,
    default_output_guard,
)
from agent_platform.observability.tracing import TracingCallback
from agent_platform.router.budget import BudgetPolicy
from agent_platform.router.cost import CostCallbackHandler, CostTracker

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


def build_model(
    settings: ModelSettings, *, cost_tracker: CostTracker | None = None
) -> ChatOpenAI:
    """构建 LangChain 聊天模型（OpenAI 兼容 → DeepSeek）。

    挂两个回调：计费（累计 token 与成本）与可观测（OTel span，未配置时 no-op）。
    """
    callbacks: list[BaseCallbackHandler] = [TracingCallback()]
    if cost_tracker is not None:
        callbacks.append(CostCallbackHandler(cost_tracker))
    return ChatOpenAI(
        model=settings.model,
        api_key=SecretStr(settings.api_key),
        base_url=settings.base_url,
        temperature=0.3,
        callbacks=callbacks,
    )


def build_tools(gateway: Gateway) -> list[StructuredTool]:
    """把网关里的工具包装成 LangChain 工具，供 Agent 调用。"""

    def _tool(name: str, description: str, schema: type[BaseModel]) -> StructuredTool:
        async def _run(**kwargs: Any) -> Any:
            principal = current_principal.get()
            if principal is None:
                raise RuntimeError("工具执行时未设置 principal（需经 Agent.chat 进入）")
            return await gateway.call(principal, name, dict(kwargs))

        return StructuredTool.from_function(
            name=name,
            description=description,
            args_schema=schema,
            coroutine=_run,
        )

    tools = [
        _tool("read_file", "读取根目录内的文本文件内容", ReadFileArgs),
        _tool("write_file", "写入文本到根目录内的文件", WriteFileArgs),
        _tool("list_dir", "列出根目录内某个目录的内容", ListDirArgs),
    ]
    # 知识/记忆工具由网关注册（serve.py 组装），这里按注册情况动态暴露给 Agent。
    names = gateway.tool_names()
    if "search_knowledge" in names:
        tools.append(
            _tool("search_knowledge", "检索企业知识库，返回带来源的上下文片段", SearchKnowledgeArgs)
        )
    if "remember" in names:
        tools.append(_tool("remember", "把内容写入当前用户的长期记忆", RememberArgs))
    if "recall" in names:
        tools.append(_tool("recall", "按语义召回当前用户的长期记忆", RecallArgs))
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
    ) -> None:
        self._graph = graph
        self._input_guard = input_guard or default_input_guard
        self._output_guard = output_guard or default_output_guard
        self._budget = budget
        self._cost_tracker = cost_tracker

    def _check_budget(self) -> None:
        """模型调用前检查预算：仅在同时配置了预算与成本追踪时生效。"""
        if self._budget is not None and self._cost_tracker is not None:
            self._budget.check(self._cost_tracker)

    async def chat(
        self, message: str, *, thread_id: str = "default", principal: Principal
    ) -> str:
        """以指定身份运行一轮对话，返回最终回复文本。

        输入护栏挡在模型之前（拦截注入），输出护栏挡在返回之前（脱敏）。
        """
        check = self._input_guard.check(message)
        if not check.allowed:
            raise GuardViolation(check.reason)
        self._check_budget()
        with principal_context(principal):
            result = await self._graph.ainvoke(
                {"messages": [HumanMessage(content=message)]},
                config={"configurable": {"thread_id": thread_id}},
            )
        last = result["messages"][-1]
        return self._output_guard.mask(str(last.content))

    async def stream(
        self, message: str, *, thread_id: str = "default", principal: Principal
    ) -> AsyncIterator[str]:
        """以指定身份运行一轮对话，**流式**产出回复分块（token 级）。

        与 chat() 相同的输入护栏：疑似注入在模型调用前拦截（GuardViolation）。

        注意（安全权衡，面试可讲）：流式场景下，输出脱敏无法对「已发送的分块」撤回，
        因此这里**只做输入护栏**，输出脱敏不逐块应用——分块是原始文本。敏感内容的
        脱敏由非流式 /v1/chat 与审计落库兜底；生产上可在流式缓冲上做「延迟窗口脱敏」，
        以「晚一点、但完整」换取安全。这是流式体验与安全的经典取舍。
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
                # 只取 AI 消息的内容分块；工具调用分块（content 为空）与工具结果消息跳过。
                if isinstance(chunk, (AIMessage, AIMessageChunk)):
                    text = chunk.content
                    if isinstance(text, str) and text:
                        yield text


def build_agent(
    settings: ModelSettings,
    gateway: Gateway,
    *,
    model: BaseChatModel | None = None,
    cost_tracker: CostTracker | None = None,
    input_guard: InputGuard | None = None,
    output_guard: OutputGuard | None = None,
    budget: BudgetPolicy | None = None,
) -> Agent:
    """组装 Agent：默认用 DeepSeek，测试可注入假模型。护栏默认开启。

    预算：显式传入 budget 优先；否则若 settings 配置了 cost_limit / token_limit，
    自动构造 BudgetPolicy。预算需配合 cost_tracker（记账）才生效。
    """
    model = model or build_model(settings, cost_tracker=cost_tracker)
    tools = build_tools(gateway)
    graph = create_agent(model, tools, checkpointer=MemorySaver())
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
    )
