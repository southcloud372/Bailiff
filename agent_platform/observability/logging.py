"""结构化日志：一行一个 JSON，自动带上 request_id / principal / thread。

为什么不用默认的 ``logging.basicConfig`` 文本格式？
- 生产上日志是被机器消费的（Loki / ELK / CloudWatch）：字段可检索比「好看」重要；
- Agent 的调用链是「一次请求 → N 次模型调用 → M 次工具调用」，没有 request_id
  就无法把散落的日志串成一条链；
- 与 OTel trace 并存：日志解决「发生了什么」，trace 解决「耗时花在哪」。

设计取舍：不引入 structlog/loguru，是因为标准库 ``logging`` + 自定义 Formatter
已能覆盖需求，少一个依赖就少一个故障面。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from agent_platform.context import (
    current_principal,
    current_request_id,
    current_thread_id,
)

#: 由 ContextFilter 注入到 LogRecord 的字段名。
_CONTEXT_ATTRS = ("request_id", "principal_id", "thread_id")


class ContextFilter(logging.Filter):
    """把 contextvars 里的请求上下文塞进 LogRecord，供 Formatter 使用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        principal = current_principal.get()
        record.request_id = current_request_id.get() or "-"
        record.principal_id = principal.id if principal is not None else "-"
        record.thread_id = current_thread_id.get() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """把日志记录格式化成单行 JSON。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": getattr(record, "request_id", "-"),
            "principal_id": getattr(record, "principal_id", "-"),
            "thread_id": getattr(record, "thread_id", "-"),
        }
        # ``extra={"event": ...}`` 之类的业务字段原样透传，便于按事件名聚合。
        for key, value in record.__dict__.items():
            if key in payload or key in logging.LogRecord.__dict__:
                continue
            if key.startswith("_"):
                continue
            if key in ("args", "exc_info", "exc_text", "stack_info", "msg", "message"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class TextFormatter(logging.Formatter):
    """人类可读格式（本地开发用），同样带请求上下文。"""

    def __init__(self) -> None:
        super().__init__(
            "%(asctime)s %(levelname)-7s [%(request_id)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )


def configure_logging(level: str = "INFO", *, json_output: bool = True) -> None:
    """配置根 logger。重复调用是幂等的（不会叠加 handler）。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(ContextFilter())
    handler.setFormatter(JsonFormatter() if json_output else TextFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # 第三方库在 INFO 级别过于聒噪，压到 WARNING（它们是「噪声」不是「信号」）。
    for noisy in ("httpx", "httpcore", "urllib3", "asyncio", "mcp", "litellm"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """取 logger（薄封装，方便将来统一注入字段）。"""
    return logging.getLogger(name)
