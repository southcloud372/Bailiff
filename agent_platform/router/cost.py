"""成本追踪：聚合每次模型调用的 token 用量与费用。

企业级平台的「token 经济学」——让每次模型调用都可量化成本，为成本限额、模型选型
（大模型规划 / 小模型抽取）与可观测提供数据。费用估算走 LiteLLM 价格表；报价缺失
或模型未知时按 0 计，**不因计费失败而中断主链路**（计费是旁路，主流程优先）。
"""

from __future__ import annotations

import threading
from typing import Any

import litellm
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from pydantic import BaseModel, Field


def estimate_cost(model: str, prompt_tokens: int, completion_tokens: int) -> float:
    """估算一次调用的费用（元）。报价缺失/模型未知时返回 0.0。

    LiteLLM 的 `cost_per_token` 需要 provider 前缀（如 `deepseek/deepseek-chat`），
    而我们的模型名多为裸名（`deepseek-chat`）或响应别名（`deepseek-flash`），
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
        except Exception:
            continue
    return 0.0


class ModelCost(BaseModel):
    """单个模型的累计成本与用量。"""

    cost: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0


class CostSummary(BaseModel):
    """成本聚合视图（供 /v1/cost 返回）。"""

    total_cost: float = 0.0
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    by_model: dict[str, ModelCost] = Field(default_factory=dict)


class CostTracker:
    """线程安全的成本累加器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._total_cost = 0.0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._by_model: dict[str, ModelCost] = {}

    def record(
        self, model: str, prompt_tokens: int, completion_tokens: int, cost: float
    ) -> None:
        with self._lock:
            self._total_cost += cost
            self._prompt_tokens += prompt_tokens
            self._completion_tokens += completion_tokens
            entry = self._by_model.setdefault(model, ModelCost())
            entry.cost += cost
            entry.prompt_tokens += prompt_tokens
            entry.completion_tokens += completion_tokens

    def summary(self) -> CostSummary:
        with self._lock:
            return CostSummary(
                total_cost=self._total_cost,
                total_prompt_tokens=self._prompt_tokens,
                total_completion_tokens=self._completion_tokens,
                by_model={k: v.model_copy() for k, v in self._by_model.items()},
            )


class CostCallbackHandler(BaseCallbackHandler):
    """LangChain 回调：在每次 LLM 调用结束时记录用量与成本。

    挂到 ChatOpenAI 的 callbacks 上，Agent/工作流里所有模型调用都会被计费。
    """

    def __init__(self, tracker: CostTracker) -> None:
        self._tracker = tracker

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        llm_output = response.llm_output or {}
        usage = llm_output.get("token_usage")
        if not usage:
            return
        model = str(llm_output.get("model_name") or llm_output.get("model") or "unknown")
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        self._tracker.record(
            model, prompt_tokens, completion_tokens,
            estimate_cost(model, prompt_tokens, completion_tokens),
        )
