# 运维手册

面向「已经上线、半夜被叫起来」的场景。日常开发看 [README](../README.md)，接口看 [api.md](api.md)。

## 一、部署形态

| 形态 | 适用 | checkpoint | 限流 | 缓存 | 命令 |
|---|---|---|---|---|---|
| 单机 | 本地开发 / 内网试用 / 演示 | sqlite | memory | off | `uv run python scripts/serve.py` |
| 单容器 | 小团队自托管 | sqlite + 卷 | memory | off | `docker compose -f docker-compose.demo.yml up -d` |
| 完整栈 | 生产多副本 | postgres | redis | 可选 | `docker compose up -d` |
| K8s | 规模化 | postgres | redis | 可选 | 见 roadmap |

**多副本的三个硬前提**（少一个就会出诡异问题）：

1. `AGENT_CHECKPOINT_BACKEND=postgres` ， 否则 A 副本的审批在 B 副本上不存在；
2. `AGENT_RATE_LIMIT_BACKEND=redis` ， 否则 N 个副本 = N 倍配额；
3. 会话亲和性或粘性路由 ， SSE 长连接不能在处理中途换副本。

## 二、配置矩阵

| 变量 | 默认 | 生产建议 | 配错的后果 |
|---|---|---|---|
| `AGENT_ENV` | `dev` | `prod` | 示例 JWT 密钥不会被拦，等于没有鉴权 |
| `AGENT_JWT_SECRET` | 示例值 | 32+ 位随机 | 同上 |
| `AGENT_LLM_BACKEND` | `auto` | `deepseek` | `auto` 缺 key 会静默降级到离线模型 |
| `DEEPSEEK_API_KEY` | 空 | 必填 | 无 key 时 prod 的 `/readyz` 不就绪（已拦） |
| `AGENT_CHECKPOINT_BACKEND` | `sqlite` | `postgres` | 多副本下状态互相看不见 |
| `AGENT_CHECKPOINT_DIR` | `./data/checkpoints` | 挂卷 | 容器重建后审批任务全丢 |
| `AGENT_RATE_LIMIT_BACKEND` | `memory` | `redis` | 配额被放大 N 倍 |
| `AGENT_CACHE_ENABLED` | `false` | 视流量 | 开在多轮接口上会串上下文 |
| `AGENT_READ_ONLY` | `false` | 演示环境 `true` | 未开时公网实例可被当免费网盘 |
| `AGENT_HTTP_ALLOWED_HOSTS` | 空 | 按需最小集 | 留空 = 不出网；填过宽 = SSRF 面变大 |
| `AGENT_RAG_BACKEND` | `auto`（本地） | `postgres` | 留在本地时多副本各有一份索引，检索结果不一致 |
| `DEEPSEEK_COST_LIMIT` | 0（不限） | 按租户设 | 不设限额时一个循环能把账单跑穿 |

## 三、探针与流量

```yaml
# K8s 语义（docker compose 同理）
livenessProbe:                 # 只证明进程活着，绝不查下游
  httpGet: { path: /healthz, port: 8000 }
readinessProbe:                # 查下游，失败即摘流量
  httpGet: { path: /readyz, port: 8000 }
```

**混用会把「部分不可用」放大成「全部不可用」**：把 `/readyz` 配成 liveness，Postgres 抖一下
所有副本会被同时重启，缓存冷启动叠加，雪崩。

## 四、备份与恢复

要备份的只有三样：

```bash
# 1. checkpoint（会话与未完成审批，丢了 = 用户上下文全断）
pg_dump -Fc "$AGENT_POSTGRES_DSN" > checkpoint.dump
# 2. 向量库（知识库与长期记忆，重建成本最高）
pg_dump -Fc -t vectors "$AGENT_POSTGRES_DSN" > vectors.dump
# 3. 文件沙箱（用户产物）
tar czf fs.tar.gz -C data/fs .
```

恢复顺序：`Postgres 实例 → 扩展（vector/pg_trgm）→ 表数据 → 启动应用（`ensure_schema` 建索引）`。
索引重建期间检索会慢，属预期。

**SQLite 形态的备份**用 `sqlite3 db ".backup out.sqlite"`，不要直接 `cp`，运行中拷贝可能拿到撕裂的快照。

