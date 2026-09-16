"""输入/输出护栏：Prompt 注入检测 + 敏感信息脱敏。

输入护栏挡在模型调用之前（拦截疑似注入），输出护栏挡在结果返回之前（脱敏密钥/
卡号等）。这是**启发式的第一道防线**，用于「宁可误杀、不可漏放」的快速拦截；
生产上应再叠加「LLM-as-judge 二判」与「语义护栏」（见 evaluation/ 与后续）。

护栏默认开启：Agent 组装时若无显式传入，即用内置默认实例。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from pydantic import BaseModel


class GuardViolation(Exception):
    """输入被护栏拦截（如疑似 Prompt 注入）。"""


class GuardResult(BaseModel):
    """护栏判定结果。"""

    allowed: bool
    reason: str = ""
    masked: str = ""


# 常见 Prompt 注入特征（中英双语、大小写不敏感）。
_INJECTION_PATTERNS: tuple[str, ...] = (
    r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions",
    r"disregard\s+(all\s+)?instructions",
    r"(reveal|show|print|leak)\s+(your\s+)?(system\s+)?prompt",
    r"jailbreak",
    r"(忽略|无视|忘记)\s*(前面|之前|上面|上述|之前所有|所有)?\s*的?\s*(指令|规则|设定|提示词)",
    r"(系统|system)\s*(提示词|prompt)",
    r"(告诉|给|发|泄露)\s*(我)?\s*(你的)?\s*(系统)?\s*(提示词|prompt)",
    r"不要遵守.*(规则|指令|设定)",
)

# 输出脱敏特征：密钥 / 私钥 / 卡号等。
_SECRET_PATTERNS: tuple[str, ...] = (
    r"sk-[A-Za-z0-9]{16,}",           # OpenAI / DeepSeek 风格 key
    r"AKIA[0-9A-Z]{16}",              # AWS Access Key
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"\b\d{15,19}\b",                 # 银行卡号
)


class InputGuard:
    """拦截疑似 Prompt 注入的输入。"""

    def __init__(self, patterns: Sequence[str] = _INJECTION_PATTERNS) -> None:
        self._patterns = [re.compile(p, re.IGNORECASE) for p in patterns]

    def check(self, text: str) -> GuardResult:
        for pattern in self._patterns:
            if pattern.search(text):
                return GuardResult(allowed=False, reason="检测到疑似 Prompt 注入")
        return GuardResult(allowed=True)


class OutputGuard:
    """对输出做敏感信息脱敏（mask，不阻断）。"""

    def __init__(self, patterns: Sequence[str] = _SECRET_PATTERNS) -> None:
        self._patterns = [re.compile(p) for p in patterns]

    def mask(self, text: str) -> str:
        masked = text
        for pattern in self._patterns:
            masked = pattern.sub("[已脱敏]", masked)
        return masked


# 内置默认实例（进程级共享，无状态）。
default_input_guard = InputGuard()
default_output_guard = OutputGuard()
