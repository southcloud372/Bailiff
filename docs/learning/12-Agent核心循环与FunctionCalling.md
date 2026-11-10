# Agent 核心循环与 Function Calling

这篇解决一个核心问题：模型只会「生成文本」，为什么能读文件、查数据库、发工单？读完你能
手写一个不依赖框架的 Agent 循环，能解释 ReAct、工具 schema、并行调用和终止条件，能看懂
本项目 `core/agent.py` 如何把模型、工具和网关串起来，并能回答「为什么不能直接把函数塞给
模型」。

前置：读完第 10 篇（Python 基础）和第 11 篇（LLM 原理）。代码需要 `DEEPSEEK_API_KEY`。

---

## 1. 一次 LLM 调用能返回什么

**概念**

Chat Completions 的返回有两种形态：普通文本，或者 `tool_calls`。后者是一个数组，每项包含
工具名、参数 JSON 字符串和一个 `id`。模型**不会执行**工具，它只是输出「我想调用谁、参数是
什么」。执行是运行时的事。

**为什么需要**

把「决策」和「执行」分开，是 Agent 可控的前提。模型负责在不确定的自然语言里选动作，代码
负责校验参数、检查权限、真正执行、把结果写回消息。只要执行权在代码手里，就有地方加限流、
审计和人工审批。

**最小代码**

```python
resp = client.chat.completions.create(   # 需要 DEEPSEEK_API_KEY
    model="deepseek-flash",
    messages=[{"role": "user", "content": "读一下 note.txt"}],
    tools=[{
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    }],
)
msg = resp.choices[0].message
print(msg.content)        # 通常为 None
print(msg.tool_calls)     # [ChatCompletionMessageToolCall(...)]
```

**本项目在哪**

- `agent_platform/core/agent.py:109` 的 `build_model` 构造 `ChatOpenAI`，指向 DeepSeek 的
  OpenAI 兼容端点。
- `agent_platform/core/agent.py:128` 的 `build_tools` 把网关工具包装成 LangChain
  `StructuredTool`，`create_agent` 再把这些工具 schema 绑定给模型。
- 模型返回的 `tool_calls` 由 LangGraph 的工具节点消费，不是由模型自己执行。

---

## 2. ReAct 循环：Thought → Action → Observation

**概念**

ReAct 是「推理 + 行动」的交错循环：

1. 模型根据当前消息决定下一步（Thought，可能不可见）；
2. 输出一个工具调用（Action）；
3. 运行时执行工具，把结果作为 `tool` 消息写回（Observation）；
4. 模型看到新观察，继续下一轮，直到不再调用工具。

**为什么需要**

一次调用只能产生一个「快照」。很多任务需要多步：先列目录，再读文件，再根据内容决定是否
写入。循环让模型能在每一步根据真实结果调整，而不是一次性猜完所有参数。

**最小伪代码**

```text
messages = [user_message]
for step in range(max_steps):
    reply = llm(messages, tools)
    if not reply.tool_calls:
        return reply.content
    messages.append(reply)
    for call in reply.tool_calls:
        result = execute(call.name, call.args)
        messages.append(tool_message(call.id, result))
raise MaxStepsExceeded
```

**本项目在哪**

- `agent_platform/core/agent.py:311` 的
  `create_agent(model, tools, checkpointer=saver)` 编译出来的就是这张「模型 ⇄ 工具」图。
- 循环的每一轮都会把完整消息历史交给模型；checkpointer 按 `thread_id` 保存状态，所以
  第二轮能看到第一轮的工具结果。
- `tests/test_agent.py` 的 `_ScriptedModel` 用两条消息模拟「先调用 write_file，再给最终
  答复」，验证的正是这个循环。

---

## 3. 工具 schema：description 就是 prompt

**概念**

一个工具由三部分组成：名字、描述、参数 JSON Schema。模型根据名字和描述判断「什么时候用」，
根据 schema 生成参数。参数校验发生在运行时，不在模型里。

**为什么需要**

工具描述是模型唯一的决策依据。描述写得含糊，模型就会选错工具或漏填参数；参数 schema 写得
太松，模型就会传 `{"path": null}`。工具设计本质上是「给模型写 API 文档」，要像写 prompt
一样认真。

