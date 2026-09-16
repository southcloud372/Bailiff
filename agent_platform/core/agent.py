"""LangGraph 编排层：ReAct Agent（模型 + 网关工具闭环）。

模型用 LangChain ChatOpenAI 对接 DeepSeek（OpenAI 兼容），工具统一走 Gateway——
每一次工具调用都经过鉴权/权限/限流/审计，治理不旁路。

注：模型路由（router/llm.py 的 LiteLLM ModelRouter）是更低一层的「多供应商路由/
降级/成本」抽象；这里先用 LangChain 原生 chat model 以获得 tool-calling 集成，
两者共享 ModelSettings。后续可把 Agent 的模型也接到 ModelRouter（ChatLiteLLM 或
自定义 BaseChatModel）以复用降级能力。
"""

from __future__ import annotations

from typing import Any

from langchain.agents import create_agent
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


def build_model(settings: ModelSettings) -> ChatOpenAI:
    """构建 LangChain 聊天模型（OpenAI 兼容 → DeepSeek）。"""
    return ChatOpenAI(
        model=settings.model,
        api_key=SecretStr(settings.api_key),
        base_url=settings.base_url,
        temperature=0.3,
    )


def build_tools(gateway: Gateway, principal: Principal) -> list[StructuredTool]:
    """把网关里的工具包装成 LangChain 工具，供 Agent 调用。"""

    def _tool(name: str, description: str, schema: type[BaseModel]) -> StructuredTool:
        async def _run(**kwargs: Any) -> Any:
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

    async def chat(self, message: str, *, thread_id: str = "default") -> str:
        """按 thread_id 维护会话上下文，返回最终回复文本。"""
        result = await self._graph.ainvoke(
            {"messages": [HumanMessage(content=message)]},
            config={"configurable": {"thread_id": thread_id}},
        )
        last = result["messages"][-1]
        return str(last.content)


def build_agent(
    settings: ModelSettings,
    gateway: Gateway,
    principal: Principal,
    *,
    model: BaseChatModel | None = None,
) -> Agent:
    """组装 Agent：默认用 DeepSeek，测试可注入假模型。"""
    model = model or build_model(settings)
    tools = build_tools(gateway, principal)
    graph = create_agent(model, tools, checkpointer=MemorySaver())
    return Agent(graph)
