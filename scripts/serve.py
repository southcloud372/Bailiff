"""启动网关服务。用法：uv run python scripts/serve.py

组装顺序：可观测 → 网关（含知识/记忆工具）→ 模型/Agent → HTTP。
未配置模型或未起 Docker 基础设施时仍可启动：/v1/chat 返回 503，知识/记忆工具在
被调用时才报连接错误（注册本身不连库）。
"""

from __future__ import annotations

import os

import uvicorn

from agent_platform.api.app import create_app
from agent_platform.core.agent import build_agent
from agent_platform.core.workflow import build_ticket_workflow
from agent_platform.gateway.config import KnowledgeSettings, ModelSettings
from agent_platform.gateway.policy import PermissionRule
from agent_platform.gateway.wiring import build_platform
from agent_platform.knowledge.embedder import OllamaEmbedder
from agent_platform.knowledge.rag import RAGPipeline
from agent_platform.knowledge.store import PgVectorStore
from agent_platform.knowledge.tools import build_knowledge_tools
from agent_platform.memory.service import MemoryService
from agent_platform.observability.tracing import init_tracing
from agent_platform.router.cost import CostTracker


def main() -> None:
    # 可观测：未配置 OTel/Langfuse 时是 no-op。
    init_tracing()

    settings = KnowledgeSettings()
    embedder = OllamaEmbedder(
        base_url=settings.embedding_base_url, model=settings.embedding_model
    )
    store = PgVectorStore(settings.vector_dsn)
    rag = RAGPipeline(store, embedder)
    memory = MemoryService(store, embedder)

    knowledge_tools = build_knowledge_tools(rag, memory)
    knowledge_rules = [
        PermissionRule(tool=name, roles=frozenset({"agent"}))
        for name in knowledge_tools
    ]
    platform = build_platform(
        extra_tools=knowledge_tools, extra_rules=knowledge_rules
    )

    cost = CostTracker()
    agent = build_agent(ModelSettings(), platform.gateway, cost_tracker=cost)
    workflow = build_ticket_workflow(platform.gateway, cost_tracker=cost)
    app = create_app(platform, agent=agent, workflow=workflow, cost_tracker=cost)

    # 本地默认仅本机监听；容器化部署时用 AGENT_HOST=0.0.0.0 暴露端口。
    host = os.environ.get("AGENT_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_PORT", "8000"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