**最小代码**

```python
from pydantic import BaseModel, Field

class WriteFileArgs(BaseModel):
    path: str = Field(description="根目录内的相对路径，例如 note.txt")
    content: str = Field(description="要写入的文本内容")

schema = WriteFileArgs.model_json_schema()
print(schema["required"])          # ['path', 'content']
print(schema["properties"]["path"]["description"])
```

**本项目在哪**

- `agent_platform/core/agent.py:72` 起定义了 `ReadFileArgs`、`WriteFileArgs`、`ListDirArgs`、
  `SearchKnowledgeArgs`、`RememberArgs`、`RecallArgs`，每个字段都有 `description`。
- `agent_platform/core/agent.py:142` 用
  `StructuredTool.from_function(name=..., description=..., args_schema=schema, coroutine=_run)`
  把「名字 + 描述 + Pydantic 模型」注册成模型可调用的工具。
- `agent_platform/gateway/mcp_client.py` 通过 MCP 的 `tools/list` 动态发现远端工具及其
  `input_schema`，再转成网关统一签名；工具不是硬编码的。

---

## 4. 手写一个最小 Agent 循环

下面这段不依赖 LangChain / LangGraph，只用 OpenAI SDK；完整示例约 40 行，循环本体约 15 行。
它演示了消息如何追加、工具结果如何回填、步数上限如何生效。

```python
import json
from openai import OpenAI

client = OpenAI(api_key="sk-...", base_url="https://api.deepseek.com")  # 需要 KEY
FILES = {"note.txt": "hello"}

def read_file(path: str) -> str:
    return FILES.get(path, "NOT_FOUND")

def write_file(path: str, content: str) -> str:
    FILES[path] = content
    return f"wrote {len(content)} chars to {path}"

TOOLS = [
    {"type": "function", "function": {
        "name": "read_file", "description": "读取文件内容",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "写入文件内容",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
]
DISPATCH = {"read_file": read_file, "write_file": write_file}

def run(question: str, max_steps: int = 6) -> str:
    messages = [{"role": "user", "content": question}]
    for _ in range(max_steps):
        resp = client.chat.completions.create(
            model="deepseek-flash", messages=messages, tools=TOOLS)
        msg = resp.choices[0].message
        messages.append(msg.model_dump(exclude_none=True))
        if not msg.tool_calls:
            return msg.content or ""
        for call in msg.tool_calls:
            args = json.loads(call.function.arguments)
            result = DISPATCH[call.function.name](**args)
            messages.append({
                "role": "tool", "tool_call_id": call.id, "content": str(result),
            })
    raise RuntimeError("超过最大步数")

print(run("把 note.txt 的内容改成 bye，再读出来确认"))
```

关键点：

- assistant 消息必须原样追加，包含 `tool_calls` 和每个 `id`；否则下一轮模型不知道结果对应
  哪个调用。
- `tool` 消息的 `tool_call_id` 必须和调用一一对应，顺序不重要，id 必须对。
- 参数是 JSON 字符串，要 `json.loads`；生产代码还要用 Pydantic 校验，不能直接 `**args`。
- `max_steps` 是硬上限，防止模型无限调用。

**本项目在哪**

- 这段手写循环等价于 `create_agent` 编译出的图，但本项目多了 checkpoint、流式输出、工具
  治理、预算和护栏。
- `agent_platform/core/agent.py:276` 的 `stream()` 用 `astream(..., stream_mode="messages")`
  拿到 token 级分块，这是手写循环没有的能力。

---

## 5. 并行工具调用

**概念**

模型可以在一条 assistant 消息里返回多个 `tool_calls`。它们之间通常没有依赖，可以并发执行；
执行完把每个结果按 `tool_call_id` 写回，模型下一轮就能看到全部观察。

**为什么需要**

串行执行三个 200ms 的工具要 600ms，并发只要 200ms 多一点。对延迟敏感的问答场景，这是直接
的用户体验差异。但并行不是无条件的：写同一个文件、扣同一份库存这类有副作用的操作，并发会
引入竞态，必须串行或加幂等键。

**最小代码**

