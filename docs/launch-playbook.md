# 把项目推到 2k star：可执行的冷启动方案

这份文档不谈"写好代码自然有人 star"。**2k star 是分发问题，不是代码问题**：
代码决定「来了之后会不会 star 并转发」，分发决定「有没有人来」。

先说结论性的数字关系（开源项目的一般规律，非承诺）：

| 阶段 | 典型转化率 | 意味着拿到 2k star 需要 |
|---|---|---|
| 曝光 → 点击仓库 | 2%–5% | 4 万–10 万次曝光 |
| 点击 → star | 8%–15% | 1.3 万–2.5 万次有效点击 |
| star → 实际运行 | 3%–8% | 60–160 人真的跑起来 |

所以真正要设计的是**两件事**：让人愿意点（一句话价值 + 可信度证据），让人点了就 star（30 秒内看到东西）。

---

## 一、已就位的资产（本轮已完成）

| 资产 | 状态 | 为什么它影响 star |
|---|---|---|
| 英文 README + 中文 README | ✅ | GitHub 的自然流量以英文搜索为主；中文社区靠 zh-CN 承接 |
| 60 秒零 Key 演示 | ✅ `docker-compose.demo.yml` | **最高杠杆**：不需要注册、不需要充值就能看到界面动 |
| 「两个真实坑」章节 | ✅ | 工程师 star 的动机是「这人踩过我踩的坑」，不是「功能多」 |
| MIT 许可证 | ✅ | 公司内试用不看许可证，但会看有没有 |
| Issue / PR 模板、CoC、SECURITY | ✅ | 显得"有人在维护"，直接影响 star 决策 |
| Makefile 单入口 | ✅ `make check` | 让人相信你自己在跑这些检查 |
| CI（ruff + mypy strict + pytest） | ✅ | README 上的绿勾是可信度 |
| CHANGELOG / ROADMAP | ✅ | 「有路线图」= 不是一次性玩具 |
| 15 篇学习文档 | ✅ `docs/learning/` | 中文社区传播素材，也是差异化（很少有项目附完整教学） |

## 二、还需要你亲手做的 6 件事（我做不了的部分）

1. **确认仓库地址与实际一致**：当前所有链接指向 `southcloud372/Bailiff`，
   若改用户名或仓库名，统一替换：
   ```bash
   grep -rl "southcloud372/Bailiff" . --exclude-dir=.git | xargs sed -i 's#southcloud372/Bailiff#<新路径>#g'
   ```
2. **仓库设置**（直接影响搜索曝光）：
   - Description：`Production-grade agent runtime: MCP-native tool gateway, durable checkpoints, cost caps, eval-gated CI.`
   - Topics：`ai-agents` `llm` `mcp` `langgraph` `agent-framework` `llmops` `rag` `pgvector` `fastapi` `observability`
   - 勾选 Discussions（问题有地方去，不会变成 issue 噪音）
   - Social preview 图（1280×640）：截控制台 + 左侧三个标签「governance / checkpoint / eval」
3. **打第一个 Release**（`v0.2.0`，附 CHANGELOG 段落）。有 Release 的仓库 star 转化明显更好。
4. **建 5 个 `good first issue`**：从 ROADMAP 的 help wanted 里拆，每个都写清楚验收标准。
5. **录一段 30 秒 GIF**：`docker compose -f docker-compose.demo.yml up --build` → 打开页面 → 点演示登录 → 提问 → 看到「思考过程」展开。放到 README 顶部。**这是转化率最高的一个素材。**
6. **开通 GitHub Sponsors 或至少写一句"Star 是最好的支持"**——不要小看这句话。

## 三、四周发布节奏

### 第 0 周：打磨（不要发）

- 找一个完全没看过这个项目的人（同学即可），让他按 README 从零跑。**你在旁边只看不说**，记录他卡住的每一步。README 的坑 90% 在这一步暴露。
- 修掉所有「跑不起来」类问题。第一次曝光只有一次机会。

### 第 1 周：中文圈首发（低风险练手）

