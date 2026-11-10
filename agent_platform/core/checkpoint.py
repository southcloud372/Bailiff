"""LangGraph 图状态的持久化后端。

MemorySaver 把状态放在进程内存里，进程重启即丢失：等待人工审批的工单会消失，
多轮对话上下文会断。这里提供三种后端，按部署形态选择：

memory 只用于测试；sqlite 单机持久化，不依赖外部服务；postgres 供多副本共享。

写入采用 write-through，读取按线程惰性加载。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
)
from langgraph.checkpoint.memory import InMemorySaver

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id            TEXT NOT NULL,
    checkpoint_ns        TEXT NOT NULL,
    checkpoint_id        TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    checkpoint_type      TEXT NOT NULL,
    checkpoint_blob      BLOB NOT NULL,
    metadata_type        TEXT NOT NULL,
    metadata_blob        BLOB NOT NULL,
    updated_at           REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE IF NOT EXISTS checkpoint_writes (
    thread_id     TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    write_idx     INTEGER NOT NULL,
    channel       TEXT NOT NULL,
    value_type    TEXT NOT NULL,
    value_blob    BLOB NOT NULL,
    task_path     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, write_idx)
);
CREATE TABLE IF NOT EXISTS checkpoint_blobs (
    thread_id     TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL,
    channel       TEXT NOT NULL,
    version       TEXT NOT NULL,
    type          TEXT NOT NULL,
    data          BLOB NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_thread
    ON checkpoints (thread_id, checkpoint_ns, checkpoint_id DESC);
"""


