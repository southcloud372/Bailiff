"""网关审计：每一次工具调用/访问尝试都要留痕。

生产级 Agent 平台的治理底线是可审计，谁、何时、调了什么工具、结果如何。
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field

Outcome = Literal["allowed", "denied", "rate_limited", "error"]


class AuditEvent(BaseModel):
    """一次工具访问的审计记录。"""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    principal_id: str
    tool: str
    arguments: dict[str, Any]
    outcome: Outcome
    duration_ms: float | None = None


class AuditSink(Protocol):
    def __call__(self, event: AuditEvent) -> None: ...


class JsonlAuditSink:
    """把审计事件追加写入 JSONL 文件，供采集器（Filebeat/Vector）消费。

    进程内缓冲只用于最近的排查，真正的留存靠这里：审计是合规要求，不能随进程退出丢失。
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def __call__(self, event: AuditEvent) -> None:
        line = event.model_dump_json()
        with self._lock, self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


class AuditLogger:
    """审计日志器：进程内保留最近 N 条，可选落盘。

    内存部分必须是**有界**的。早期实现用 list 只增不减，长跑必然 OOM，
    而且这种泄漏在压测时不一定暴露，往往在线上跑几天后才发作。
    """

    def __init__(self, sink: AuditSink | None = None, max_events: int = 5000) -> None:
        self._events: deque[AuditEvent] = deque(maxlen=max_events)
        self._sink = sink
        self._lock = threading.Lock()

    def log(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)
        if self._sink is not None:
            self._sink(event)

    @property
    def events(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)
