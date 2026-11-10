"""幂等键与会话保留策略的测试。

两者都在防同一类事故：**重复或无限增长**。前者让重试不产生第二次副作用，
后者让会话状态不会把磁盘吃满。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_platform.api.app import create_app
from agent_platform.api.idempotency import IdempotencyConflict, IdempotencyStore
from agent_platform.core.checkpoint import SqliteCheckpointSaver
from agent_platform.gateway.auth import encode_token
from agent_platform.gateway.config import Settings
from agent_platform.gateway.policy import Principal
from agent_platform.gateway.wiring import build_platform

SECRET = "idem-secret-key-0123456789abcdef"


class _CountingAgent:
    """记录被调用次数，用来证明「重试没有真的执行第二次」。"""

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, message: str, *, thread_id: str, principal: Principal) -> str:
        self.calls += 1
        return f"第{self.calls}次: {message}"

    async def ask(self, message: str, *, principal: Principal) -> str:
        return await self.chat(message, thread_id="ask", principal=principal)

    async def stream(self, message: str, *, thread_id: str, principal: Principal):
        yield "ok"


# ── 存储层语义 ──────────────────────────────────────────────
def test_lookup_miss_returns_none() -> None:
    store = IdempotencyStore()
    assert store.lookup("u1:/v1/chat", "k1", "d1") is None


def test_same_key_same_body_replays() -> None:
    store = IdempotencyStore()
    store.remember("u1:/v1/chat", "k1", "d1", 200, {"reply": "hi"})
    hit = store.lookup("u1:/v1/chat", "k1", "d1")
    assert hit is not None
    assert hit.body == {"reply": "hi"}


def test_same_key_different_body_conflicts() -> None:
    store = IdempotencyStore()
    store.remember("u1:/v1/chat", "k1", "d1", 200, {"reply": "hi"})
    with pytest.raises(IdempotencyConflict):
        store.lookup("u1:/v1/chat", "k1", "d2")


def test_key_is_scoped_per_principal() -> None:
    """不同用户用了同一个 key 不应互相干扰。"""
    store = IdempotencyStore()
    store.remember("alice:/v1/chat", "k1", "d1", 200, {"reply": "alice"})
    assert store.lookup("bob:/v1/chat", "k1", "d1") is None


def test_entries_expire(monkeypatch: pytest.MonkeyPatch) -> None:
    store = IdempotencyStore(ttl_seconds=10)
    store.remember("u1:/v1/chat", "k1", "d1", 200, {"reply": "hi"})

    later = time.monotonic() + 11
    monkeypatch.setattr("agent_platform.api.idempotency.time.monotonic", lambda: later)
    assert store.lookup("u1:/v1/chat", "k1", "d1") is None


def test_store_is_bounded() -> None:
    store = IdempotencyStore(max_entries=3)
    for i in range(10):
        store.remember("u1", f"k{i}", "d", 200, {"i": i})
    assert len(store) == 3


# ── HTTP 行为 ───────────────────────────────────────────────
async def _client(tmp_path: Path, agent: Any) -> httpx.AsyncClient:
    platform = build_platform(Settings(jwt_secret=SECRET, fs_root=tmp_path))
    app = create_app(platform, agent=agent)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    )


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {encode_token('u1', frozenset({'agent'}), SECRET)}"}


@pytest.mark.asyncio
async def test_retry_with_same_key_does_not_execute_twice(tmp_path: Path) -> None:
    agent = _CountingAgent()
    async with await _client(tmp_path, agent) as client:
        headers = {**_auth(), "Idempotency-Key": "req-1"}
        first = await client.post("/v1/chat", headers=headers, json={"message": "写入"})
        second = await client.post("/v1/chat", headers=headers, json={"message": "写入"})

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.headers.get("Idempotent-Replay") == "true"
    assert first.json() == second.json()
    assert agent.calls == 1


@pytest.mark.asyncio
async def test_different_key_executes_again(tmp_path: Path) -> None:
    agent = _CountingAgent()
    async with await _client(tmp_path, agent) as client:
        await client.post(
            "/v1/chat", headers={**_auth(), "Idempotency-Key": "a"}, json={"message": "x"}
        )
        await client.post(
            "/v1/chat", headers={**_auth(), "Idempotency-Key": "b"}, json={"message": "x"}
        )
    assert agent.calls == 2


@pytest.mark.asyncio
async def test_same_key_different_payload_returns_409(tmp_path: Path) -> None:
    agent = _CountingAgent()
    async with await _client(tmp_path, agent) as client:
        await client.post(
            "/v1/chat", headers={**_auth(), "Idempotency-Key": "k"}, json={"message": "A"}
        )
        resp = await client.post(
            "/v1/chat", headers={**_auth(), "Idempotency-Key": "k"}, json={"message": "B"}
        )
    assert resp.status_code == 409
    assert agent.calls == 1


# ── 会话保留策略 ────────────────────────────────────────────
def test_prune_identifies_and_deletes_stale_threads(tmp_path: Path) -> None:
    db = tmp_path / "agent.sqlite"
    saver = SqliteCheckpointSaver(db)
    # 两条会话：一条"很久以前"写入，一条刚写入
    for thread_id in ("old", "fresh"):
        saver.storage[thread_id] = {}
        saver._conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, "
            "checkpoint_type, checkpoint_blob, metadata_type, metadata_blob, updated_at) "
            "VALUES (?, '', 'c1', 't', x'00', 't', x'00', ?)",
            (thread_id, time.time() - (40 * 86400) if thread_id == "old" else time.time()),
        )
    saver._conn.commit()

    cutoff = time.time() - 30 * 86400
    preview = saver.prune_threads(older_than=cutoff, dry_run=True)
    assert preview == ["old"]
    # 预演不能真删
    assert saver._conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 2

    deleted = saver.prune_threads(older_than=cutoff, dry_run=False)
    assert deleted == ["old"]
    remaining = {row[0] for row in saver._conn.execute("SELECT thread_id FROM checkpoints")}
    assert remaining == {"fresh"}
    saver.close()


def test_old_database_gets_migrated(tmp_path: Path) -> None:
    """早期库没有 updated_at 列，打开时必须自动补列而不是报错。"""
    db = tmp_path / "legacy.sqlite"
    import sqlite3

    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE checkpoints (
            thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL,
            checkpoint_id TEXT NOT NULL, parent_checkpoint_id TEXT,
            checkpoint_type TEXT NOT NULL, checkpoint_blob BLOB NOT NULL,
            metadata_type TEXT NOT NULL, metadata_blob BLOB NOT NULL,
            PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
        );
        """
    )
    conn.execute(
        "INSERT INTO checkpoints VALUES ('t1','','c1',NULL,'t',x'00','t',x'00')"
    )
    conn.commit()
    conn.close()

    saver = SqliteCheckpointSaver(db)
    stats = saver.thread_stats()
    assert stats and stats[0][0] == "t1"
    # 存量数据被打上当前时间，不会被下一次清理当成过期数据误删
    assert stats[0][1] > time.time() - 60
    saver.close()
