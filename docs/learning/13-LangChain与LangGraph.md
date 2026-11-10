# 13 · LangChain 与 LangGraph：从「调模型」到「可暂停、可恢复的图」

这篇解决一个具体困惑：LangChain 和 LangGraph 到底谁管什么，为什么 v1 只推 `create_agent`，
以及一个「能跑」的 Agent 和一个「重启后还能接着跑」的 Agent 差在哪。
读完后你能：用 `create_agent` 二十行搭出 ReAct 助手；用 `StateGraph` 手写出同一套循环；
说清 checkpoint / interrupt 的存储与恢复机制；并讲明白本项目为什么同时保留自主 Agent 与确定性工作流。

> 版本口径（与 `pyproject.toml`、`docs/stack-verification-2026-11.md` 一致）：
> LangGraph `1.2.20`、LangChain `1.4.9`、langchain-core `1.7.7`、langchain-openai `1.7.4`。

## 0. 先把四个包分清

初学者最容易混的是「装了 `langchain` 是不是就什么都有了」。实际上是分层依赖：

| 包 | 角色 | 你可以不装吗 |
|---|---|---|
| `langchain-core` | 抽象层：`BaseChatModel`、`BaseMessage`、`BaseTool`、`Runnable`、回调 | 不能，所有包都依赖它 |
| `langchain` | 高层入口：`create_agent`、middleware、结构化输出 | 只用 LangGraph 时可以 |
| `langchain-openai` | 具体模型适配器（DeepSeek 走 OpenAI 兼容协议） | 换别家就换这个包 |
| `langgraph` | 图执行引擎：`StateGraph`、checkpoint、`interrupt` | 用 `create_agent` 也绕不开，它就建在图引擎上 |

一句话：**LangChain 给你「零件和装配线」，LangGraph 给你「运行时」**。
`create_agent` 返回的不是某个 `AgentExecutor` 对象，而是一个编译好的 `CompiledStateGraph`——
这是理解 v1 的关键，后面所有能力（持久化、中断、流式）都从这句话长出来。

## 1. LangChain v1 的五个核心抽象

### 1.1 chat model：唯一的模型入口

```python
from langchain_openai import ChatOpenAI

model = ChatOpenAI(
    model="deepseek-flash",
    base_url="https://api.deepseek.com/v1",   # DeepSeek 的 OpenAI 兼容端点
    api_key="sk-...",                          # 生产用 SecretStr / 环境变量
    temperature=0.3,
)
model.invoke("用一句话解释什么是向量检索")
```

四个调用面：`invoke` / `batch` / `stream` / `astream`（异步）。抽象成 `BaseChatModel` 之后，
上层代码不关心背后是 DeepSeek、OpenAI 还是本地 Ollama——本项目 `core/agent.py:109` 的
`build_model()` 就是二十行做这件事，顺便挂上计费与追踪回调。

### 1.2 message：模型世界的唯一货币

| 类型 | 谁产生 | 关键字段 |
|---|---|---|
| `SystemMessage` | 你 | 角色设定、约束 |
| `HumanMessage` | 用户 | `content` |
| `AIMessage` | 模型 | `content`、**`tool_calls`** |
| `ToolMessage` | 你的执行器 | `content`、**`tool_call_id`** |

`tool_calls` 与 `tool_call_id` 是一对：模型说「我要调 `get_order_status`，参数是 A123，调用号 call_1」，
你必须回一条 `tool_call_id="call_1"` 的消息。**对不上号，模型就无法把结果和请求关联起来**，
这是手写 ReAct 循环第一个会踩的坑。

### 1.3 tool：给模型看的「说明书 + 实现」

```python
from langchain_core.tools import tool

@tool
def get_order_status(order_id: str) -> str:
    """按订单号查询物流状态。"""      # docstring 就是模型看到的 description
    return ORDERS.get(order_id, "未找到该订单")
```

`@tool` 从函数签名 + docstring 生成 JSON Schema，模型只看见 Schema，看不见函数体。
想要更严格的参数约束就用 `StructuredTool.from_function(args_schema=SomeModel)`——
本项目 `core/agent.py:128` 的 `build_tools()` 用的正是这条路，因为 Pydantic 模型能写字段级
`Field(description=...)`，比 docstring 更可控。