| 渠道 | 写法要点 |
|---|---|
| V2EX / 程序员 | 标题带痛点："把 Agent 放进生产要补的 20% 我都实现了" |
| 掘金 | 直接搬「两个真实坑」那节，标题写 bug 现象（`reasoning_content` 400） |
| 即刻 / 小红书 | 控制台截图 + 一句话，不要贴代码 |

目的不是 star，是**拿到第一批真实反馈**，并验证「两个坑」这类内容有没有传播力。

### 第 2 周：英文技术社区（主战场）

| 渠道 | 注意 |
|---|---|
| Reddit `r/LocalLLaMA`、`r/AI_Agents`、`r/LangChain` | 用「踩坑复盘」口吻，不要用「我做了一个框架」口吻。后者会被当广告删。标题示例：*Two bugs that only appear when a reasoning model calls tools (and how we fixed them)* |
| Hacker News `Show HN` | 只在第 2 周之后发。标题：`Show HN: An agent runtime where every tool call is governed` |
| X / Twitter | 一图流：架构图 + 一句「orchestration decides, governance enforces」，@LangChainAI @deepseek_ai |

**关键**：每条内容都必须能独立成立（有信息量），而不是「求 star」。求 star 的帖子转化率极低且伤声誉。

### 第 3–4 周：内容复利

技术博客 10 个选题（每个都能独立引流，且都指向仓库）：

1. 为什么 `MemorySaver` 会让审批流程在重启后失效
2. 思考模型的 `reasoning_content` 必须回传：一个只在第二步炸的 bug
3. 一次失败的工具调用如何永久写坏一条会话线程
4. 单步 90% × 5 步 = 59%：为什么 Agent 上线要看 pass^k
5. MCP 网关该怎么设计（动态发现 + 命名空间 + 故障隔离）
6. 语义缓存为什么不能开在多轮对话上
7. `/healthz` 与 `/readyz` 混用会把「部分不可用」放大成「全部不可用」
8. Redis 限流故障时该放行还是该拒绝：fail-open vs fail-degraded
9. pgvector 的 `metadata @>` 过滤陷阱：先取 k 再过滤会召回塌陷
10. 一个 Agent 平台的最小可用架构（七层，附取舍）

发布节奏：每周 2 篇，中文发掘金/公众号，英文发 dev.to + Medium，结尾一句指向仓库。

## 四、会被问到的问题，先准备好答案

| 问题 | 回答要点 |
|---|---|
| 和 LangGraph 有什么区别？ | 它是库，这是运行时。补的是治理、持久化、成本、评测四件事。 |
| 和 Dify 比呢？ | Dify 是可视化编排给业务用；这是给要读代码、要审计的团队用。 |
| 生产跑过吗？ | **诚实回答**：没有大规模流量。但每个生产化决定都有 ADR 和测试，且写清了已知边界。 |
| 为什么用 DeepSeek 不用 OpenAI？ | 成本与中文能力，且是 OpenAI 兼容协议，换供应商只改环境变量。 |
| 代码是不是 AI 写的？ | 不重要。重要的是能不能解释每一处取舍——`docs/adr/` 和 README 的「两个坑」就是解释。 |

## 五、明确不要做的事

- **不要买 star / 互 star**：GitHub 会清，且被社区发现后项目直接社死。
- **不要在第一天发 HN**：没有讨论基础时，一次失败的 Show HN 几乎不能重发。
- **不要写「企业级」「赋能」「一站式」**：英文社区对这类词的容忍度是 0。
- **不要把所有功能都说成已完成**：README 里「Known limitations」比功能列表更能建立信任。
- **不要为了 star 加一堆没用的大词**（多智能体、A2A、AGI）——被追问一句就崩。

## 六、衡量

每两周记录一次：star 数、clone 数（Insights → Traffic）、Issue 数、外部 PR 数。
**clone/star 比**是最有意义的指标：高于 0.5 说明真的有人在用，低于 0.1 说明只是被收藏。
