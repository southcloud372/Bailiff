"""网关审计：每一次工具调用/访问尝试都要留痕。

企业级 Agent 平台的治理底线是可审计——谁、何时、调了什么工具、结果如何。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class AuditEvent(BaseModel):
    """一次工具访问的审计记录。"""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    principal_id: str
    tool: str
    arguments: dict[str, Any]
    outcome: Literal["allowed", "denied", "error"]
    duration_ms: float | None = None


class AuditSink(Protocol):
    def __call__(self, event: AuditEvent) -> None: ...


class AuditLogger:
    """内存 sink 的审计日志器；后续接入 Postgres 持久化。"""

    def __init__(self, sink: AuditSink | None = None) -> None:
        self._events: list[AuditEvent] = []
        self._sink = sink

    def log(self, event: AuditEvent) -> None:
        self._events.append(event)
        if self._sink is not None:
            self._sink(event)

    @property
    def events(self) -> list[AuditEvent]:
        return list(self._events)