### 1.4 runnable：所有东西都能 `|` 起来

`prompt | model | parser` 是 LCEL 的写法，v1 仍然可用，因为「可组合」这件事本身没错。
但注意分工：**LCEL 适合线性数据流（RAG 管道、格式化），不适合带循环和分支的 Agent**。
Agent 有「模型→工具→模型→…」的不定轮次循环，用 `|` 表达会很别扭，这才是 LangGraph 存在的理由。

### 1.5 middleware：v1 唯一的「改行为」入口

v1 把扩展点收敛成 middleware，而不是让你继承执行器。可用的装饰器有
`@before_agent`、`@before_model`、`@after_model`、`@after_agent`、`@wrap_model_call`、`@wrap_tool_call`：

```python
from langchain.agents.middleware import before_model, wrap_tool_call

@wrap_tool_call
async def log_tool(request, handler):
    print("调用工具:", request.tool_call["name"])
    return await handler(request)
```

现成中间件覆盖了生产里 80% 的横切需求：`SummarizationMiddleware`（上下文压缩）、
`ToolRetryMiddleware`、`ModelFallbackMiddleware`、`PIIMiddleware`、`HumanInTheLoopMiddleware`。
**为什么这点重要**：老版本要加「工具失败重试」，只能 fork 一份 AgentExecutor 或写子类；
现在是一行 `middleware=[ToolRetryMiddleware(...)]`，升级 LangChain 时不会 conflicts。

## 2. 为什么 v1 只推 `create_agent`

`create_react_agent` 还在（`langgraph.prebuilt` 里能 import），但它已经退为**兼容层**：
保留是为了不破坏存量代码，新代码不该再引入。原因有四条：

1. **收敛实现**：v0 有 `initialize_agent`、`AgentExecutor`、各种 `create_*_agent`，
   每种一套循环、一套错误处理，HITL 和流式要各写一遍。v1 只有一个入口、一份循环。
2. **直接产出图**：返回 `CompiledStateGraph`，于是 checkpoint、`interrupt`、`stream_mode`、
   time travel 全部免费继承。**你不用 LangGraph，也在用 LangGraph**。
3. **扩展点唯一**：middleware 是官方指定的改法，避免「升级即冲突」的继承链。
4. **结构化输出内联**：v1 把结构化输出放进主循环，不再单独加节点，
   并提供 `ToolStrategy`（伪工具调用）与 `ProviderStrategy`（provider 原生）。
   DeepSeek 不支持 `response_format`，本项目因此用 `method="function_calling"`
   （见 `core/workflow.py:74`）。

代价也要说清：`create_agent` 把循环藏起来了。**当流程需要「确定的审批门」「固定的三步走」时，
它就不合适**——这正是第 6 节要讲的取舍。

## 3. 同一个需求，两种写法

需求：用户问「订单 A123 到哪了」，模型自己决定是否查工具，查到后回答，支持多轮。

### 3.1 写法 A：`create_agent`（约 20 行）

```python
from langchain.agents import create_agent
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

agent = create_agent(
    model,
    [get_order_status],
    system_prompt="你是订单助手，查不到就直说，不要编造物流信息。",
    checkpointer=InMemorySaver(),        # 想要多轮，必须给 checkpointer
)

cfg = {"configurable": {"thread_id": "user-42"}}
out = agent.invoke({"messages": [HumanMessage("订单 A123 到哪了")]}, cfg)
print(out["messages"][-1].content)
```

你没写循环、没写 `tool_calls` 解析、没写 `ToolMessage` 回填，但图里已经全有了。

### 3.2 写法 B：`StateGraph`（约 40 行，等价语义）

```python
from typing import Annotated, TypedDict
from langchain_core.messages import AnyMessage, ToolMessage
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import InMemorySaver

class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]   # 只增不改，见 4.1

def call_model(state: AgentState) -> dict:
    ai = model.bind_tools([get_order_status]).invoke(state["messages"])
    return {"messages": [ai]}

def call_tools(state: AgentState) -> dict:
    last = state["messages"][-1]
    outs = []
    for call in last.tool_calls:                          # 可能一次调多个
        result = get_order_status.invoke(call["args"])
        outs.append(ToolMessage(content=str(result), tool_call_id=call["id"]))
    return {"messages": outs}

def should_continue(state: AgentState) -> str:
    return "tools" if state["messages"][-1].tool_calls else END

builder = StateGraph(AgentState)
builder.add_node("model", call_model)
builder.add_node("tools", call_tools)
builder.add_edge(START, "model")
builder.add_conditional_edges("model", should_continue, {"tools": "tools", END: END})
builder.add_edge("tools", "model")                        # 回到模型，形成循环
graph = builder.compile(checkpointer=InMemorySaver())

graph.invoke({"messages": [HumanMessage("订单 A123 到哪了")]}, cfg)
```