```python
import asyncio

async def execute_all(calls, handlers):
    async def one(call):
        return await handlers[call.function.name](**json.loads(call.function.arguments))

    results = await asyncio.gather(*(one(call) for call in calls))
    for call, result in zip(calls, results):
        messages.append({
            "role": "tool", "tool_call_id": call.id, "content": str(result),
        })
```

**本项目在哪**

- `create_agent` 的工具节点会消费 assistant 消息里的多个工具调用；每个调用最终都走
  `gateway.call(principal, name, args)`，因此限流和审计是**按调用**计的，不是按轮次。
- `agent_platform/gateway/ratelimit.py` 的令牌桶按 `principal.id` 计；并行调用会同时消耗
  多个令牌，这是有意的：防的是单个用户打爆下游，而不是鼓励无限并发。
- 如果工具有副作用，应该在工具实现里做幂等（例如按 `tool_call_id` 去重），不能假设框架
  一定串行。

---

## 6. 终止条件与死循环

**概念**

Agent 循环必须有确定的停止条件：模型不再调用工具、达到步数上限、超出预算、检测到重复调用、
需要人工审批。没有这些条件，模型可能一直调用同一个工具，或者在一个失败的工具上反复重试。

**为什么需要**

模型不知道「已经试过了」。如果工具返回「文件不存在」，它可能换个路径再试，再失败，再试。
每一步都花钱、都增加延迟，最后把上下文塞满。终止条件是成本控制和可用性的底线。

**最小代码**

```python
def run_loop(messages, tools, max_steps: int = 6) -> str:
    seen: set[tuple[str, str]] = set()
    for _ in range(max_steps):
        reply = llm(messages, tools)
        if not reply.tool_calls:
            return reply.content
        for call in reply.tool_calls:
            signature = (call.function.name, call.function.arguments)
            if signature in seen:
                return "检测到重复调用，停止并请求人工确认"
            seen.add(signature)
            result = execute(call)
            if result.startswith("ERROR:"):
                messages.append(tool_message(call.id, result + "；不要重试同样参数"))
            else:
                messages.append(tool_message(call.id, result))
    return "达到最大步数，转人工"
```

**本项目在哪**

- `agent_platform/router/budget.py` 的 `BudgetPolicy.check` 在**请求进入图执行前**检查累计
  费用和 token，超限抛 `BudgetExceeded`。注意当前实现是每请求一次，不是每次模型调用一次；
  多步 ReAct 中途仍可能超预算，要逐次拦截应把检查挂到模型回调或 middleware。
- LangGraph 对图的超级步数有 `recursion_limit`（常见默认 25，具体以版本文档为准）；上线时
  应该显式设置，并把「步数上限」写进产品行为，而不是等框架报错。
- `core/workflow.py` 的敏感操作走 `interrupt()`，人工审批是另一种终止条件：图停在那里，
  等人决定继续还是拒绝。

---

## 7. 框架替你做了什么

**概念**

`create_agent` 不是一个黑盒 API，它编译出一张状态图：模型节点、工具节点、条件边、消息状态
和 checkpoint。你手写循环里的「追加消息、执行工具、回填结果、判断是否继续」，它都做了。

**为什么需要**

手写循环适合理解原理，不适合生产：流式输出、并行调用、错误处理、状态持久化、中断恢复、
可观测埋点，每一项都要自己写，而且容易写错。框架的价值是把这些共性能力收敛成一张可编译的
图，让你把精力放在工具和治理上。

**最小代码**

```python
from langchain.agents import create_agent

graph = create_agent(model, tools, checkpointer=saver)

async def main() -> None:
    result = await graph.ainvoke(
        {"messages": [{"role": "user", "content": "读一下 note.txt"}]},
        config={"configurable": {"thread_id": "t1"}},
    )
    print(result["messages"][-1].content)
```

**本项目在哪**

- `agent_platform/core/agent.py:311` 调用 `create_agent(model, tools, checkpointer=saver)`；
  `saver` 由 `build_checkpointer` 按 `memory` / `sqlite` / `postgres` 选择。
- 框架负责：消息状态合并、工具 schema 绑定、工具节点执行、循环边、checkpoint、流式事件。
- 框架**不负责**：这个用户能不能调用这个工具、调用要不要审计、路径是否越界、成本是否超限。
  这些在本项目的 Gateway、guardrails、budget 里，见下一节。

