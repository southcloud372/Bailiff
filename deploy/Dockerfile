# Agent 平台 API 服务镜像（多阶段构建）。
#
# 构建：docker build -f deploy/Dockerfile -t agent-platform .
# 运行：docker run -p 8000:8000 \
#          -e DEEPSEEK_API_KEY=... -e AGENT_HOST=0.0.0.0 \
#          -e AGENT_CHECKPOINT_BACKEND=postgres \
#          -e AGENT_POSTGRES_DSN=postgresql://agent:agent@db:5432/agent_platform \
#          -e AGENT_RATE_LIMIT_BACKEND=redis \
#          -e AGENT_REDIS_URL=redis://redis:6379/0 \
#          agent-platform
#
# 要点：
# - 密钥只通过运行时环境变量注入，绝不打进镜像层；
# - builder 与 runtime 分离，最终镜像不含 uv/编译器；
# - 以非 root 用户运行（容器逃逸后的第一道限制）；
# - `--extra prod` 安装 Postgres checkpoint 与 Redis 客户端（生产必需，本地可选）。

# ── 构建阶段 ────────────────────────────────────────────────────────────────
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# 先只拷贝依赖清单：源码变更不会触发依赖重装（层缓存的关键）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra prod

COPY agent_platform ./agent_platform
COPY scripts ./scripts
RUN uv sync --frozen --no-dev --extra prod

# ── 前端阶段 ────────────────────────────────────────────────────────────────
# 控制台打进同一个镜像：部署形态收敛为「一个容器、一个端口」，不需要额外 nginx。
# 没装 node 的机器也能 `docker build` 出完整可演示的镜像。
FROM node:22-alpine AS console

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# ── 运行阶段 ────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

# 时区与 CA 证书：调用 HTTPS 模型接口需要
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app
# 控制台构建产物由 FastAPI 直接托管（见 api/app.py 的 console 挂载）
COPY --from=console --chown=app:app /web/dist ./web/dist

# 运行时可写目录（SQLite checkpoint / 文件沙箱根目录）
RUN mkdir -p /app/data/checkpoints /app/data/fs && chown -R app:app /app/data

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app
EXPOSE 8000

# 存活探针（容器内自检；k8s 里用 /readyz 判流量）
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2)"

CMD ["python", "scripts/serve.py"]