### 3.3 怎么选

| 维度 | `create_agent` | `StateGraph` |
|---|---|---|
| 代码量 | 20 行 | 40 行起，流程越长越划算 |
| 流程可控性 | 循环内部不可见 | 每个节点、每条边都可控 |
| 加审批门 | 用 middleware 或 `interrupt_before` | 显式加节点，语义最清楚 |
| 排查问题 | 看 trace 里的 `model`/`tools` 节点 | 断点就是你写的节点名 |
| 适用 | 开放式问答、工具调用不确定 | 合规流程、固定步骤、必须人工卡点 |

判断口径一句话：**「下一步走哪，是模型决定还是业务决定？」**
模型决定 → `create_agent`；业务决定 → `StateGraph`。

## 4. LangGraph 的六个零件

### 4.1 State 与 reducer：状态怎么合并

State 是一个 `TypedDict`。节点返回的 dict 默认**覆盖**同名字段；
想让字段「累加」就必须给它一个 reducer：

```python
from typing import Annotated
from langgraph.graph.message import add_messages

class State(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]  # 追加并按 id 去重/更新
    steps: Annotated[int, lambda a, b: a + b]            # 自定义：累加
    draft: str                                            # 无 reducer：覆盖
```

`add_messages` 不只是 `append`：它按消息 `id` 做 upsert，所以「同一条消息被改写」不会产生重复。
子图与父图共享字段时，两边 reducer 必须一致，否则合并行为会出乎意料。

### 4.2 节点与边

节点就是 `(state) -> dict` 的函数（sync/async 都行），返回值是**状态增量**，不是完整状态。
边分三种：`add_edge`（固定）、`add_conditional_edges`（按函数返回值查表）、
`START`/`END`（虚拟起止点）。图编译前是声明，`compile()` 之后才可执行。

### 4.3 条件边

`add_conditional_edges("model", should_continue, {"tools": "tools", END: END})` 的第三个参数是
**路由表**：函数只返回 key，映射到节点名。好处是路由逻辑与拓扑解耦，图可视化时能画出所有分支。
不传路由表也能跑（返回值直接当节点名），但节点多了容易写错字符串。

### 4.4 子图：把一张图当节点用

```python
sub = build_research_subgraph().compile()      # 已编译的子图
builder.add_node("research", sub)              # 直接当节点
```

父子图的状态键要么兼容、要么用一层包装函数做转换。子图的 checkpoint 落在独立的
`checkpoint_ns` 里，所以中断能精确到「父图的第 2 步、子图的第 1 个节点」。
本项目暂未拆子图，但多智能体协同（一个 agent 作为另一个的节点）走的就是这条路。

### 4.5 `interrupt()` 与 `Command(resume=)`：真正的暂停

```python
from langgraph.types import interrupt, Command

def gate(state: dict) -> dict:
    decision = interrupt({"action": state["tool_name"], "args": state["tool_args"]})
    return {"approved": bool(decision.get("approve"))}

cfg = {"configurable": {"thread_id": "t-1"}}
r1 = graph.invoke({"tool_name": "write_file"}, cfg)
print(r1["__interrupt__"][0].value)     # 审批上下文，返回给前端

r2 = graph.invoke(Command(resume={"approve": True}), cfg)   # 从这里接着跑
```

三个必须记住的语义：

1. **`interrupt()` 靠抛异常实现暂停**，所以调用点之前的代码在恢复时会**重新执行一遍**。
   中断前不要做有副作用的操作（写库、发消息），否则恢复时做两次。
2. 恢复值就是 `interrupt()` 的返回值，不是塞进 state 的某个魔法字段。
3. 一次运行可能有多个中断（并行分支），`r1["__interrupt__"]` 是列表；
   恢复时要按 `interrupt.id` 对应，别假设只有一个。