class SqliteCheckpointSaver(InMemorySaver):
    """写穿到 SQLite 的 checkpoint saver（单文件 = 单图，避免不同图的 thread 撞车）。"""

    def __init__(self, db_path: str | Path) -> None:
        super().__init__()
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._path = path
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        self._db_lock = threading.Lock()
        self._loaded_threads: set[str] = set()

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    def close(self) -> None:
        with self._db_lock:
            self._conn.close()

    def __enter__(self) -> SqliteCheckpointSaver:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    # ── 读：惰性 hydrate ─────────────────────────────────────────────────────

    def _ensure_loaded(self, thread_id: str) -> None:
        """把某个线程的历史从 SQLite 载回内存（每个线程只做一次）。"""
        if thread_id in self._loaded_threads:
            return
        self._loaded_threads.add(thread_id)
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
                "checkpoint_type, checkpoint_blob, metadata_type, metadata_blob "
                "FROM checkpoints WHERE thread_id = ?",
                (thread_id,),
            ).fetchall()
            for ns, cp_id, parent, cp_type, cp_blob, md_type, md_blob in rows:
                self.storage[thread_id][ns][cp_id] = (
                    (cp_type, bytes(cp_blob)),
                    (md_type, bytes(md_blob)),
                    parent,
                )
            write_rows = self._conn.execute(
                "SELECT checkpoint_ns, checkpoint_id, task_id, write_idx, channel, "
                "value_type, value_blob, task_path FROM checkpoint_writes "
                "WHERE thread_id = ?",
                (thread_id,),
            ).fetchall()
            for ns, cp_id, task_id, idx, channel, v_type, v_blob, task_path in write_rows:
                self.writes[(thread_id, ns, cp_id)][(task_id, int(idx))] = (
                    task_id,
                    channel,
                    (v_type, bytes(v_blob)),
                    task_path,
                )
            blob_rows = self._conn.execute(
                "SELECT checkpoint_ns, channel, version, type, data "
                "FROM checkpoint_blobs WHERE thread_id = ?",
                (thread_id,),
            ).fetchall()
            for ns, channel, version, b_type, b_data in blob_rows:
                self.blobs[(thread_id, ns, channel, version)] = (
                    b_type,
                    bytes(b_data),
                )

    def _load_all_threads(self) -> None:
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints"
            ).fetchall()
        for (thread_id,) in rows:
            self._ensure_loaded(str(thread_id))

    def get_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id: str = config["configurable"]["thread_id"]
        self._ensure_loaded(thread_id)
        return super().get_tuple(config)

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[CheckpointTuple]:
        if config is None:
            self._load_all_threads()
        else:
            self._ensure_loaded(config["configurable"]["thread_id"])
        return super().list(config, filter=filter, before=before, limit=limit)

    # ── 写：写穿 ─────────────────────────────────────────────────────────────

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        result = super().put(config, checkpoint, metadata, new_versions)
        self._loaded_threads.add(thread_id)

        stored = self.storage[thread_id][checkpoint_ns][checkpoint["id"]]
        (cp_type, cp_blob), (md_type, md_blob), parent = stored
        with self._db_lock:
            self._conn.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, "
                "parent_checkpoint_id, checkpoint_type, checkpoint_blob, "
                "metadata_type, metadata_blob, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(thread_id, checkpoint_ns, checkpoint_id) DO UPDATE SET "
                "parent_checkpoint_id=excluded.parent_checkpoint_id, "
                "checkpoint_type=excluded.checkpoint_type, "
                "checkpoint_blob=excluded.checkpoint_blob, "
                "metadata_type=excluded.metadata_type, "
                "metadata_blob=excluded.metadata_blob, "
                "updated_at=excluded.updated_at",
                (
                    thread_id,
                    checkpoint_ns,
                    checkpoint["id"],
                    parent,
                    cp_type,
                    cp_blob,
                    md_type,
                    md_blob,
                    time.time(),
                ),
            )
            for channel, version in new_versions.items():
                blob = self.blobs.get((thread_id, checkpoint_ns, channel, version))
                if blob is None:
                    continue
                self._conn.execute(
                    "INSERT INTO checkpoint_blobs (thread_id, checkpoint_ns, channel, "
                    "version, type, data) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(thread_id, checkpoint_ns, channel, version) "
                    "DO UPDATE SET type=excluded.type, data=excluded.data",
                    (
                        thread_id,
                        checkpoint_ns,
                        channel,
                        str(version),
                        blob[0],
                        blob[1],
                    ),
                )
            self._conn.commit()
        return result

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        thread_id: str = config["configurable"]["thread_id"]
        checkpoint_ns: str = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id: str = config["configurable"]["checkpoint_id"]
        super().put_writes(config, writes, task_id, task_path)
        persisted = self.writes.get((thread_id, checkpoint_ns, checkpoint_id), {})
        with self._db_lock:
            for (stored_task_id, idx), (
                _tid,
                channel,
                (value_type, value_blob),
                stored_task_path,
            ) in persisted.items():
                self._conn.execute(
                    "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, "
                    "checkpoint_id, task_id, write_idx, channel, value_type, "
                    "value_blob, task_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(thread_id, checkpoint_ns, checkpoint_id, task_id, "
                    "write_idx) DO UPDATE SET channel=excluded.channel, "
                    "value_type=excluded.value_type, value_blob=excluded.value_blob",
                    (
                        thread_id,
                        checkpoint_ns,
                        checkpoint_id,
                        stored_task_id,
                        int(idx),
                        channel,
                        value_type,
                        value_blob,
                        stored_task_path,
                    ),
                )
            self._conn.commit()

    def _migrate(self) -> None:
        """给早期库补 updated_at 列。SQLite 不支持 IF NOT EXISTS，只能先查。"""
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(checkpoints)")
        }
        if "updated_at" not in columns:
            self._conn.execute(
                "ALTER TABLE checkpoints ADD COLUMN updated_at REAL NOT NULL DEFAULT 0"
            )
            # 存量数据没有时间戳，用当前时间兜底，避免下一次清理直接把它们全删了
            self._conn.execute(
                "UPDATE checkpoints SET updated_at = ? WHERE updated_at = 0",
                (time.time(),),
            )
        # 索引放在补列之后建，否则老库会在建索引时报 "no such column"
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_checkpoints_updated "
            "ON checkpoints (thread_id, updated_at DESC)"
        )

    def thread_stats(self, limit: int = 50) -> Sequence[tuple[str, float, int]]:
        """返回 (thread_id, 最后更新时间戳, checkpoint 数)，按时间倒序。"""
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT thread_id, MAX(updated_at) AS ts, COUNT(*) AS n "
                "FROM checkpoints GROUP BY thread_id ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(str(r[0]), float(r[1] or 0.0), int(r[2])) for r in rows]

    def prune_threads(self, *, older_than: float, dry_run: bool = False) -> Sequence[str]:
        """删除 ``older_than``（时间戳）之前再无写入的线程，返回被删的 thread_id。"""
        with self._db_lock:
            rows = self._conn.execute(
                "SELECT thread_id, MAX(updated_at) FROM checkpoints "
                "GROUP BY thread_id HAVING MAX(updated_at) < ?",
                (older_than,),
            ).fetchall()
        stale = [str(r[0]) for r in rows]
        if dry_run:
            return stale
        for thread_id in stale:
            self.delete_thread(thread_id)
        return stale

    def delete_thread(self, thread_id: str) -> None:
        super().delete_thread(thread_id)
        self._loaded_threads.discard(thread_id)
        with self._db_lock:
            for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
                self._conn.execute(
                    f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,)
                )
            self._conn.commit()


