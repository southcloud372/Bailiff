"""checkpoint 持久化测试：验证「进程重启后还能续跑」。

这是 HITL 的地基：工单停在 interrupt() 等审批，人几小时后才点同意，进程早就重启过。
如果持久化不生效，整个审批闭环在真实环境里就是断的。
"""

from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pytest
from langgraph.graph import END, START, StateGraph

from agent_platform.core.checkpoint import (
    SqliteCheckpointSaver,
    build_checkpointer,
    thread_has_checkpoint,
)


class Counter(TypedDict):
    n: int


def _inc(state: Counter) -> dict[str, int]:
    return {"n": state["n"] + 1}


def _compile(path: Path):
    graph = StateGraph(Counter)
    graph.add_node("inc", _inc)
    graph.add_edge(START, "inc")
    graph.add_edge("inc", END)
    return graph.compile(checkpointer=SqliteCheckpointSaver(path))


def test_sqlite_checkpoint_survives_new_instance(tmp_path: Path) -> None:
    db = tmp_path / "cp.sqlite"
    config = {"configurable": {"thread_id": "t1"}}

    first = _compile(db)
    assert first.invoke({"n": 0}, config) == {"n": 1}
    # 释放连接，模拟进程退出
    first.checkpointer.close()  # type: ignore[attr-defined]

    # 新实例（等价于重启后的进程）应能读回同一线程的状态
    second = _compile(db)
    snapshot = second.get_state(config)
    assert snapshot.values == {"n": 1}
    assert thread_has_checkpoint(second.checkpointer, "t1") is True  # type: ignore[arg-type]
    second.checkpointer.close()  # type: ignore[attr-defined]


def test_unknown_thread_has_no_checkpoint(tmp_path: Path) -> None:
    saver = SqliteCheckpointSaver(tmp_path / "cp.sqlite")
    try:
        assert thread_has_checkpoint(saver, "never-seen") is False
    finally:
        saver.close()


def test_sqlite_checkpoint_isolated_by_thread(tmp_path: Path) -> None:
    graph = _compile(tmp_path / "cp.sqlite")
    try:
        graph.invoke({"n": 0}, {"configurable": {"thread_id": "a"}})
        graph.invoke({"n": 100}, {"configurable": {"thread_id": "b"}})
        assert graph.get_state({"configurable": {"thread_id": "a"}}).values == {"n": 1}
        assert graph.get_state({"configurable": {"thread_id": "b"}}).values == {"n": 101}
    finally:
        graph.checkpointer.close()  # type: ignore[attr-defined]


def test_delete_thread_purges_state(tmp_path: Path) -> None:
    graph = _compile(tmp_path / "cp.sqlite")
    config = {"configurable": {"thread_id": "x"}}
    try:
        graph.invoke({"n": 0}, config)
        assert graph.get_state(config).values == {"n": 1}
        graph.checkpointer.delete_thread("x")  # type: ignore[attr-defined]
        assert graph.get_state(config).values == {}
        # 再删一次不存在的线程也不应抛异常（幂等）
        graph.checkpointer.delete_thread("x")  # type: ignore[attr-defined]
    finally:
        graph.checkpointer.close()  # type: ignore[attr-defined]


def test_build_checkpointer_backend_errors() -> None:
    with pytest.raises(ValueError, match="sqlite"):
        build_checkpointer("sqlite")
    with pytest.raises(ValueError, match="postgres"):
        build_checkpointer("postgres")
    with pytest.raises(ValueError, match="不支持"):
        build_checkpointer("oracle")
