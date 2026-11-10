"""成本追踪：聚合每次模型调用的 token 用量与费用。

生产级平台的「token 经济学」，让每次模型调用都可量化成本，为成本限额、模型选型
（大模型规划 / 小模型抽取）与可观测提供数据。费用估算走 LiteLLM 价格表；报价缺失
或模型未知时按 0 计，**不因计费失败而中断主链路**（计费是旁路，主流程优先）。

prompt caching：DeepSeek 上下文命中时返回 ``cached_tokens``，这里单独记录并计算
命中率（缓存命中 token / 总 prompt token），用于观察缓存带来的成本下探，不臆造缓存单价。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import litellm
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from pydantic import BaseModel, Field

from agent_platform.context import current_principal

logger = logging.getLogger(__name__)


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """估算一次调用的费用（元）。报价缺失/模型未知时返回 0.0。

    LiteLLM 的 `cost_per_token` 需要 provider 前缀（如 `deepseek/deepseek-flash`），
    而我们的模型名多为裸名（`deepseek-flash`）或响应别名（`deepseek-flash`），
    这里按需补 `deepseek/` 前缀再查询。
    """
    candidates = [model]
    if "deepseek" in model and "/" not in model:
        candidates.append(f"deepseek/{model}")
    for candidate in candidates:
        try:
            prompt_cost, completion_cost = litellm.cost_per_token(
                model=candidate,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
            return prompt_cost + completion_cost
        except Exception as exc:
            # 不要静默：价格表过期会导致成本恒为 0，是最难发现的成本事故。
            logger.debug(
                "cost_lookup_failed", extra={"model": candidate, "error": str(exc)}
            )
            continue
    return 0.0


class ModelCost(BaseModel):
    """单个模型的累计成本与用量。"""

    cost: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0


class CostSummary(BaseModel):
    """成本聚合视图（供 /v1/cost 返回）。"""

    total_cost: float = 0.0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    total_cached_tokens: int = 0
    cache_hit_rate: float = 0.0
    by_model: dict[str, ModelCost] = Field(default_factory=dict)


class _Bucket:
    """一个维度的累计值（全局或单个调用方）。"""

    __slots__ = ("by_model", "cached_tokens", "completion_tokens", "cost", "prompt_tokens")

    def __init__(self) -> None:
        self.cost = 0.0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.cached_tokens = 0
        self.by_model: dict[str, ModelCost] = {}

    def add(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: float,
        cached_tokens: int,
    ) -> None:
        self.cost += cost
        self.prompt_tokens += prompt_tokens
        self.completion_tokens += completion_tokens
        self.cached_tokens += cached_tokens
        entry = self.by_model.setdefault(model, ModelCost())
        entry.cost += cost
        entry.prompt_tokens += prompt_tokens
        entry.completion_tokens += completion_tokens
        entry.cached_tokens += cached_tokens

    def merge_into(self, other: _Bucket) -> None:
        other.cost += self.cost
        other.prompt_tokens += self.prompt_tokens
        other.completion_tokens += self.completion_tokens
        other.cached_tokens += self.cached_tokens
        for name, entry in self.by_model.items():
            target = other.by_model.setdefault(name, ModelCost())
            target.cost += entry.cost
            target.prompt_tokens += entry.prompt_tokens
            target.completion_tokens += entry.completion_tokens
            target.cached_tokens += entry.cached_tokens

    def to_summary(self) -> CostSummary:
        hit_rate = (
            round(self.cached_tokens / self.prompt_tokens, 4) if self.prompt_tokens else 0.0
        )
        return CostSummary(
            total_cost=self.cost,
            total_prompt_tokens=self.prompt_tokens,
            total_completion_tokens=self.completion_tokens,
            total_cached_tokens=self.cached_tokens,
            cache_hit_rate=hit_rate,
            by_model={k: v.model_copy() for k, v in self.by_model.items()},
        )


class CostTracker:
    """线程安全的成本累加器，按调用方分桶。

    分桶不是可选项：只有全局账本时，任何登录用户查询 /v1/cost 都能看到所有人的
    累计花费，多租户下这是数据泄露。全局汇总仅保留给内部与运维使用。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._buckets: dict[str, _Bucket] = {}

    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost: float,
        cached_tokens: int = 0,
        principal_id: str = "",
    ) -> None:
        with self._lock:
            bucket = self._buckets.setdefault(principal_id, _Bucket())
            bucket.add(model, prompt_tokens, completion_tokens, cost, cached_tokens)

    def summary(self, principal_id: str | None = None) -> CostSummary:
        """传 principal_id 返回该调用方的账本；为空返回全局汇总。"""
        with self._lock:
            if principal_id is not None:
                return (self._buckets.get(principal_id) or _Bucket()).to_summary()
            merged = _Bucket()
            for bucket in self._buckets.values():
                bucket.merge_into(merged)
            return merged.to_summary()


class CostCallbackHandler(BaseCallbackHandler):
    """LangChain 回调：在每次 LLM 调用结束时记录用量与成本。

    挂到 ChatOpenAI 的 callbacks 上，Agent/工作流里所有模型调用都会被计费。
    """

    def __init__(self, tracker: CostTracker, principal_id: str | None = None) -> None:
        self._tracker = tracker
        # 显式传入优先。不要只依赖 contextvar：LangChain 的同步回调可能在其他
        # 执行上下文里被调用，contextvar 在那里不可见，成本会被记到空桶上。
        self._principal_id = principal_id

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        llm_output = response.llm_output or {}
        usage = llm_output.get("token_usage")
        if not usage:
            return
        model = str(llm_output.get("model_name") or llm_output.get("model") or "unknown")
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        cached_tokens = self._cached_tokens_from(usage)
        principal_id = self._principal_id
        if principal_id is None:
            principal = current_principal.get()
            principal_id = principal.id if principal is not None else ""
        self._tracker.record(
            model,
            prompt_tokens,
            completion_tokens,
            estimate_cost(model, prompt_tokens, completion_tokens),
            cached_tokens,
            principal_id=principal_id,
        )

    @staticmethod
    def _cached_tokens_from(usage: dict[str, Any]) -> int:
        """从 token_usage 提取缓存命中 token（``prompt_tokens_details.cached_tokens``）。"""
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict):
            return int(details.get("cached_tokens") or 0)
        return 0