def build_checkpointer(
    backend: str = "memory",
    *,
    sqlite_path: str | Path | None = None,
    postgres_dsn: str | None = None,
) -> Any:
    """按配置构造 checkpointer：``memory`` / ``sqlite`` / ``postgres``。

    返回类型用 ``Any`` 是因为 LangGraph 的 ``BaseCheckpointSaver`` 泛型参数随
    serde 变化，上层只把它当不透明对象传给 ``compile(checkpointer=...)``。

    ``postgres`` 后端依赖可选包 ``langgraph-checkpoint-postgres``（``uv sync --extra prod``），
    未安装时给出可执行的报错，而不是 ``ImportError`` 栈。
    """
    if backend == "memory":
        return InMemorySaver()
    if backend == "sqlite":
        if sqlite_path is None:
            raise ValueError("sqlite checkpoint 后端必须提供 sqlite_path")
        return SqliteCheckpointSaver(sqlite_path)
    if backend == "postgres":
        if postgres_dsn is None:
            raise ValueError("postgres checkpoint 后端必须提供 postgres_dsn")
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except ImportError as exc:  # pragma: no cover - 取决于可选依赖是否安装
            raise RuntimeError(
                "postgres checkpoint 需要可选依赖：uv sync --extra prod "
                "（langgraph-checkpoint-postgres + psycopg_pool）"
            ) from exc
        from psycopg_pool import AsyncConnectionPool

        # 连接池由 saver 持有；调用方负责在应用 lifespan 里关闭。
        pool = AsyncConnectionPool(postgres_dsn, open=False, kwargs={"autocommit": True})
        return _PostgresCheckpointer(AsyncPostgresSaver(cast(Any, pool)), pool)
    raise ValueError(f"不支持的 checkpoint 后端: {backend}")


class _PostgresCheckpointer:
    """把「连接池 + AsyncPostgresSaver」组合成可 await 初始化的 checkpointer 门面。

    ``AsyncPostgresSaver.setup()`` 需要建表（幂等），而 ``compile()`` 是同步的，
    所以这里提供 ``await setup()`` 由应用启动时调用，其余方法直接委托。
    """

    def __init__(self, saver: Any, pool: Any) -> None:
        self._saver = saver
        self._pool = pool

    async def setup(self) -> None:
        await self._pool.open()
        await self._saver.setup()

    async def aclose(self) -> None:
        await self._pool.close()

    def __getattr__(self, item: str) -> Any:
        return getattr(self._saver, item)


def thread_has_checkpoint(saver: Any, thread_id: str) -> bool:
    """判断某线程是否已有落盘状态（供 ``/readyz`` 与运维排查使用）。"""
    if isinstance(saver, SqliteCheckpointSaver):
        with saver._db_lock:
            row = saver._conn.execute(
                "SELECT 1 FROM checkpoints WHERE thread_id = ? LIMIT 1", (thread_id,)
            ).fetchone()
        return row is not None
    # AsyncPostgresSaver 的读取是异步接口，不能在同步探针里 await；
    # 这类后端由 /readyz 的「连通性探针」负责，不在这里猜测。
    if isinstance(saver, _PostgresCheckpointer) or not hasattr(saver, "get_tuple"):
        return False
    try:
        config: RunnableConfig = {"configurable": {"thread_id": thread_id}}
        return saver.get_tuple(config) is not None
    except Exception:
        return False


def checkpoint_id_of(config: RunnableConfig) -> str | None:
    """从 config 里取 checkpoint id（口径与 LangGraph 一致）。"""
    return get_checkpoint_id(config)