### 4.6 `stream_mode`：流什么给你

| 取值 | 每次产出 | 典型用途 |
|---|---|---|
| `values` | 完整状态快照 | 调试、前端整页刷新 |
| `updates` | 单节点增量 | 展示「正在查资料」 |
| `messages` | `(消息分块, 元数据)` | token 级打字机 |
| `custom` | 你在节点里 `writer()` 写的东西 | 业务进度条 |
| `checkpoints` / `tasks` / `debug` | 执行细节 | 运维排查、轨迹回放 |

本项目 `core/agent.py:276` 用 `stream_mode="messages"`，并且**只取 `AIMessage`/`AIMessageChunk`**——
因为工具调用分块和工具结果也会流出来，不筛就会把 JSON 参数念给用户听。

## 5. checkpoint 到底是什么，没有它会怎样

### 5.1 一次 checkpoint 存了什么

不是「一段聊天记录」，而是**图在某个 super-step 结束时的完整可恢复快照**：

- **channel 值**：每个状态字段的当前值（`messages` 是整条列表）；
- **channel 版本**：每个字段的版本号，用于判断「这次要不要重算」；
- **pending writes**：本轮已算完、但下游还没消费的节点输出（并行分支会用到）；
- **元数据**：`thread_id`、`checkpoint_ns`（子图命名空间）、`checkpoint_id`、
  `parent_checkpoint_id`。

`(thread_id, checkpoint_ns, checkpoint_id)` 是主键，`parent_checkpoint_id` 串成一条链。
**有链就能回到任意一步**：`graph.get_state_history(cfg)` 拿到全部快照，
指定 `checkpoint_id` 重放就是 time travel。

### 5.2 没有它会怎样

| 场景 | 用 `MemorySaver` 的后果 |
|---|---|
| 发版 / OOM / K8s 驱逐 | 进行中的会话全丢，用户回来发现「它失忆了」 |
| 两个副本 + 负载均衡 | 下一轮打到另一台，`thread_id` 在那边不存在，直接从头开始 |
| HITL 审批 | 工单停在 `interrupt()`，人第二天点同意，进程早重启了，审批上下文没了 |
| 失败重试 | 只能整条重跑，不能从失败节点续跑，钱和时间都白花 |
| 排查 | 没有历史快照，无法复现「它当时为什么这么答」 |

所以「checkpoint 是选配」是错的。**只要涉及多轮、人工审批、多副本，它就是硬门槛。**

### 5.3 本项目的三级后端

`core/checkpoint.py:296` 的 `build_checkpointer(backend, ...)` 用一个环境变量切三档：

| 档位 | 实现 | 适用 | 代价 |
|---|---|---|---|
| `memory` | `InMemorySaver` | 单测、CI | 重启即丢，不能多副本 |
| `sqlite` | 自研 `SqliteCheckpointSaver`（写穿） | 本地开发、单机部署 | 单机；多进程写同一文件不安全 |
| `postgres` | `AsyncPostgresSaver` + `AsyncConnectionPool` | 生产多副本 | 需要 PG 与 `--extra prod` 依赖 |

这个设计的意图是**「clone 下来就能跑」**：不装 Docker 也能对话（sqlite），
上生产只改一个环境变量（postgres），业务代码零改动。ADR-0005 记录了这条决策。

### 5.4 为什么 SQLite 是「写穿」而不是重写协议

自己实现 `BaseCheckpointSaver` 要处理增量 channel、版本比对、pending writes 合并——
这套语义极易写错，而且写错了不会立刻报错，只会在「恢复后状态少一条」时炸。

本项目的做法（`core/checkpoint.py:82`）：

1. **继承官方 `InMemorySaver`**，语义 100% 复用；
2. 在 `put` / `put_writes` / `delete_thread` 之后，把「刚变化的那部分」写进 SQLite；
3. 读的时候按 `thread_id` **惰性 hydrate** 回内存（`_ensure_loaded`），避免启动全表扫；
4. 内存是一级缓存，SQLite 是持久层。

三张表对应官方模型：`checkpoints`（快照 + parent 链）、`checkpoint_writes`（pending writes）、
`checkpoint_blobs`（按 channel+version 去重的值，避免重复存大对象）。
注意 `delete_thread` 必须三张表一起删，否则会留下孤儿 blob，表只涨不降。

