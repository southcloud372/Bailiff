"""启动网关服务。用法：uv run python scripts/serve.py"""

from __future__ import annotations

import uvicorn

from agent_platform.api.app import create_app


def main() -> None:
    app = create_app()
    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
