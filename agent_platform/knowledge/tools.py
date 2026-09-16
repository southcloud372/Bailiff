"""把知识检索与长期记忆暴露成网关工具（带调用方身份）。

工具函数统一走网关签名 ``(principal, args) -> result``，身份用于记忆分域；
组装方把这些工具注册进网关（``build_platform(extra_tools=...)``），从而让
知识/记忆调用同样经过鉴权、限流与审计。
"""

from __future__ import annotations

from typing import Any

from agent_platform.gateway.policy import Principal
from agent_platform.gateway.registry import ToolFn
from agent_platform.knowledge.rag import RAGPipeline
from agent_platform.memory.service import MemoryService


def build_knowledge_tools(
    rag: RAGPipeline, memory: MemoryService
) -> dict[str, ToolFn]:
    """返回 search_knowledge / remember / recall 三个网关工具。"""

    async def search_knowledge(principal: Principal, args: dict[str, Any]) -> str:
        hits = await rag.search(str(args["query"]))
        return rag.format_context(hits)

    async def remember(principal: Principal, args: dict[str, Any]) -> str:
        record_id = await memory.remember(str(args["content"]), user_id=principal.id)
        return f"已写入长期记忆: {record_id}"

    async def recall(principal: Principal, args: dict[str, Any]) -> str:
        hits = await memory.recall(str(args["query"]), user_id=principal.id)
        return rag.format_context(hits)

    return {
        "search_knowledge": search_knowledge,
        "remember": remember,
        "recall": recall,
    }