### 5.5 Postgres 分支的两个坑

- **`compile()` 是同步的，`AsyncPostgresSaver.setup()` 是异步的**。所以 `build_checkpointer`
  返回一个 `_PostgresCheckpointer` 门面，把 `setup()` 暴露给 FastAPI `lifespan` 调用（幂等建表）。
- **连接池必须由 saver 持有并在关闭时释放**，否则每次重建应用都漏一批连接。

还有一个容易被忽略的运维债：无状态接口 `/v1/ask` 每次新建 thread，如果用完不删，
checkpoint 表会被「一问一答」撑爆。`core/agent.py:248` 的 `_drop_thread()` 就是还这笔债的。

## 6. 本项目：两种编排为什么都要

### 6.1 `core/agent.py`：自主 ReAct

模型自己决定调哪个工具、调几次、什么时候收尾。工具统一经网关：

```python
async def _run(**kwargs):
    principal = current_principal.get()            # 身份来自 contextvar，不写死在 Agent 上
    return await gateway.call(principal, name, dict(kwargs))
```

两个设计点值得讲：

- **身份用 contextvar 传**，而不是构造 Agent 时绑定。这样同一个 Agent 实例可以服务所有用户，
  权限检查落在网关那一层，工具执行时才知道「现在是谁在调」。
- **工具清单是动态的**：`build_tools()` 先看网关注册了哪些（`gateway.tool_names()`），
  有 `search_knowledge` 才把知识库工具挂上去。这样关掉 RAG 不需要改 Agent 代码。

### 6.2 `core/workflow.py`：确定性工作流

`classify → (approval) → execute → answer`，四个节点、两条条件边。
为什么不用 `create_agent`？因为这里的「下一步」是**业务规则**决定的：

- 只有 `sensitive` 意图才进审批节点，这是合规要求，不能交给模型即兴判断；
- 审批通过才 `execute`，不通过直接 `answer` 回「已拒绝」，工具一次都不调；
- 整个流程可审计：每一步都是命名节点，日志里能直接看到 `classify` / `approval` / `execute`。

### 6.3 approval 节点的 `interrupt()` 细节

```python
async def approval_node(state: TicketState) -> dict[str, Any]:
    decision = interrupt({                       # ① 暂停，把上下文交给外部
        "type": "approval",
        "request": state["request"],
        "action": state["tool_name"],
        "arguments": state["tool_args"],
    })
    return {"approval": decision}                # ② 恢复时把 resume 值写进状态
```

四个细节，面试常问：

1. **审批前不落盘**：`execute_node` 在 `approval` 之后，`interrupt()` 挂起时工具没被调用，
   所以「拒绝」是真的没执行，不是「执行完再告诉你已拒绝」。
2. **恢复靠 thread_id**：`TicketWorkflow.resume()` 用同一个 `thread_id` + `Command(resume=...)`，
   所以 checkpointer 必须是持久化的。用 `InMemorySaver` 的审批是假 HITL——重启即失效。
   `build_ticket_workflow()` 的 docstring 明确写了这一点。
3. **节点会重放**：`approval_node` 在恢复时从头执行一遍，`interrupt()` 这次直接返回 resume 值。
   所以中断之前的代码必须无副作用（这里只读 state，安全）。
4. **恢复后仍要鉴权**：`resume()` 重新进入 `principal_context(principal)`，
   `execute_node` 里的 `current_principal` 是**这次审批人**的身份，不是发起人的。
   真实系统里这里还要校验「审批人是否有权批准这个操作」，否则就是越权漏洞。

## 自测题

**1. `create_agent` 返回的对象类型是什么？这意味着什么？**
`CompiledStateGraph`（LangGraph 编译后的图）。意味着它天然支持 checkpointer、`interrupt`、
`stream_mode`、state history；也意味着你随时可以 `get_state()` 看内部状态。

**2. `create_react_agent` 还能用吗？**
能 import，但它是兼容层，v1 迁移文档要求新代码用 `langchain.agents.create_agent`。
两者不是「新旧 API 并存」，而是「旧的是为了不破坏存量」。

**3. State 里 `messages` 为什么必须配 `add_messages` reducer？**
节点返回 `{"messages": [ai]}` 时，无 reducer 会**覆盖**整个列表，历史全丢；
`add_messages` 做追加 + 按 id upsert，才有多轮对话。