## 五、升级

```bash
# 1. 先看会变什么
git fetch && git log --oneline HEAD..origin/main
# 2. 依赖必须与锁文件一致（这行是关键，避免「本地能跑线上装出另一套版本」）
uv sync --frozen --extra prod
# 3. 本地质量门（CI 同款）
make check
# 4. 滚动更新：新副本先通过 /readyz 再接流量，旧副本处理完存量 SSE 再退
```

**数据库变更**目前没有迁移框架（表结构由 `ensure_schema()` 幂等创建）。加字段前先确认：
加列可向后兼容，改类型/删列必须走双写+回填，否则滚动更新期间新旧副本会互相写坏数据。

## 六、故障处置

### 1. `/readyz` 503，`/healthz` 200

说明进程活着但依赖不可用。**不要去重启**，先看 `probes` 里哪个 `critical` 挂了：

| 失败探针 | 先查 |
|---|---|
| `checkpoint` | 数据库连通性、磁盘是否写满 |
| `auth` | 是否把示例密钥带上生产了 |
| `llm` | API key 是否失效（prod 下这是 critical） |
| `mcp` | 外部 MCP server 是否可达（non-critical，只降级） |

### 2. 某个用户「突然不会说话了」

按顺序排查（每步都有明确判据）：

1. `GET /v1/cost` 看是否触顶 → 402；
2. 日志搜 `rate_limited` → 429（多副本没配 Redis 时最像随机故障）；
3. 搜 `tool_failed` 且 `kind=upstream_error` → 某个依赖工具挂了，模型会解释「能力降级」；
4. 如果只有**一个会话**坏而新会话正常 → 历史被写坏，理论上 `_repair_dangling_tool_calls` 会自愈；日志里搜 `tool_call_history_repaired` 确认。

### 3. 账单异常增长

- `agent_llm_tokens_total` 按模型看是谁在涨；
- 是否有循环调用：搜同一 `thread_id` 连续 `tool_call` 记录；
- 是否有人把 `DEEPSEEK_COST_LIMIT` 设成了 0（不限）；
- 语义缓存是否误开在多轮接口上（会导致「每次都在重新推理」的反面，命中率异常低）。

### 4. SSE 流到一半断了

- 中间层缓冲：nginx 需要 `proxy_buffering off;`，否则 token 会被攒包；
- 超时：网关 `proxy_read_timeout` 要大于最长生成时间；
- 滚动发布：旧副本退出会切断长连接，客户端需实现「断线重连 + 从 checkpoint 续读」。

### 5. 容器 OOM

- `read_file` 读大文件 / `http_fetch` 拉大响应是最常见原因。工具出口已按 8KB 字符截断，抓取按 512KB 字节截断；
- 若仍 OOM，检查是否有并发大响应叠加，调低 `max_bytes` 或加并发闸门。

## 七、容量估算

单副本（2 vCPU / 2GB）实测量级：

| 场景 | 吞吐 | 瓶颈 |
|---|---|---|
| 纯流式对话（模型侧 1s 首 token） | 20–40 并发 | 上游 API 速率 |
| 含 2–3 次工具调用 | 8–15 并发 | 工具的 IO |
| 纯 `/v1/ask` + 缓存命中 | 100+ QPS | 无 |

扩容优先看 `agent_http_request_duration_seconds` 的 p95，而不是 CPU，Agent 服务的瓶颈通常在等上游。

## 八、日志

结构化 JSON（structlog），关键事件名：

| 事件 | 含义 |
|---|---|
| `http_request` | 访问日志，带 `request_id` / `route` / `status` / `duration_ms` |
| `tool_failed` | 工具失败，带 `tool` / `kind` / `error` |
| `tool_call_history_repaired` | 检测到并修复了悬空 tool_calls（说明曾有中断） |
| `chat_stream_failed` | 流式生成异常中断，带堆栈 |
| `checkpoint_setup_failed` | Postgres 建表失败（多副本下会导致状态不一致，需立即处理） |
| `cost_lookup_failed` | 价格表里找不到模型 → 成本恒为 0，是**最难发现的成本事故** |
