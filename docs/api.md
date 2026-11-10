# API 参考

所有接口都在同一个 FastAPI 应用上，默认 `http://localhost:8000`。交互式文档在 `/docs`（OpenAPI）。

## 认证

除探针与（dev 环境的）演示登录外，所有接口都需要 Bearer Token：

```
Authorization: Bearer <JWT>
```

- 算法 HS256，密钥来自 `AGENT_JWT_SECRET`；
- 载荷：`sub`（principal id）、`roles`（角色数组）、`exp`；
- 生产由 IdP / 网关注发，本项目只做**验证**，不承担签发（`/v1/auth/dev-token` 仅 dev 存在）。

**principal 决定三件事**：工具权限（角色）、限流配额、会话命名空间。

## 通用约定

| 项目 | 约定 |
|---|---|
| 内容类型 | `application/json`；流式为 `text/event-stream` |
| 追踪 | 响应头带 `x-request-id`，请求头传入则沿用（跨服务串链路） |
| 错误体 | `{"detail": "人类可读原因"}` |
| 身份 | 401 = 没带/无效 token；403 = 有身份但角色不够；429 = 限流；402 = 预算超限 |

---

## 探针与指标

### GET /healthz

存活探针。**只回答「进程还在不在」**，绝不触碰任何下游。容器重启判定用它。

```json
{"status": "ok"}
```

### GET /readyz

就绪探针。并发探测所有依赖，任一 **critical** 失败返回 **503**（让负载均衡摘流量，而不是重启进程）。

```json
{
  "ready": true,
  "probes": [
    {"name": "gateway", "healthy": true, "critical": true, "detail": "6 个工具已注册", "latency_ms": 0.03},
    {"name": "auth", "healthy": true, "critical": true, "detail": "ok", "latency_ms": 0.58},
    {"name": "llm", "healthy": true, "critical": false, "detail": "deepseek", "latency_ms": 0.02},
    {"name": "checkpoint", "healthy": true, "critical": true, "detail": "backend=SqliteCheckpointSaver", "latency_ms": 0.17}
  ]
}
```

关键语义：

- `llm` 探针在 dev 是 non-critical（允许离线演示），**在 prod 是 critical**——带着不会推理的模型上线，接口全 200 但回答全是废话，是最隐蔽的故障；
- `auth` 探针在 prod 拒绝示例密钥，即「配置错误无法上线」，而不是靠人记得改。

### GET /metrics

Prometheus 文本格式。零额外依赖（自己实现的文本渲染，不引 prometheus_client）。

主要指标：`agent_http_requests_total`、`agent_http_request_duration_seconds`、`agent_tool_calls_total`（含 `status` 标签）、`agent_llm_tokens_total`。

---

## 对话

### POST /v1/chat

有状态多轮对话，回复一次性返回。

```json
{"message": "在 note.txt 写入 hello，然后读回来", "thread_id": "conv-42"}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `message` | 是 | 用户输入 |
| `thread_id` | 否 | 会话 id。不传则用 principal 的默认会话 |

```json
{"reply": "已写入并读回：hello"}
```

### POST /v1/chat/stream

SSE 流式。事件**分类型**，因为思考模型会先产出一大段推理：

| 事件 | 载荷 | 说明 |
|---|---|---|
| `reasoning` | `{"type":"reasoning","text":"..."}` | 推理片段（前端可折叠） |
| `delta` | `{"type":"delta","text":"...","delta":"..."}` | 正文片段（`delta` 键为兼容旧客户端保留） |
| `tool_call` | `{"type":"tool_call","name":"read_file","args":"..."}` | 发起工具调用 |
| `tool_result` | `{"type":"tool_result","name":"read_file","text":"..."}` | 工具返回（截断到 2KB） |
| 结束 | `data: [DONE]` | — |
| 错误 | `{"error":"...","detail":"..."}` | `detail` 仅 dev 返回 |

```bash
curl -N -X POST localhost:8000/v1/chat/stream \
  -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -d '{"message":"数到三"}'