**4. `interrupt()` 之后的代码在恢复时会怎样？**
整个节点从头重新执行，`interrupt()` 这次直接返回 resume 值。
所以中断前的副作用会执行两次，必须保证幂等。

**5. 用 `MemorySaver` 做审批门，问题出在哪？**
审批可能隔几小时，进程重启后 checkpoint 丢失，`Command(resume=...)` 找不到 thread，
工单永远卡住。这是「假 HITL」，生产必须用 sqlite/postgres。

**6. 本项目 `SqliteCheckpointSaver` 为什么继承 `InMemorySaver` 而不是直接实现协议？**
为了复用官方对增量 channel、版本、pending writes 的复杂语义；自己写容易在「恢复后状态少一条」
这类隐蔽场景出错。它只在 `put`/`put_writes`/`delete_thread` 后追加落盘。

**7. `stream_mode="messages"` 为什么还要过滤消息类型？**
因为流里既有 AI 文本分块，也有工具调用分块（content 为空）和工具结果消息。
不过滤会把工具参数 JSON 当回答吐给用户。本项目只取 `AIMessage`/`AIMessageChunk`。

**8. 什么时候该放弃 `create_agent` 改用 `StateGraph`？**
当「下一步走哪」由业务规则而非模型决定时：合规审批、固定多步流程、必须精确审计每一步。
判据是控制权归属，不是代码量。

## 面试追问

**Q1：LangChain v1 为什么把 middleware 抬成一等机制？**
要点：v0 的扩展靠继承 `AgentExecutor` 或 fork，升级必冲突，且每种 agent 一套循环，
HITL/重试/摘要各写一遍。middleware 把横切逻辑收敛到六个钩子
（`before_agent`/`before_model`/`after_model`/`after_agent`/`wrap_model_call`/`wrap_tool_call`），
一个 Agent 可叠加多个，互不影响；官方还提供了重试、降级、摘要、PII、HITL 等现成实现。

**Q2：checkpoint 和「把聊天记录存数据库」有什么本质区别？**
要点：聊天记录只是 `messages` 一个字段的值；checkpoint 是**整个图状态 + channel 版本 +
pending writes + parent 链**。前者只能「接着聊」，后者能「从第 3 步重跑」「回到审批前」「并行分支
各自恢复」。另外 checkpoint 是按 super-step 写的，天然支持中断恢复，聊天记录不行。

**Q3：`interrupt()` 是抛异常实现的，这带来什么工程约束？**
要点：① 中断点之前的代码会重放，必须无副作用或幂等；② 不要在 `try/finally` 里做清理，
因为异常会触发 finally；③ 一次运行可能有多个 interrupt（并行分支），恢复要按 id 对应；
④ 恢复必须带同一个 `thread_id`，否则找不到断点。

**Q4：多副本部署时，LangGraph 的 checkpoint 要注意什么？**
要点：必须用共享存储（`AsyncPostgresSaver`），`thread_id` 要能路由到任意副本；
连接池按副本配置，`setup()` 幂等；`InMemorySaver` 和 SQLite 在多副本下都不成立
（SQLite 在网络文件系统上不可靠）。另外要考虑 checkpoint 表的增长与归档策略。

**Q5：你怎么向面试官解释「自主 Agent」和「工作流」的取舍？**
要点：不是二选一，而是按「控制权」分层。开放式任务（问答、检索、多工具探索）用
`create_agent`，让模型决定路径；合规场景（写操作、资金、权限变更）用 `StateGraph`，
把审批门做成真实执行屏障。本项目两者共存：`core/agent.py` 负责自主，
`core/workflow.py` 负责确定性，共享同一套网关治理与同一套 checkpointer 抽象。

**Q6：如果让你给这张图加「超时自动拒绝」，怎么改？**
要点：不要在图里 `sleep`。做法是 checkpoint + 外部调度：工单停在 `interrupt()` 后，
把 `thread_id` 与截止时间写入任务表，定时器到期用 `Command(resume=...)` 恢复，
resume 值形如 `{"approve": False, "note": "超时自动拒绝"}`；
恢复接口要幂等（已被人工处理就跳过）。这样超时逻辑与图解耦，重启也不丢。
