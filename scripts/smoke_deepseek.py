"""DeepSeek 连通性冒烟测试：验证 API key 与最小模型调用闭环。

用法：
    uv run python scripts/smoke_deepseek.py

不依赖项目其它模块，直接以 httpx 调用 OpenAI 兼容的 /chat/completions 接口。
后续 Phase 2 会用 LiteLLM 模型路由替换这里的裸调用。
"""

from __future__ import annotations

import os
import sys

import httpx
from dotenv import load_dotenv


def main() -> int:
    load_dotenv()

    api_key = os.getenv("DEEPSEEK_API_KEY")
    base_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    if not api_key:
        print("[错误] 未找到 DEEPSEEK_API_KEY，请检查 .env 文件", file=sys.stderr)
        return 1

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是企业级 Agent 平台的基础设施冒烟测试。"},
            {"role": "user", "content": "用一句话说明你是什么模型，并确认你能正常返回。"},
        ],
        "max_tokens": 64,
        "temperature": 0.3,
    }
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(f"[错误] HTTP {exc.response.status_code}: {exc.response.text}", file=sys.stderr)
        return 1
    except httpx.RequestError as exc:
        print(f"[错误] 请求失败: {exc}", file=sys.stderr)
        return 1

    data = resp.json()
    choice = data["choices"][0]["message"]
    usage = data.get("usage", {})

    print("[成功] DeepSeek 连通正常")
    print(f"  model             : {data.get('model')}")
    print(f"  prompt_tokens     : {usage.get('prompt_tokens')}")
    print(f"  completion_tokens : {usage.get('completion_tokens')}")
    print(f"  total_tokens      : {usage.get('total_tokens')}")
    print(f"  reply             : {choice['content'].strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
