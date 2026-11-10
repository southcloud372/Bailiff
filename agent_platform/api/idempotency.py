"""请求幂等：同一个操作重试不会产生两次副作用。

场景很具体：客户端超时后重发、网关重试、用户连点两下「发送」。对于问答这类只读操作，
重复执行只是浪费钱；但对 ``write_file`` 这种有副作用的调用，重复执行就是数据错误。

做法是标准的 Idempotency-Key 语义（和支付网关一致）：

- 客户端为每个逻辑操作生成一个 key，重试时复用同一个 key；
- 服务端首次执行后把响应存起来，重复请求直接回放，并带 ``Idempotent-Replay: true``；
- **同一个 key 配不同的请求体** 说明客户端用错了，返回 409，而不是悄悄执行第二次。

存储是进程内的 TTL + LRU，够单副本用。多副本要换成 Redis（key 前缀加租户），
否则两个副本各存一份，等于没有。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any


class IdempotencyConflict(Exception):
    """同一个 key 被用于不同的请求体。"""


@dataclass(frozen=True)
class StoredResponse:
    status_code: int
    body: Any
    created_at: float


class IdempotencyStore:
    """进程内的幂等记录表。"""

    def __init__(self, ttl_seconds: float = 600.0, max_entries: int = 2048) -> None:
        self._ttl = ttl_seconds
        self._max = max_entries
        self._lock = threading.Lock()
        self._items: OrderedDict[str, tuple[str, StoredResponse]] = OrderedDict()

    @staticmethod
    def digest(payload: Any) -> str:
        """请求体指纹。用规范化 JSON 保证字段顺序不影响结果。"""
        canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

    def lookup(self, scope: str, key: str, digest: str) -> StoredResponse | None:
        """命中返回已存响应；key 相同但指纹不同抛 :class:`IdempotencyConflict`。"""
        cache_key = f"{scope}:{key}"
        now = time.monotonic()
        with self._lock:
            item = self._items.get(cache_key)
            if item is None:
                return None
            stored_digest, response = item
            if now - response.created_at > self._ttl:
                del self._items[cache_key]
                return None
            if stored_digest != digest:
                raise IdempotencyConflict(key)
            self._items.move_to_end(cache_key)
            return response

    def remember(
        self, scope: str, key: str, digest: str, status_code: int, body: Any
    ) -> None:
        """记录响应并淘汰最旧的条目。"""
        cache_key = f"{scope}:{key}"
        with self._lock:
            self._items[cache_key] = (
                digest,
                StoredResponse(status_code, body, time.monotonic()),
            )
            self._items.move_to_end(cache_key)
            while len(self._items) > self._max:
                self._items.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)
