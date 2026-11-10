"""请求级上下文：principal / request_id / 工具事件汇（contextvars）。

为什么用 ``contextvars`` 而不是显式传参？
- 工具执行发生在 LangGraph 图内部（框架回调里），函数签名由框架决定，塞不进身份参数；
- ``contextvars`` 随 asyncio task 浅拷贝，天然按请求隔离，不会跨请求串号。

这个模块刻意不依赖 langchain / fastapi，成为「最底层」的共享状态：
gateway、router、core、api 都可以导入它而不产生循环依赖。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any

from agent_platform.gateway.policy import Principal

#: 当前请求的调用方身份（JWT 解出的 Principal）。
current_principal: ContextVar[Principal | None] = ContextVar(
    "agent_principal", default=None
)

#: 当前请求 id（用于日志关联与响应头回显）。
current_request_id: ContextVar[str | None] = ContextVar(
    "agent_request_id", default=None
)

#: 工具事件回调：流式场景由 core 层注册，把 tool_call / tool_result 推给 SSE。
ToolEventSink = Callable[[str, dict[str, Any]], None]
current_tool_sink: ContextVar[ToolEventSink | None] = ContextVar(
    "agent_tool_sink", default=None
)

#: 会话/线程 id：让审计、成本、trace 都能落到「哪一轮对话」。
current_thread_id: ContextVar[str | None] = ContextVar(
    "agent_thread_id", default=None
)


@contextmanager
def principal_context(principal: Principal) -> Iterator[None]:
    """在上下文内设置当前调用方身份，供工具执行时解析。"""
    token: Token[Principal | None] = current_principal.set(principal)
    try:
        yield
    finally:
        current_principal.reset(token)


@contextmanager
def request_context(request_id: str) -> Iterator[None]:
    """在上下文内设置当前请求 id。"""
    token = current_request_id.set(request_id)
    try:
        yield
    finally:
        current_request_id.reset(token)


@contextmanager
def thread_context(thread_id: str) -> Iterator[None]:
    """在上下文内设置当前会话 id。"""
    token = current_thread_id.set(thread_id)
    try:
        yield
    finally:
        current_thread_id.reset(token)


@contextmanager
def tool_event_context(sink: ToolEventSink) -> Iterator[None]:
    """在上下文内注册工具事件汇（仅流式场景使用）。"""
    token = current_tool_sink.set(sink)
    try:
        yield
    finally:
        current_tool_sink.reset(token)


def emit_tool_event(kind: str, payload: dict[str, Any]) -> None:
    """把工具事件推给已注册的汇；未注册时是空操作（零开销降级）。"""
    sink = current_tool_sink.get()
    if sink is not None:
        sink(kind, payload)