```

**为什么必须分类型**：只转发正文时，推理模型的「思考十几秒」在界面上就是空白，用户会以为服务挂了。工具调用同理——花 8 秒调工具却没有任何进度提示，体验等同卡死。

### POST /v1/ask

无状态单轮问答。**唯一允许走语义缓存的对话接口**——因为它不带多轮上下文，缓存不会导致「拿别人的上下文回答你」。

---

## 工具

### GET /v1/tools

返回当前 principal 可用的工具名。

```json
{"tools": ["list_dir", "read_file", "write_file", "search_knowledge", "remember", "recall"]}
```

**这个清单就是 Agent 能看见的全部能力**。只读模式下 `write_file` 不会出现在这里，因为它在网关里根本没注册（能力不存在，而不是「存在但拒绝」）。

### POST /v1/call

直接调用工具（不经模型），走完整的治理四件套：限流 → 权限 → 存在性 → 执行 → 审计。

```json
{"tool": "read_file", "arguments": {"path": "a.txt"}}
```

---

## 成本与缓存

### GET /v1/cost

```json
{
  "total_cost": 0.00016845,
  "total_prompt_tokens": 859,
  "total_completion_tokens": 66,
  "total_cached_tokens": 0,
  "cache_hit_rate": 0.0,
  "by_model": {"deepseek-flash": {"cost": 0.00016845, "prompt_tokens": 859, "completion_tokens": 66, "cached_tokens": 0}}
}
```

### GET /v1/cache · DELETE /v1/cache

语义缓存统计 / 清空。仅当 `AGENT_CACHE_ENABLED=true` 时有效，否则返回未启用。

---

## 人工审批（HITL）

### POST /v1/tickets

发起一个会**暂停等待人工确认**的工作流。

```json
{"message": "把这个工单写进 ticket.txt", "thread_id": "tk1"}
```

```json
{
  "status": "interrupted",
  "thread_id": "tk1",
  "interrupt": {"action": "write_file", "args": {"path": "ticket.txt", "content": "..."}}
}
```

此时**副作用尚未发生**。状态已落 checkpoint——这是它能在进程重启后继续的前提（`MemorySaver` 做不到）。

### POST /v1/tickets/{thread_id}/resume

```json
{"approve": true, "note": "同意"}
```

`approve=false` 时工作流走拒绝分支，同样落审计。

> 会话键是 `principal#thread_id`。路径里传的是**你看到的 id**，服务端负责命名空间化——所以审批不会恢复到别人的（或空的）状态上。

---

## MCP

### POST /mcp/fs/mcp

把文件系统暴露成标准 MCP server（stateless Streamable HTTP，协议修订 2026-07-28）。
用任何 MCP 客户端连它，工具调用同样经过沙箱与审计。

```bash
npx @modelcontextprotocol/inspector   # 连 http://localhost:8000/mcp/fs/mcp
```

---

## 演示登录（仅 dev）

### POST /v1/auth/dev-token

`AGENT_ENV=dev` 时才注册。生产环境这条路由**不存在**（404），不是靠鉴权挡。

```json
{"principal_id": "demo", "roles": ["agent"]}
```

```json
{"token": "eyJ...", "principal_id": "demo", "roles": ["agent"], "expires_in": 3600}
```

---

## 错误码

| 状态 | 场景 | 客户端该做什么 |
|---|---|---|
| 400 | 请求体不合法 | 修参数 |
| 401 | 缺 token / 签名错 / 过期 | 重新登录 |
| 402 | 预算超限（`DEEPSEEK_COST_LIMIT`） | 提示用户或调高额度 |
| 403 | 角色无权调用该工具 | 申请权限，不要重试 |
| 404 | 路由不存在（含 prod 下的 dev-token） | 检查环境 |
| 429 | 限流 | 退避重试（带 `Retry-After` 更佳） |
| 503 | 未配置模型 / 工作流，或 `/readyz` 不就绪 | 查看 `/readyz` 定位具体依赖 |

## 版本与兼容

- ABI 层面只增不改：新增字段安全，删除字段会走 deprecation（响应头 `Deprecation`）；
- SSE 事件类型是枚举，客户端必须**忽略未知类型**——这是流式协议向前兼容的唯一方式。
