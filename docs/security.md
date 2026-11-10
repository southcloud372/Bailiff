# 安全模型

Agent 系统的攻击面与传统 Web 不同：真正的风险不是 SQL 注入，而是**模型被说服去做它不该做的事**。
所以边界不能画在提示词上，必须画在**执行之前**。

## 一、信任边界

```
用户输入 ──┐
检索到的文档 ──┼─→ [模型] ──→ 工具调用请求 ──┬─→ [网关] ──→ 真实副作用
工具输出 ──┘         ↑ 不可信              │     ↑ 唯一的信任边界
                     └──────────────────────┘
```

三条铁律：

1. **模型输出一律视为不可信输入**。它可以建议调用 `write_file(path="../../etc/passwd")`，网关必须拒绝；
2. **提示词不是安全边界**。注入检测是降噪手段，不是防线；
3. **能力最小化优先于权限校验**。能不注册的能力就不注册（见 `AGENT_READ_ONLY`）。

## 二、对照 OWASP LLM Top 10

| 风险 | 本项目的位置 | 控制 |
|---|---|---|
| LLM01 提示词注入 | `guardrails/guards.py` | 输入侧规则检测（降噪）+ **网关兜底**（注入成功也拿不到越权能力） |
| LLM02 不安全输出处理 | `core/agent.py` | 输出脱敏；流式场景明确放弃逐块脱敏（见下方取舍） |
| LLM03 供应链 | `pyproject.toml` / `uv.lock` | 锁文件 + `uv sync --frozen`，CI 拒绝漂移 |
| LLM05 输出注入下游 | `tools/web.py` | 抓取内容只作为文本交给模型，不执行、不拼接进 shell/SQL |
| LLM06 过度代理 | `gateway/policy.py` | 角色→工具白名单；只读模式下写入能力不存在 |
| LLM07 系统提示泄露 | `core/agent.py` | 系统提示不含密钥；密钥只在进程环境变量里 |
| LLM08 向量库弱点 | `knowledge/` | 按 `user_id`/`tenant` 做 metadata 过滤（**必须先过滤再取 top_k**） |
| LLM10 无界消耗 | `router/budget.py`、`router/cost.py` | 预算硬限额（402）+ 工具结果 8KB 截断 + 抓取 512KB 截断 |

## 三、具体攻击与防线

### 1. SSRF（服务端请求伪造）

**最容易被打穿的一环**。`http_fetch` 的四层防护（详见 `tools/web.py` 顶部注释）：

| 攻击 | 防线 |
|---|---|
| 直接请求 `http://169.254.169.254/...`（云元数据，能拿 IAM 凭据） | DNS 解析后校验 IP 是否公网 |
| 白名单域名解析到 `10.0.0.5` | 同上（只校验字符串是不够的） |
| 白名单页面 302 跳到内网 | 手动跟随重定向 + 每跳复检 |
| 500MB 响应炸内存/账单 | 流式读取 + 字节上限 |

**已知残留风险**：校验与实际连接是两次 DNS 解析，短 TTL 下存在 rebinding 窗口。
彻底解决需把连接固定到已校验 IP（自定义 transport），当前未做。

### 2. 路径穿越

`gateway/security.py` 的 `resolve_within_root` 用 `Path.resolve()` 归一化后校验前缀，
拒绝 `../`、绝对路径、符号链接逃逸。所有文件工具都必须经过它——包括 MCP server 暴露的那份。

### 3. 跨用户数据泄露（IDOR）

三个入口全部做了身份绑定：

| 数据 | 隔离方式 |
|---|---|
| 会话（checkpoint） | 键为 `principal#thread_id`，别人的线程不在你的命名空间 |
| 长期记忆 | 向量 metadata 带 `user_id`，检索时前置过滤 |
| 知识库 | 按 `tenant`/`collection` 过滤（需部署方配置） |

**最容易漏的是第一个**：直接把客户端传来的 `thread_id` 当 checkpoint 键，猜中就能读走整段历史。

### 4. 拒绝服务与成本攻击

| 入口 | 防线 |
|---|---|
| 高频请求 | 令牌桶 / Redis 滑动窗口 |
| 单次超长输入 | 输入护栏 + 模型侧 max_tokens |
| 工具返回巨大 | 出口截断（8KB） |
| 死循环调工具 | LangGraph 递归上限 + 预算硬限额 |
| 语义缓存被污染 | 只对无状态 `/v1/ask` 开启 |

### 5. 流式输出的取舍（诚实记录）

SSE 一旦把 token 发出去就**无法撤回**。因此流式只做输入护栏，输出脱敏是非流式接口与审计兜底。
生产上若必须安全，可做「延迟窗口脱敏」：缓冲 N 个 token 再发，用延迟换可撤回。我们没有做，因为
它会把首 token 延迟从 1s 拉到 3s 以上——这是产品决策，不是技术缺陷。

## 四、上线前检查清单

```bash
# 全部应输出「不是示例值」
uv run python - <<'PY'
from agent_platform.gateway.config import Settings
s = Settings()
print("env:", s.env)
print("jwt 是示例值吗:", s.jwt_secret.startswith("dev-"))
print("只读模式:", s.read_only)
print("出网白名单:", s.http_allowed_hosts or "（关闭）")
PY
```

- [ ] `AGENT_ENV=prod`，`AGENT_JWT_SECRET` 已换成 32 位以上随机值
- [ ] `curl /readyz` 全部 `healthy`，且 `llm` 探针显示 `deepseek`
- [ ] 容器以非 root 运行（`docker run --user 10001`，镜像已默认）
- [ ] `data/` 目录不在镜像层里（`.dockerignore` 已排除）
- [ ] 公网实例开 `AGENT_READ_ONLY=true`（否则文件写入能力等于免费网盘）
- [ ] `AGENT_HTTP_ALLOWED_HOSTS` 是**最小集**，不是 `*`
- [ ] `DEEPSEEK_COST_LIMIT` 已设置（0 = 不限，等于不设防）
- [ ] 审计日志已落盘/采集，保留期符合合规要求
- [ ] 备份已验证**能恢复**（只备份不演练等于没备份）

## 五、报告漏洞

见 [SECURITY.md](../SECURITY.md)。请不要在公开 issue 里贴利用细节。