---

## 8. 本项目的完整链路

把上面的零件串起来，一次工具调用经过这些层：

```
Agent.chat(message, principal, thread_id)
  → 输入护栏 → 预算检查 → LangGraph.ainvoke
    → 模型节点：DeepSeek 返回 tool_calls
      → 工具节点：StructuredTool(coroutine=_run)
        → current_principal.get()（contextvar）
        → gateway.call(principal, name, args)
          → 限流 → 权限 → 存在性 → 执行 → 审计
            → registry 里的工具（文件系统 / 知识库 / 记忆 / MCP）
        → 结果回填为 tool 消息
    → 模型节点：生成最终答复
  → 输出护栏脱敏 → 返回
```

对应代码：

- `agent_platform/core/agent.py:128` `build_tools`：文件系统三件套硬编码暴露；知识/记忆
  工具是否出现，取决于 `gateway.tool_names()`。
- `agent_platform/core/agent.py:133`：工具执行时从 `current_principal` 取身份，取不到直接
  报错，防止绕过 Agent 直接调用。
- `agent_platform/core/agent.py:138`：`await gateway.call(principal, name, dict(kwargs))`，
  统一走治理。
- `agent_platform/gateway/gateway.py:35`：限流 → 权限 → 存在性 → 执行 → 审计，每一步都
  记录 `outcome`。
- `agent_platform/gateway/mcp_client.py`：外部 MCP server 通过 `tools/list` 发现工具，
  注册进同一个 `ToolRegistry`，加 server 前缀避免重名；单个 server 挂掉不影响其他。

一个容易被忽略的细节：`build_tools` 里的 `_run` 是 `async def`，但文件系统工具本身是同步
函数。网关的 `await fn(principal, arguments)` 之所以成立，是因为 `wiring.py` 把它们包装成了
`async def read/write/ls`。如果直接注册同步函数，`await` 会报 `TypeError`。

---

## 9. 为什么工具调用必须过网关，而不是直接给 Agent 一个函数

**概念**

「直接给函数」是指 `tools=[read_file, write_file]`，模型生成参数后框架直接调用。它简单，
但把「模型输出」当成了可信输入。「过网关」是指模型只能提出 `(tool_name, args)`，由网关用
当前身份做授权、限流、审计、沙箱校验后再执行。

**为什么需要**

五个原因：

1. **模型输出不可信**。Prompt 注入可以让模型调用攻击者想要的工具；参数也是模型生成的，
   可能包含 `../../etc/passwd`。
2. **需要按用户授权**。同一个 Agent 服务多个用户，A 不能读 B 的文件。函数本身不知道
   「当前是谁」，网关通过 `Principal` 做默认拒绝的白名单。
3. **需要可审计**。谁、何时、调了什么、参数是什么、结果如何，必须留痕。直接调用没有统一
   的审计点，只能在每个函数里重复写。
4. **需要限流和成本控制**。模型可能并行调用十个工具，或者陷入循环。网关按用户限流，预算
   按累计 token/费用拦截。
5. **需要隔离第三方工具**。MCP server 是外部进程，可能被攻破或本身有漏洞；网关做命名空间、
   白名单、超时和错误隔离，单个 server 挂掉不拖垮整个平台。

**最小代码**

```python
# 反例：函数直接暴露给模型，没有身份、没有审计、没有沙箱
tools = [read_file, write_file]

# 正例：模型只能提出调用，网关决定能不能执行
async def _run(**kwargs):
    principal = current_principal.get()
    if principal is None:
        raise RuntimeError("缺少调用方身份")
    return await gateway.call(principal, "write_file", kwargs)

tool = StructuredTool.from_function(
    name="write_file", description="写入文件", args_schema=WriteFileArgs, coroutine=_run,
)
```

**本项目在哪**

- `agent_platform/gateway/gateway.py:40` 限流在最前，防的是单个用户打爆下游；`:47` 权限
  在存在性之前，避免向未授权调用方泄露「工具是否存在」；`:58` 执行时把 `principal` 传给
  工具，供按用户分域的记忆工具使用。
