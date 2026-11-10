# 一条命令完成一件常见事。Windows 上请用 `uv run ...` 的原生命令（见 README）。
#
#   make            # 列出所有目标
#   make setup      # 装依赖（Python + 前端）
#   make dev        # 本地起服务（http://localhost:8000）
#   make demo       # 零 Key 的 Docker 演示（含控制台）

.DEFAULT_GOAL := help
.PHONY: help setup dev web demo test lint fmt typecheck check eval build-web clean docker

PY := uv run python

help: ## 显示所有可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## 安装 Python 与前端依赖
	uv sync --extra prod
	cd web && npm install --no-audit --no-fund

dev: ## 启动 API + 控制台（需先 make setup）
	$(PY) scripts/serve.py

web: ## 前端开发服务器（热更新，代理到 8000）
	cd web && npm run dev

demo: ## 零 API Key 的 Docker 演示
	docker compose -f docker-compose.demo.yml up --build

ingest: ## 把示例语料灌进本地检索库（零依赖）
	$(PY) scripts/ingest.py

test: ## 跑测试
	$(PY) -m pytest -q

lint: ## Ruff 静态检查
	$(PY) -m ruff check .

fmt: ## Ruff 自动修复
	$(PY) -m ruff check . --fix

typecheck: ## mypy strict 类型检查
	$(PY) -m mypy agent_platform

check: lint typecheck test ## 提交前跑这一条（CI 同款）

eval: ## 跑评测集（需要 API Key）
	$(PY) scripts/eval.py

build-web: ## 构建控制台到 web/dist
	cd web && npm run build

docker: ## 构建生产镜像
	docker build -f deploy/Dockerfile -t bailiff:local .

clean: ## 清理缓存与构建产物
	rm -rf .pytest_cache .mypy_cache .ruff_cache web/dist
