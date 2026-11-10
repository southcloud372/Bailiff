"""启动网关服务。用法：uv run python scripts/serve.py

组装顺序：可观测 → 基础设施（checkpoint/限流/缓存）→ 网关（含知识/记忆/MCP 工具）
→ 模型/Agent → HTTP。

**降级原则**：任何一个可选依赖缺失（模型 key、pgvector、Redis、Ollama）都不阻止启动，
只是对应能力不可用，并在 ``/readyz`` 里如实报告。这让「clone 下来就能跑」和
「生产多副本」共用同一份代码，只靠 ``AGENT_*`` 环境变量切换。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import uvicorn

from agent_platform.api.app import create_app, default_readiness
from agent_platform.core.agent import build_agent
from agent_platform.core.checkpoint import build_checkpointer
from agent_platform.core.workflow import build_ticket_workflow
from agent_platform.gateway.audit import JsonlAuditSink
from agent_platform.gateway.config import KnowledgeSettings, ModelSettings, RuntimeSettings
from agent_platform.gateway.mcp_client import MCPGateway, MCPServerSpec
from agent_platform.gateway.policy import PermissionRule
from agent_platform.gateway.ratelimit import build_rate_limiter
from agent_platform.gateway.wiring import build_platform
from agent_platform.knowledge.factory import build_knowledge_stack
from agent_platform.knowledge.rag import RAGPipeline
from agent_platform.knowledge.tools import build_knowledge_tools
from agent_platform.memory.service import MemoryService
from agent_platform.observability.health import Readiness, callable_probe, static_probe
from agent_platform.observability.tracing import init_tracing
from agent_platform.router.cache import SemanticCache
from agent_platform.router.cost import CostTracker

logger = logging.getLogger(__name__)


def _load_mcp_specs(raw: str) -> list[MCPServerSpec]:
    """解析 ``AGENT_MCP_SERVERS``（JSON 数组），坏配置只警告不影响启动。"""
    if not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("mcp_spec_invalid_json", extra={"error": str(exc)})
        return []
    if not isinstance(data, list):
        logger.warning("mcp_spec_not_list")
        return []
    specs: list[MCPServerSpec] = []
    for item in data:
        try:
            specs.append(MCPServerSpec.from_dict(item))
        except Exception as exc:
            logger.warning("mcp_spec_invalid", extra={"item": item, "error": str(exc)})
    return specs


def _build_readiness(
    platform: Any,
    *,
    checkpointer: Any,
    mcp_gateway: MCPGateway | None,
    cache: Any | None,
    model_settings: ModelSettings | None = None,
    knowledge: tuple[str, int] | None = None,
) -> Readiness:
    """在默认探针之上追加运行时依赖探针。"""
    readiness = default_readiness(platform)

    if model_settings is not None:
        # 模型后端是「静默降级」风险最高的地方：没配 key 也能跑，但答不出真东西。
        # 非 critical（演示模式依然可用），但必须让 /readyz 一眼看出来。
        from agent_platform.core.agent import resolve_model_backend

        backend = resolve_model_backend(model_settings)
        # dev 下演示模型只告警（方便本地跑）；prod 下必须判不就绪，
        # 「带着一个不会推理的模型上线」是最隐蔽的故障：接口全 200，回答全是废话。
        readiness.add(
            "llm",
            static_probe(
                backend == "deepseek",
                "deepseek" if backend == "deepseek" else "mock 离线演示（未配置 API Key）",
            ),
            critical=platform.settings.env != "dev",
        )

    def _checkpoint_ok() -> tuple[bool, str]:
        backend = type(checkpointer).__name__
        return True, f"backend={backend}"

    readiness.add("checkpoint", callable_probe(_checkpoint_ok), critical=True)

    if mcp_gateway is not None and mcp_gateway.specs:
        health = mcp_gateway.health()
        connected = sum(1 for item in health.values() if item["connected"])
        readiness.add(
            "mcp",
            static_probe(
                connected > 0,
                f"{connected}/{len(health)} 个 MCP server 已连接",
            ),
            critical=False,
        )

    if cache is not None:
        readiness.add(
            "semantic_cache", static_probe(True, "已启用（嵌入服务按需探测）"), critical=False
        )

    if knowledge is not None:
        backend, chunks = knowledge
        # 片段为 0 说明库还没灌：接口能通，但检索永远返回空，看起来像「模型不会用知识库」。
        readiness.add(
            "knowledge",
            static_probe(
                chunks > 0,
                f"backend={backend}，片段={chunks}"
                + ("" if chunks > 0 else "（未灌库：运行 uv run python scripts/ingest.py）"),
            ),
            critical=False,
        )
    return readiness


def main() -> None:
    # 可观测：未配置 OTel/Langfuse 时是 no-op。
    init_tracing()

    runtime = RuntimeSettings()
    model_settings = ModelSettings()

    # ── 基础设施 ────────────────────────────────────────────────────────────
    # checkpoint 必须按线程隔离到不同文件：同一 saver 实例同时服务两张图会串 thread。
    def _make_saver(name: str) -> Any:
        if runtime.checkpoint_backend == "sqlite":
            runtime.checkpoint_dir.mkdir(parents=True, exist_ok=True)
            path = runtime.checkpoint_dir / f"{name}.sqlite"
            return build_checkpointer("sqlite", sqlite_path=path)
        if runtime.checkpoint_backend == "postgres":
            return build_checkpointer("postgres", postgres_dsn=runtime.postgres_dsn)
        return build_checkpointer("memory")

    agent_saver = _make_saver("agent")
    ticket_saver = _make_saver("tickets")

    limiter = build_rate_limiter(
        runtime.rate_limit_backend,
        capacity=100,
        refill_per_second=10.0,
        redis_url=runtime.redis_url,
    )

    # ── 知识/记忆：默认本地 SQLite 后端，配 AGENT_RAG_BACKEND=postgres 才走 pgvector ──
    knowledge_settings = KnowledgeSettings()
    stack = build_knowledge_stack(knowledge_settings)
    store, embedder, rag_backend = stack.store, stack.embedder, stack.backend
    rag = RAGPipeline(store, embedder)
    memory = MemoryService(store, embedder)
    knowledge_tools = build_knowledge_tools(rag, memory)

    # ── MCP 网关（可选：外部 MCP server 由 AGENT_MCP_SERVERS 注入）───────────
    mcp_gateway = MCPGateway(_load_mcp_specs(os.environ.get("AGENT_MCP_SERVERS", "")))

    # 审计落盘是可选的：不配就走进程内环形缓冲（最近 5000 条），配了则追加 JSONL。
    audit_log = os.environ.get("AGENT_AUDIT_LOG", "").strip()
    audit_sink = JsonlAuditSink(audit_log) if audit_log else None

    platform = build_platform(
        audit_sink=audit_sink,
        extra_tools=knowledge_tools,
        extra_rules=[
            PermissionRule(tool=name, roles=frozenset({"agent"})) for name in knowledge_tools
        ],
        rate_limiter=limiter,
    )

    async def _start_mcp() -> None:
        """连上外部 MCP server 并把工具注册进网关（治理不旁路）。"""
        if not mcp_gateway.specs:
            return
        await mcp_gateway.start()
        mcp_gateway.register_into(platform.registry, platform.policy)

    async def _stop_mcp() -> None:
        await mcp_gateway.stop()
        await store.aclose()

    # ── 编排层 ──────────────────────────────────────────────────────────────
    cost = CostTracker()
    cache = (
        SemanticCache(
            embedder,
            threshold=runtime.cache_threshold,
            ttl_seconds=runtime.cache_ttl_seconds,
            max_entries=runtime.cache_max_entries,
        )
        if runtime.cache_enabled
        else None
    )
    agent = build_agent(
        model_settings,
        platform.gateway,
        cost_tracker=cost,
        checkpointer=agent_saver,
        cache=cache,
    )
    workflow = build_ticket_workflow(
        platform.gateway, cost_tracker=cost, checkpointer=ticket_saver
    )

    async def _setup_postgres() -> None:
        """Postgres checkpointer 需要显式建表（幂等）。"""
        for saver in (agent_saver, ticket_saver):
            setup = getattr(saver, "setup", None)
            if setup is not None:
                await setup()

    async def _setup_postgres_guarded() -> None:
        if runtime.checkpoint_backend != "postgres":
            return
        try:
            await _setup_postgres()
        except Exception as exc:
            logger.error("checkpoint_setup_failed", extra={"error": str(exc)})

    try:
        chunk_count = asyncio.run(store.count())
    except Exception:
        chunk_count = -1

    readiness = _build_readiness(
        platform,
        checkpointer=agent_saver,
        mcp_gateway=mcp_gateway,
        cache=cache,
        model_settings=model_settings,
        knowledge=(rag_backend, chunk_count),
    )

    app = create_app(
        platform,
        agent=agent,
        workflow=workflow,
        cost_tracker=cost,
        readiness=readiness,
        cache=cache,
        startup_hooks=[_setup_postgres_guarded, _start_mcp],
        shutdown_hooks=[_stop_mcp],
    )

    # 本地默认仅本机监听；容器化部署时用 AGENT_HOST=0.0.0.0 暴露端口。
    host = os.environ.get("AGENT_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
