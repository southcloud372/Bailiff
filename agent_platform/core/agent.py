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
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph.state import CompiledStateGraph
from pydantic import BaseModel, Field, SecretStr

from agent_platform.gateway.config import ModelSettings
from agent_platform.gateway.gateway import Gateway
from agent_platform.gateway.policy import Principal
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


def build_model(
    settings: ModelSettings, *, cost_tracker: CostTracker | None = None
) -> ChatOpenAI:
    """构建 LangChain 聊天模型（OpenAI 兼容 → DeepSeek）。

    传入 cost_tracker 时挂上计费回调，所有模型调用都会累计 token 与成本。
    """
    callbacks: list[BaseCallbackHandler] | None = (
        [CostCallbackHandler(cost_tracker)] if cost_tracker is not None else None
    )
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

    return [
        _tool("read_file", "读取根目录内的文本文件内容", ReadFileArgs),
        _tool("write_file", "写入文本到根目录内的文件", WriteFileArgs),
        _tool("list_dir", "列出根目录内某个目录的内容", ListDirArgs),
    ]


class Agent:
    """对编译好的 LangGraph agent 的高层封装。"""

    def __init__(self, graph: CompiledStateGraph[Any, Any, Any, Any]) -> None:
        self._graph = graph

    async def chat(
        self, message: str, *, thread_id: str = "default", principal: Principal
    ) -> str:
        """以指定身份运行一轮对话，返回最终回复文本。"""
        with principal_context(principal):
            result = await self._graph.ainvoke(
                {"messages": [HumanMessage(content=message)]},
                config={"configurable": {"thread_id": thread_id}},
            )
        last = result["messages"][-1]
        return str(last.content)


def build_agent(
    settings: ModelSettings,
    gateway: Gateway,
    *,
    model: BaseChatModel | None = None,
    cost_tracker: CostTracker | None = None,
) -> Agent:
    """组装 Agent：默认用 DeepSeek，测试可注入假模型。"""
    model = model or build_model(settings, cost_tracker=cost_tracker)
    tools = build_tools(gateway)
    graph = create_agent(model, tools, checkpointer=MemorySaver())
    return Agent(graph)
