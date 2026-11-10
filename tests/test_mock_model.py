"""离线演示模型与后端选择的测试。

演示模式是开源项目的「第一印象」：没有 key 也必须能跑。
这里锁住两点：缺 key 不会崩、显式要求真模型时不会静默降级。
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from agent_platform.core.agent import build_model, resolve_model_backend
from agent_platform.core.mock import MockChatModel
from agent_platform.gateway.config import ModelSettings


def test_auto_without_key_falls_back_to_mock() -> None:
    assert resolve_model_backend(ModelSettings(api_key="")) == "mock"


def test_auto_with_key_uses_deepseek() -> None:
    assert resolve_model_backend(ModelSettings(api_key="sk-x")) == "deepseek"


def test_explicit_deepseek_without_key_raises() -> None:
    """生产必须显式失败，不能悄悄用假模型回答用户。"""
    with pytest.raises(RuntimeError):
        resolve_model_backend(ModelSettings(api_key="", llm_backend="deepseek"))


def test_explicit_mock_wins_over_key() -> None:
    assert resolve_model_backend(ModelSettings(api_key="sk-x", llm_backend="mock")) == "mock"


def test_build_model_returns_mock_without_key() -> None:
    assert isinstance(build_model(ModelSettings(api_key="")), MockChatModel)


@pytest.mark.asyncio
async def test_mock_streams_and_reports_usage() -> None:
    """演示模型也要逐块流式并带 usage，否则成本与指标链路是空的。"""
    model = MockChatModel()
    chunks = [chunk async for chunk in model.astream([HumanMessage(content="你好")])]
    assert len(chunks) > 1
    text = "".join(str(c.content) for c in chunks)
    assert "你好" in text  # 回显用户问题，便于演示时确认链路通

    reply = await model.ainvoke([HumanMessage(content="你好")])
    assert isinstance(reply, AIMessage)
    assert reply.usage_metadata is not None
    assert reply.usage_metadata["output_tokens"] > 0


def test_mock_never_binds_real_tools() -> None:
    """bind_tools 必须存在（否则 create_agent 无法构图），但不应真的调用。"""
    model = MockChatModel()
    assert model.bind_tools([object()]) is model