- `agent_platform/gateway/policy.py` 默认拒绝：没有命中白名单就拒绝，模型说什么都不算。
- `agent_platform/tools/filesystem.py` 的所有路径都经 `gateway/security.py` 的
  `resolve_within_root` 校验，拒绝绝对路径、`..` 和符号链接逃逸；这是「即使模型被注入，
  也只能在沙箱内操作」的兜底。
- `agent_platform/core/workflow.py` 的敏感操作先 `interrupt()` 等人工审批，审批通过才
  调用 `gateway.call`；审批是执行前的真实屏障，不是提示词。
- 结论：给 Agent 一个函数，等于把函数的全部权限交给模型；给 Agent 一个网关，模型只有
  「申请调用」的权利。

---

## 自测题

1. **为什么需要 Agent 循环，而不是一次 LLM 调用？**
   答：一次调用只能给出一个决策；多步任务需要根据工具的真实结果调整下一步。循环把
   Action 和 Observation 交替起来，模型才能在每一步基于事实继续。

2. **工具 schema 的三个核心字段是什么？description 为什么重要？**
   答：名字、描述、参数 JSON Schema。描述是模型选择工具的依据，写得含糊会导致选错工具或
   漏参数；schema 约束参数形状，运行时还要用 Pydantic 校验。

3. **assistant 消息和 tool 消息如何对应？**
   答：assistant 消息里的每个 `tool_call` 有唯一 `id`；执行结果放在 `role="tool"` 的消息
   里，`tool_call_id` 必须等于对应的 id。顺序不重要，id 不能错。

4. **并行工具调用适合什么场景？什么场景不能并行？**
   答：适合相互独立、只读或幂等的调用，能降低延迟；写同一资源、扣库存、有顺序依赖的操作
   不能并行，需要串行、加锁或幂等键。

5. **Agent 死循环的常见原因和止血手段？**
   答：工具反复失败但模型一直重试、参数重复、模型不收敛。止血手段：最大步数、重复调用
   签名检测、预算硬上限、把错误信息明确回填并提示不要重试、敏感操作转人工。

6. **`create_agent` 替你做了什么，没做什么？**
   答：做了消息状态、工具绑定、工具节点、循环边、checkpoint、流式事件；没做身份授权、
   审计、沙箱、预算和护栏，这些要在网关和编排层补。

7. **为什么不能把 `write_file` 直接放进 `tools` 列表？**
   答：模型的参数不可信，可能路径穿越或越权；函数本身不知道当前用户是谁，也没有统一审计、
   限流和审批。必须通过网关做权限、沙箱、审计和限流。

---

## 面试追问

1. **「讲一下你的 Agent 循环是怎么跑起来的。」**
   要点：模型节点产出 `tool_calls` → 工具节点执行 → 结果以 `tool` 消息回填 → 模型再决策，
   直到没有工具调用或触发终止条件；checkpoint 按 `thread_id` 保存状态；工具统一走网关，
   身份用 contextvar 传。

2. **「工具描述怎么写才好？」**
   要点：写「什么时候用、什么时候不用、参数含义、返回什么」，而不是写实现细节；参数用
   Pydantic 加 `Field(description=...)`；描述要能区分相邻工具（read_file vs list_dir）；
   描述是 prompt 的一部分，改动要跑评测。

3. **「Agent 陷入死循环怎么办？」**
   要点：先复现并看 trace，确认是工具错误、描述歧义还是模型不收敛；加最大步数、重复签名
   检测、预算上限；把工具错误明确写回并禁止同参数重试；必要时转人工。不要只靠调 prompt。

4. **「为什么工具调用要过网关？多一层不慢吗？」**
   要点：慢的是模型和外部 I/O，网关是内存里的几次判断；它提供授权、限流、审计、沙箱和
   故障隔离，是安全与可运维的必需层。没有网关，任何 prompt 注入都直接变成一次真实操作。

5. **「并行工具调用会带来什么问题？」**
   要点：结果要按 `tool_call_id` 正确回填；有副作用的工具会竞态；限流和审计要按调用计；
   失败要部分成功可恢复。解决方式是只并行无依赖的读操作，写操作串行或幂等，并把每个调用
   的 outcome 单独记录。
