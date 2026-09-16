# web · 轻量前端（React + Vite + TypeScript）

Agent Platform 控制台：网关健康检查 + 工具列表 + Agent 对话。

## 运行

先启动后端（仓库根目录）：

```bash
uv run python scripts/serve.py
uv run python scripts/mint_token.py demo agent   # 生成演示 token，粘贴到控制台
```

再启动前端：

```bash
cd web
npm install
npm run dev      # http://localhost:5173，/v1 与 /healthz 自动代理到 127.0.0.1:8000
```

## 构建

```bash
npm run build    # tsc 类型检查 + vite 打包，产出 web/dist/
npm run preview  # 预览构建产物
```

## 设计说明

- **认证**：前端只「持有并携带」JWT（Bearer），签发在服务端完成；多租户身份
  （principal）贯穿 Agent → 网关权限检查，不落空。
- **代理**：开发态由 Vite 把 `/v1`、`/healthz` 转发到后端，同源免 CORS；生产由
  网关/反代（见 `deploy/`）统一路由。
- **流式**：当前 `/v1/chat` 为一次性返回；SSE 流式输出列为后续增强项。
