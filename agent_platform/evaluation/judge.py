"""LLM-as-judge：用模型对 Agent 回答做结构化打分。

DeepSeek 不支持 ``response_format``（结构化输出），但支持 function calling，故用
``with_structured_output(..., method="function_calling")`` 拿到结构化结论
（0-100 分 + 一句话理由）。评判模型独立于被测 Agent，避免「自己给自己打分」的偏向。
"""

from __future__ import annotations

from typing import Any, cast

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, Field

_JUDGE_PROMPT = """你是严格的评测裁判。请根据「参考答案」评估「Agent 回答」的质量。

- 语义正确性：回答是否准确、无事实错误
- 完整性：是否覆盖参考答案的关键要点

问题：{question}
参考答案：{reference}
Agent 回答：{answer}

给出 0-100 的评分与一句话理由。"""


class JudgeVerdict(BaseModel):
    """裁判结构化结论。"""

    score: int = Field(ge=0, le=100, description="回答质量评分 0-100")
    reason: str = Field(description="一句话评分理由")


class LLMJudge:
    """用 LLM 作为裁判。"""

    def __init__(self, model: BaseChatModel) -> None:
        # with_structured_output 返回类型为 Runnable[LanguageModelInput, BaseModel|dict]，
        # 具体泛型随 schema 变化，这里以 Any 承接，运行时即 JudgeVerdict。
        self._structured: Any = model.with_structured_output(
            JudgeVerdict, method="function_calling"
        )

    async def evaluate(
        self, question: str, reference: str, answer: str
    ) -> JudgeVerdict:
        prompt = _JUDGE_PROMPT.format(
            question=question, reference=reference, answer=answer
        )
        return cast(JudgeVerdict, await self._structured.ainvoke(prompt))
