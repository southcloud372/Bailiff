"""启动网关服务。用法：uv run python scripts/serve.py

组装顺序：网关（build_platform）→ 模型/Agent（build_agent）→ HTTP（create_app）。
Agent 未配置模型时（无 DEEPSEEK_API_KEY）仍可启动，/v1/chat 返回 503，其余端点正常。
"""

from __future__ import annotations

import os

import uvicorn

from agent_platform.api.app import create_app
from agent_platform.core.agent import build_agent
from agent_platform.core.workflow import build_ticket_workflow
from agent_platform.gateway.config import ModelSettings
from agent_platform.gateway.wiring import build_platform
from agent_platform.router.cost import CostTracker


def main() -> None:
    platform = build_platform()
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
