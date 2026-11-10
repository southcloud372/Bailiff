"""企业级多智能体编排平台（校招旗舰项目）。

参考 2026 七层 Agent 架构：
协议(MCP) → 编排(LangGraph) → 治理 → 模型路由 → 记忆 → 数据，
贯穿横切的可观测与评测。
"""

from __future__ import annotations

import os

__version__ = "0.1.0"

# LiteLLM 默认在 **import 时**去 GitHub 拉最新的价格表；离线/被墙环境下会重试三次、
# 让 `import agent_platform` 平白多花 20 秒（冷启动直接超时）。这里在任何子模块
# import litellm 之前强制走内置价格表，把网络依赖从启动路径上摘掉。
# 需要最新价格时（如供应商调价）显式设 LITELLM_LOCAL_MODEL_COST_MAP=0。
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
