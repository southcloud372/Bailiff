"""Agent 事件模型：把「一次对话里发生了什么」变成可流式消费的有类型事件。

SSE 只传字符串，但企业前端需要区分「模型吐字」「工具开始执行」「工具返回」
「等待人工审批」「出错」这些语义不同的东西，否则前端只能看到一串文本，无法
渲染工具卡片、审批按钮或错误态。

``AgentEvent`` 就是这层契约：core 层产出、api 层编码成 SSE、前端按 ``type`` 分支。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    "meta",        # 会话建立：thread_id / request_id
    "token",       # 模型增量输出
    "tool_call",   # 工具开始执行（含参数）
    "tool_result", # 工具执行结束（含结论与耗时）
    "interrupt",   # HITL：图暂停，等待审批
    "error",       # 可恢复错误（护栏/预算/模型）
    "done",        # 本轮结束（含 token / 成本汇总）
]


class AgentEvent(BaseModel):
    """一条流式事件。``data`` 的 schema 由 ``type`` 决定。"""

    type: EventType
    data: dict[str, Any] = Field(default_factory=dict)


def meta_event(thread_id: str, request_id: str | None = None) -> AgentEvent:
    return AgentEvent(type="meta", data={"thread_id": thread_id, "request_id": request_id})


def token_event(text: str) -> AgentEvent:
    return AgentEvent(type="token", data={"delta": text})


def tool_call_event(tool: str, arguments: dict[str, Any]) -> AgentEvent:
    return AgentEvent(type="tool_call", data={"tool": tool, "arguments": arguments})


def tool_result_event(
    tool: str, *, outcome: str, duration_ms: float, preview: str
) -> AgentEvent:
    return AgentEvent(
        type="tool_result",
        data={
            "tool": tool,
            "outcome": outcome,
            "duration_ms": round(duration_ms, 2),
            "preview": preview,
        },
    )


def interrupt_event(payload: dict[str, Any]) -> AgentEvent:
    return AgentEvent(type="interrupt", data=payload)


def error_event(code: str, message: str) -> AgentEvent:
    return AgentEvent(type="error", data={"code": code, "message": message})


def done_event(**data: Any) -> AgentEvent:
    return AgentEvent(type="done", data=data)
