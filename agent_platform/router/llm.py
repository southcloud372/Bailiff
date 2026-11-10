"""模型路由：统一模型调用入口，支持多模型 fallback 与用量聚合。

企业级 Agent 平台的模型层要点：
1. 不裸调某一家 API，而是走 LiteLLM 统一路由（多供应商可替换、可降级）；
2. 主模型失败自动降级到候选模型（fallback）；
3. 每次调用都返回用量与「实际服务的模型」，为成本与可观测留钩子。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import litellm
from pydantic import BaseModel

from agent_platform.gateway.config import ModelSettings
from agent_platform.router.cost import CostTracker, estimate_cost

logger = logging.getLogger(__name__)

CompletionFn = Callable[..., Awaitable[Any]]


class ModelRoutingError(Exception):
    """所有候选模型均调用失败。"""


class LLMResult(BaseModel):
    """一次模型调用的结果与用量。"""

    model: str
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    cost: float = 0.0


class ModelRouter:
    """按顺序尝试候选模型，成功即返回；全失败则抛 ModelRoutingError。"""

    def __init__(
        self,
        models: Sequence[str],
        *,
        api_key: str,
        base_url: str,
        completion: CompletionFn = litellm.acompletion,
        cost_tracker: CostTracker | None = None,
    ) -> None:
        if not models:
            raise ModelRoutingError("未配置任何候选模型")
        self._models = list(models)
        self._api_key = api_key
        self._base_url = base_url
        self._completion = completion
        self._cost_tracker = cost_tracker

    async def complete(self, messages: list[dict[str, Any]], **kwargs: Any) -> LLMResult:
        last_exc: Exception | None = None
        for model in self._models:
            try:
                return await self._call_one(model, messages, **kwargs)
            except Exception as exc:  # 路由需吞掉单模型失败以尝试 fallback
                last_exc = exc
                logger.warning("模型 %s 调用失败，尝试下一候选: %s", model, exc)
        raise ModelRoutingError(f"所有候选模型均失败: {list(self._models)}") from last_exc

    async def _call_one(
        self, model: str, messages: list[dict[str, Any]], **kwargs: Any
    ) -> LLMResult:
        resp = await self._completion(
            model=f"openai/{model}",
            messages=messages,
            api_key=self._api_key,
            base_url=self._base_url,
            **kwargs,
        )
        choice = resp.choices[0].message
        content = (choice.content or "").strip()
        usage = resp.usage
        prompt_tokens = int(usage.prompt_tokens or 0) if usage else 0
        completion_tokens = int(usage.completion_tokens or 0) if usage else 0
        cached_tokens = 0
        if usage is not None:
            details = getattr(usage, "prompt_tokens_details", None)
            if details is not None:
                cached_tokens = int(getattr(details, "cached_tokens", 0) or 0)
        cost = estimate_cost(model, prompt_tokens, completion_tokens)
        if self._cost_tracker is not None:
            self._cost_tracker.record(
                model, prompt_tokens, completion_tokens, cost, cached_tokens
            )
        return LLMResult(
            model=resp.model or model,
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cached_tokens=cached_tokens,
            cost=cost,
        )


def build_router(settings: ModelSettings | None = None) -> ModelRouter:
    """从配置组装模型路由。未配 API key 直接失败（快速暴露配置问题）。"""
    settings = settings or ModelSettings()
    if not settings.api_key:
        raise ModelRoutingError("未配置 DEEPSEEK_API_KEY")
    return ModelRouter(
        [settings.model],
        api_key=settings.api_key,
        base_url=settings.base_url,
    )
