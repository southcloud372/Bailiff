"""离线模型：没有 API Key 时让整条链路仍然可跑。

不联网、不计费、输出确定，用于本地开发、CI 与演示。它实现了 bind_tools 和流式接口，
所以 SSE、指标、审计、计费走的都是真实代码路径，只有推理是假的。

只用于演示与测试。生产应设 AGENT_LLM_BACKEND=deepseek，缺 key 会直接报错；
默认的 auto 会降级到本模型，并由 /readyz 报 degraded。
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

_DEMO_REPLY = (
    "当前运行在**离线演示模式**（未配置模型 API Key），"
    "所以我不会真的推理，只走完整链路："
    "鉴权 → 限流 → 输入护栏 → 图编排 → SSE 流式 → 成本记账 → 指标与审计。\n\n"
    "你刚才问的是：「{question}」\n\n"
    "要接入真模型，在 .env 里设置：\n"
    "  DEEPSEEK_API_KEY=sk-...\n"
    "  AGENT_LLM_BACKEND=deepseek\n"
    "然后重启即可，其余代码一行都不用改。"
)


class MockChatModel(BaseChatModel):
    """确定性的离线模型：无网络、无费用、支持流式与工具绑定。"""

    model_name: str = "mock-demo"
    """模型名（会出现在 trace / 成本表里，便于识别演示流量）。"""

    chunk_size: int = 3
    """每次流式吐出的字符数。压小一点是为了让前端看起来像真流式。"""

    @property
    def _llm_type(self) -> str:
        return "mock-demo"

    def bind_tools(
        self, tools: Sequence[Any], **kwargs: Any
    ) -> MockChatModel:
        """接受工具绑定但从不调用：演示模式不产生副作用。"""
        return self

    # ── 内部：构造回复 ────────────────────────────────────────────────
    @staticmethod
    def _question(messages: list[BaseMessage]) -> str:
        for msg in reversed(messages):
            if msg.type == "human":
                return str(msg.content)
        return "（空输入）"

    def _reply(self, messages: list[BaseMessage]) -> str:
        return _DEMO_REPLY.format(question=self._question(messages)[:200])

    @staticmethod
    def _with_usage(message: AIMessage) -> AIMessage:
        """伪造 usage：让成本回调、指标、/v1/cost 都有数据可算。

        token 数按字符数粗估（中文约 1 字 1 token），仅用于演示账面是否打通。
        """
        text = str(message.content)
        message.usage_metadata = {
            "input_tokens": 42,
            "output_tokens": max(1, len(text) // 2),
            "total_tokens": 42 + max(1, len(text) // 2),
        }
        return message

    # ── BaseChatModel 必须实现的两个钩子 ──────────────────────────────
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        msg = self._with_usage(AIMessage(content=self._reply(messages)))
        return ChatResult(generations=[ChatGeneration(message=msg)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        text = self._reply(messages)
        for i in range(0, len(text), self.chunk_size):
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(content=text[i : i + self.chunk_size])
            )
            if run_manager is not None:
                run_manager.on_llm_new_token(str(chunk.text), chunk=chunk)
            yield chunk

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        """异步流式：逐块 ``yield`` 并让出事件循环，避免占满单线程。"""
        text = self._reply(messages)
        for i in range(0, len(text), self.chunk_size):
            await asyncio.sleep(0.01)  # 模拟网络间隔，让前端能看出是流式
            yield ChatGenerationChunk(
                message=AIMessageChunk(content=text[i : i + self.chunk_size])
            )
