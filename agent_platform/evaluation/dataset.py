"""评测数据集：golden dataset 的定义与加载。

golden dataset 用 JSONL 版本化在仓库里（每条一行），约 50 条以内即可，别堆到上千条
（PLAN.md 明确的风险提示）。RAG 引用类用例在 Phase 2 的 pgvector 就绪后再补。
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel


class GoldenCase(BaseModel):
    """一条评测用例：问题 + 参考答案 + 类别。"""

    id: str
    question: str
    reference: str
    category: str = "general"


def load_dataset(path: Path) -> list[GoldenCase]:
    """从 JSONL 加载 golden dataset（每行一条 GoldenCase，空行忽略）。"""
    cases: list[GoldenCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        cases.append(GoldenCase.model_validate_json(stripped))
    return cases
