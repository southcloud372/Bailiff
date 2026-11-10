"""SSE 编码与心跳：把 ``AgentEvent`` 流变成浏览器可消费的 ``text/event-stream``。

两个容易被忽略、但生产必须处理的细节：

1. **命名事件**：只发 ``data:`` 的话前端只能拿到字符串；用 ``event: <type>`` 才能
   让浏览器 ``EventSource`` 按类型分发（或让前端用 fetch + 流式解析）。
2. **心跳**：SSE 走 HTTP 长连接，中间任何反向代理（Nginx/ALB）都有空闲超时。
   定期发一行注释 ``: keep-alive`` 既能保活，又不会被 EventSource 当作事件派发。
   心跳与业务事件并发，用「生产者任务 + 队列 + 超时取」实现，不阻塞模型输出。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse

from agent_platform.core.events import AgentEvent, done_event, error_event

#: 反向代理空闲超时通常 30–60s，这里取更保守的 15s。
DEFAULT_HEARTBEAT_SECONDS = 15.0


def encode_sse(event: AgentEvent) -> str:
    """编码一条命名 SSE 事件。``ensure_ascii=False`` 保证中文可读。"""
    payload = json.dumps(event.data, ensure_ascii=False, default=str)
    return f"event: {event.type}\ndata: {payload}\n\n"


def _encode_heartbeat() -> str:
    return ": keep-alive\n\n"


async def with_heartbeat(
    source: AsyncIterator[AgentEvent],
    interval: float = DEFAULT_HEARTBEAT_SECONDS,
) -> AsyncIterator[AgentEvent | None]:
    """在业务事件流之间插入心跳（``None`` 表示心跳），并负责终止源迭代器。"""
    queue: asyncio.Queue[AgentEvent | None] = asyncio.Queue()

    async def pump() -> None:
        try:
            async for event in source:
                await queue.put(event)
        except Exception as exc:  # 源迭代器异常也要让消费端看到，而不是静默挂起
            await queue.put(error_event("stream_error", str(exc)))
        finally:
            await queue.put(None)

    task = asyncio.create_task(pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except TimeoutError:
                yield None
                continue
            if item is None:
                return
            yield item
    finally:
        task.cancel()
        # 取消后等上游真正退出，避免「生成器已关闭但任务还在跑」的泄漏。
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def sse_response(
    source: AsyncIterator[AgentEvent],
    *,
    heartbeat_seconds: float = DEFAULT_HEARTBEAT_SECONDS,
    extra_headers: dict[str, str] | None = None,
) -> StreamingResponse:
    """构造 SSE 响应：自动补 ``done`` 事件、心跳与防缓冲响应头。"""

    async def body() -> AsyncIterator[str]:
        try:
            async for item in with_heartbeat(source, heartbeat_seconds):
                if item is None:
                    yield _encode_heartbeat()
                else:
                    yield encode_sse(item)
                    if item.type == "done":
                        return
            # 源正常结束但没有显式 done：补一条，前端才能确定性收尾。
            yield encode_sse(done_event())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            yield encode_sse(error_event("internal_error", str(exc)))

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        # 关掉 Nginx 的响应缓冲，否则 token 会被攒成一坨再发（首 token 延迟爆炸）。
        "X-Accel-Buffering": "no",
    }
    if extra_headers:
        headers.update(extra_headers)
    return StreamingResponse(body(), media_type="text/event-stream", headers=headers)
