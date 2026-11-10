"""DeepSeek 思考模式适配：让「推理内容」在工具调用链中正确回传。

## 问题

DeepSeek V4.1-Flash 是**思考模型**：响应里除了 ``content`` 还有 ``reasoning_content``。
它的 API 有一条硬约束：

> 带 ``tool_calls`` 的 assistant 消息，在下一轮请求中**必须**把 ``reasoning_content``
> 一起传回；缺失直接 400 ``invalid_request_error``。

而 LangChain 的 ``ChatOpenAI._convert_message_to_dict`` 只映射白名单字段，
``AIMessage.additional_kwargs["reasoning_content"]`` 会被静默丢弃。

结果是一个非常隐蔽的故障：**单轮问答正常，只要 Agent 调一次工具，第二轮必崩。**
在纯对话 demo 里永远测不出来，只有真实多步任务才会暴露。

## 解法

覆写 ``_get_request_payload``：在标准 payload 生成之后，把 ``reasoning_content``
补回对应的 assistant 消息。这不是「绕过 SDK」，而是补上 SDK 尚未覆盖的供应商扩展字段
（OpenAI 兼容协议的常见形态）。

对非思考模型（如 deepseek-chat）无副作用：它们根本不返回 ``reasoning_content``，
该分支不会命中。
"""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

logger = logging.getLogger(__name__)

# 历史 checkpoint 可能缺少 reasoning_content（例如升级前写入的状态）。
# 此时补一个占位串，让请求合法而不是整轮 400——丢掉的是「思考过程」，
# 保住的是「已经执行过的工具调用」这条事实链。
_MISSING_REASONING = "（该轮推理内容未记录）"


class DeepSeekChatModel(ChatOpenAI):
    """在 OpenAI 兼容协议之上，补回 DeepSeek 思考模式要求的 ``reasoning_content``。"""

    def _get_request_payload(
        self,
        input_: Any,
        *,
        stop: list[str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        raw = payload.get("messages")
        if not isinstance(raw, list) or len(raw) != len(input_):
            return payload

        for source, target in zip(input_, raw, strict=True):
            if not isinstance(source, AIMessage) or target.get("role") != "assistant":
                continue
            reasoning = (source.additional_kwargs or {}).get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                target["reasoning_content"] = reasoning
            elif target.get("tool_calls"):
                target["reasoning_content"] = _MISSING_REASONING
                logger.debug("reasoning_content_missing_for_tool_call")

        payload["messages"] = _repair_dangling_tool_calls(raw)
        return payload


def _repair_dangling_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """补齐「有 tool_calls 但缺 tool 响应」的历史，避免整条线程永久 400。

    什么时候会出现缺口：
    - 工具执行时进程崩溃 / 请求被客户端断开，state 已写入 checkpoint 但工具没跑完；
    - 早期版本把工具异常直接上抛，导致 ToolNode 中途中止；
    - 人工编辑或迁移过历史数据。

    OpenAI 兼容 API 对此是**硬校验**：``An assistant message with 'tool_calls' must be
    followed by tool messages responding to each 'tool_call_id'``。一旦命中，这条会话
    后续每一轮都失败，且报错与真实原因毫无关系——是线上最典型的「线程被写坏」故障。

    修复策略：为每个缺失的 ``tool_call_id`` 补一条内容为说明的 tool 消息。
    对模型而言这是「那次调用没有结果」，语义真实；对协议而言历史重新合法。
    """
    repaired: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        repaired.append(message)
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue

        expected = [
            call.get("id") for call in message["tool_calls"] if call.get("id")
        ]
        if not expected:
            continue

        # 紧跟其后的连续 tool 消息即为本轮的响应
        cursor = index + 1
        answered: set[str] = set()
        while cursor < len(messages) and messages[cursor].get("role") == "tool":
            answered.add(str(messages[cursor].get("tool_call_id")))
            cursor += 1

        missing = [call_id for call_id in expected if call_id not in answered]
        if not missing:
            continue
        logger.warning(
            "tool_call_history_repaired", extra={"missing": len(missing)}
        )
        repaired.extend(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": "（该工具调用没有返回结果，可能被中断或已降级）",
            }
            for call_id in missing
        )
    return repaired

