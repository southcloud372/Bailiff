# Agent 平台 API 服务镜像（多阶段，uv 官方基础镜像）。
#
# 构建：docker build -f deploy/Dockerfile -t agent-platform .
# 运行：docker run -p 8000:8000 -e DEEPSEEK_API_KEY=... -e AGENT_HOST=0.0.0.0 agent-platform
#
# 注意：密钥（DEEPSEEK_API_KEY / AGENT_JWT_SECRET）通过运行时环境变量注入，
# 绝不打进镜像层；.env 已被 .gitignore 排除，不会进入 build context。

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# 先拷贝依赖清单并安装，充分利用 Docker 层缓存（源码变更不重装依赖）。
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# 再拷贝源码并安装本项目（editable，无需重新构建 wheel）。
COPY agent_platform ./agent_platform
COPY scripts ./scripts
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# 容器内监听 0.0.0.0；端口 8000 与 EXPOSE 对齐。
CMD ["python", "scripts/serve.py"]
