"""自检补上的四道防线。

这些都不是功能，而是「跑久了才出问题」的那类缺陷：
成本账本跨租户可见、审计日志无界增长、超长输入直接进模型、模型调用没有超时。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from agent_platform.api.app import create_app
from agent_platform.gateway.audit import AuditEvent, AuditLogger, JsonlAuditSink
from agent_platform.gateway.auth import encode_token
from agent_platform.gateway.config import ModelSettings, Settings
from agent_platform.gateway.wiring import build_platform
from agent_platform.router.cost import CostTracker

SECRET = "selfcheck-secret-key-0123456789abcdef"


# ── 审计：内存必须有界 ──────────────────────────────────────
def _event(i: int) -> AuditEvent:
    return AuditEvent(principal_id="u1", tool="read_file", arguments={"i": i}, outcome="allowed")


def test_audit_buffer_is_bounded() -> None:
    logger = AuditLogger(max_events=3)
    for i in range(10):
        logger.log(_event(i))
    assert len(logger.events) == 3
    # 保留的是最近的，而不是最早的
    assert logger.events[-1].arguments["i"] == 9


def test_audit_jsonl_sink_appends_parseable_lines(tmp_path: Path) -> None:
    path = tmp_path / "audit" / "tool.jsonl"
    logger = AuditLogger(sink=JsonlAuditSink(path), max_events=10)
    logger.log(_event(1))
    logger.log(_event(2))

    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["tool"] == "read_file"
    assert first["outcome"] == "allowed"


# ── 成本：按调用方隔离 ──────────────────────────────────────
def test_cost_is_tracked_per_principal() -> None:
    tracker = CostTracker()
    tracker.record("m", 10, 5, 0.01, principal_id="alice")
    tracker.record("m", 20, 30, 0.02, principal_id="bob")

    assert tracker.summary("alice").total_prompt_tokens == 10
    assert tracker.summary("bob").total_prompt_tokens == 20
    # 全局汇总仍然可用，但只供内部
    assert round(tracker.summary().total_cost, 4) == 0.03
    # 没产生过花费的调用方拿到空账本，而不是别人的数据
    assert tracker.summary("carol").total_cost == 0.0


# ── 输入边界 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_oversized_message_is_rejected(tmp_path: Path) -> None:
    platform = build_platform(Settings(jwt_secret=SECRET, fs_root=tmp_path))
    app = create_app(platform)
    token = encode_token("u1", frozenset({"agent"}), SECRET)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost:8000"
    ) as client:
        resp = await client.post(
            "/v1/chat",
            headers={"Authorization": f"Bearer {token}"},
            json={"message": "x" * 32001},
        )
    # 422 是 Pydantic 校验失败的标准状态，先于模型调用发生
    assert resp.status_code == 422


# ── 模型调用超时可以配置 ────────────────────────────────────
def test_model_timeout_is_configurable() -> None:
    assert ModelSettings().timeout_seconds > 0
    assert ModelSettings(timeout_seconds=5).timeout_seconds == 5


# ── 审计注入到网关后，被拒的调用也会留痕 ────────────────────
@pytest.mark.asyncio
async def test_denied_tool_call_is_audited(tmp_path: Path) -> None:
    from agent_platform.gateway.policy import Principal

    records: list[AuditEvent] = []
    platform = build_platform(
        Settings(jwt_secret=SECRET, fs_root=tmp_path), audit_sink=records.append
    )
    guest = Principal(id="guest", roles=frozenset({"guest"}))

    with pytest.raises(Exception):  # noqa: B017 - 具体异常已由 test_gateway 覆盖
        await platform.gateway.call(guest, "read_file", {"path": "a.txt"})

    assert records and records[-1].outcome == "denied"
    assert records[-1].principal_id == "guest"
