"""DeepSeek 思考模式适配的回归测试。

这两条约束都是「只在真实多步任务里才暴露」的类型：
1. 带 tool_calls 的 assistant 消息必须回传 reasoning_content；
2. 每个 tool_call_id 必须有对应的 tool 消息。

任一不满足 → API 400，且会话线程会被写坏，之后每轮都失败。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent_platform.router.deepseek import DeepSeekChatModel


def _model() -> DeepSeekChatModel:
    return DeepSeekChatModel(model="deepseek-flash", api_key="sk-test", base_url="https://x")


def _payload(messages: list[Any]) -> list[dict[str, Any]]:
    return _model()._get_request_payload(messages)["messages"]


def _assistant_with_call(reasoning: str | None = None) -> AIMessage:
    kwargs: dict[str, Any] = {}
    if reasoning is not None:
        kwargs["reasoning_content"] = reasoning
    return AIMessage(
        content="",
        additional_kwargs=kwargs,
        tool_calls=[{"id": "call_1", "name": "read_file", "args": {"path": "a.txt"}}],
    )


def test_reasoning_content_is_passed_back_for_tool_call() -> None:
    """思考模型的推理内容必须回传给 API，否则第二轮 400。"""
    msgs = [
        HumanMessage(content="读文件"),
        _assistant_with_call("我需要先读取文件"),
        ToolMessage(content="hello", tool_call_id="call_1"),
    ]
    tool_msg = _payload(msgs)[1]
    assert tool_msg["role"] == "assistant"
    assert tool_msg["reasoning_content"] == "我需要先读取文件"


def test_missing_reasoning_gets_placeholder() -> None:
    """历史状态缺 reasoning_content 时补占位串，保住已发生的工具调用事实。"""
    msgs = [
        HumanMessage(content="读文件"),
        _assistant_with_call(None),
        ToolMessage(content="hello", tool_call_id="call_1"),
    ]
    assert _payload(msgs)[1]["reasoning_content"]


def test_plain_assistant_message_untouched() -> None:
    """没有 tool_calls 的普通回复不需要 reasoning_content（避免污染上下文）。"""
    msgs = [HumanMessage(content="你好"), AIMessage(content="你好呀")]
    payload = _payload(msgs)
    assert "reasoning_content" not in payload[1]


def test_dangling_tool_call_is_repaired() -> None:
    """缺 tool 响应的历史会被补齐，让被写坏的线程自愈。"""
    msgs = [
        HumanMessage(content="读文件"),
        _assistant_with_call("推理"),
        # 注意：没有对应的 ToolMessage —— 模拟工具执行时进程崩溃
        HumanMessage(content="继续"),
    ]
    payload = _payload(msgs)
    roles = [m["role"] for m in payload]
    assert roles == ["user", "assistant", "tool", "user"]
    assert payload[2]["tool_call_id"] == "call_1"
    assert payload[2]["content"]


def test_complete_history_is_not_repaired() -> None:
    """历史完整时不应插入任何消息。"""
    msgs = [
        HumanMessage(content="读文件"),
        _assistant_with_call("推理"),
        ToolMessage(content="hello", tool_call_id="call_1"),
        HumanMessage(content="谢谢"),
    ]
    assert [m["role"] for m in _payload(msgs)] == ["user", "assistant", "tool", "user"]


def test_parallel_tool_calls_all_repaired() -> None:
    """并行工具调用只回了一条结果时，其余 id 也要补上。"""
    assistant = AIMessage(
        content="",
        additional_kwargs={"reasoning_content": "并行读两个文件"},
        tool_calls=[
            {"id": "call_a", "name": "read_file", "args": {"path": "a.txt"}},
            {"id": "call_b", "name": "read_file", "args": {"path": "b.txt"}},
        ],
    )
    msgs = [
        HumanMessage(content="读两个文件"),
        assistant,
        ToolMessage(content="A", tool_call_id="call_a"),
        HumanMessage(content="继续"),
    ]
    payload = _payload(msgs)
    tool_ids = [m.get("tool_call_id") for m in payload if m["role"] == "tool"]
    assert set(tool_ids) == {"call_a", "call_b"}
