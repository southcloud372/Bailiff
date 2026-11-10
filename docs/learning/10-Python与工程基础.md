# Python 与工程基础

这篇解决一个具体问题：你「粗看过 Python」，但打开 `agent_platform/` 会被 `async`、`Protocol`、
`ContextVar`、Pydantic 和 uv 一起劝退。读完你能读懂本项目 80% 的源码，能独立跑测试、加一个
小功能，并且知道每个语法点在生产代码里为什么出现，而不是只会写脚本。

阅读方式：每节先给概念和动机，再给可运行的最小代码，最后指到本项目真实文件。按「这个机制
解决了什么问题」记，不要背语法。

---

## 1. 类型注解：让错误在运行前暴露

**概念**

类型注解写在变量、参数、返回值后面，Python 运行时默认不检查，但 `mypy`、IDE、Pydantic
会在运行前或边界处检查。Python 3.13 原生支持 `X | None`、`list[str]`、`dict[str, int]`。

**为什么需要**

Agent 系统的参数形状很复杂：工具参数是嵌套 JSON，模型返回的 `tool_calls` 可能缺字段，
配置来自环境变量。没有类型，错误会推迟到线上某个请求才炸。本项目 `pyproject.toml:98`
开了 `mypy strict = true`，注解不是装饰，是 CI 门槛。

**最小代码**

```python
from collections.abc import Awaitable, Callable
from typing import Any

# 工具的统一签名：名字 + 参数 -> 异步结果
ToolFn = Callable[[str, dict[str, Any]], Awaitable[Any]]

async def echo(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{args.get('value', '')}"

def register(table: dict[str, ToolFn], name: str, fn: ToolFn) -> None:
    table[name] = fn
```

把 `fn` 换成 `Callable[[str], str]`，`mypy` 会直接报参数数量不匹配。注解的价值就在这里：
它在你还没写测试之前，先告诉你「这个函数不能被这样调用」。

**本项目在哪**

- `agent_platform/gateway/registry.py:11` 定义了 `ToolFn`，全项目所有工具都遵守这个签名。
- `agent_platform/gateway/policy.py` 的 `Principal` 用 `frozenset[str]` 表示角色集合。
- `pyproject.toml:100` 的 `strict = true` 意味着不能有隐式 `Any`，函数必须写返回类型。

---

## 2. async/await 与事件循环：等待 I/O 时别占着线程

**概念**

`async def` 定义协程，`await` 把控制权交回事件循环，等 I/O 完成再继续。事件循环是单线程的：
一个协程在 `await` 处让出，其他协程才能跑。`asyncio.gather` 并发启动多个协程并按顺序收集结果。

**为什么需要**

Agent 的一次请求会等模型 API、等数据库、等 MCP 子进程。这些等待占 99% 的时间。如果用同步
阻塞调用，一个请求就会卡住整个事件循环，FastAPI 的其他请求全部排队。异步不是「更快」，是
「等待时不浪费线程」。

**最小代码**

```python
import asyncio
import time

async def fetch(name: str, delay: float) -> str:
    await asyncio.sleep(delay)   # 模拟网络 I/O：这里让出事件循环
    return name

async def main() -> None:
    started = time.perf_counter()
    results = await asyncio.gather(fetch("a", 0.2), fetch("b", 0.2))
    print(results, round(time.perf_counter() - started, 2))

asyncio.run(main())
```

输出约为 `['a', 'b'] 0.2`。如果写成两次 `await fetch(...)`，耗时约 0.4 秒。两个常见错误：

1. 在 `async def` 里调用同步阻塞函数（如 `time.sleep`、同步数据库驱动），事件循环仍然被卡住。
   要么换异步驱动，要么用 `await asyncio.to_thread(...)`。
2. 创建了协程却没有 `await`，它不会执行，只会得到一个 `RuntimeWarning`。

**本项目在哪**

- `agent_platform/gateway/gateway.py:35` 的 `async def call` 是整条工具链的入口。
- `agent_platform/observability/health.py:108` 用 `asyncio.gather` 并发跑所有就绪探针，
  一个慢依赖不会串行拖长 `/readyz`。
- `agent_platform/api/streaming.py` 用 `asyncio.Queue` + `asyncio.wait_for` 在模型输出之间
  插入 SSE 心跳，并在结束时 `task.cancel()`，避免生成器关闭后后台任务泄漏。
- `agent_platform/core/agent.py:276` 的 `async for chunk in self._graph.astream(...)` 是
  异步生成器：模型每产出一段文本就 `yield` 给上层。

---

## 3. contextvars：不通过函数参数传身份

**概念**

`ContextVar` 是「当前执行上下文」里的变量。每个 asyncio task 拿到一份上下文副本，task 内
`set` 的值不会串到别的 task。和全局变量相比，它按请求隔离；和函数参数相比，它不需要穿过
每一层调用。

**为什么需要**

工具函数由 LangGraph 调用，函数签名是框架定的，你没法在中间加一个 `principal` 参数。
但工具执行时又必须知道「当前是谁在调用」，否则权限检查无从谈起。`ContextVar` 让身份沿着
异步调用链隐式传递，且不会在并发请求之间串号。

**最小代码**

```python
import asyncio
from contextvars import ContextVar

current_user: ContextVar[str] = ContextVar("current_user", default="anonymous")

async def handle(user: str) -> str:
    current_user.set(user)
    await asyncio.sleep(0.01)   # 模拟工具执行
    return current_user.get()

async def main() -> None:
    print(await asyncio.gather(handle("alice"), handle("bob")))

asyncio.run(main())
```

输出 `['alice', 'bob']`。如果 `current_user` 是普通全局变量，两个协程会互相覆盖。

注意 `set()` 返回一个 `token`，规范写法是在 `finally` 里 `reset(token)`，把上下文恢复到进入
前的状态。本项目用 `@contextmanager` 封装了这个配对操作。

**本项目在哪**

- `agent_platform/context.py:21` 定义 `current_principal`，`:43` 的 `principal_context`
  负责 `set` / `reset`。
- `agent_platform/core/agent.py:133` 在工具包装函数里 `current_principal.get()`，拿不到就
  直接报错，防止「绕过 Agent 直接调用工具」。
- `agent_platform/core/agent.py:220` 在 `chat()` 里用 `with principal_context(principal):`
  包住整次图执行，所以工具在图的任意节点执行都能读到身份。
- `agent_platform/context.py` 刻意不 import LangChain / FastAPI，成为最底层的共享状态，
  避免循环依赖。这是分层设计的一个细节。

---

## 4. dataclass 与 Pydantic：内部结构 vs 边界校验

**概念**

`@dataclass` 是标准库的「数据容器」，只生成 `__init__`、`__repr__`、`__eq__`，不校验类型。
Pydantic 的 `BaseModel` 在构造时做类型转换和校验，失败抛 `ValidationError`。

**为什么需要**

不是所有数据都值得付校验成本。进程内部传一个不可变的配置对象，用 `dataclass(frozen=True)`
足够；而来自 HTTP、环境变量、模型输出的数据是不可信边界，必须用 Pydantic 校验。把两者混用，
要么内部代码被无谓的校验拖慢，要么边界数据带着脏值流进核心逻辑。

**最小代码**

```python
from dataclasses import dataclass

from pydantic import BaseModel, Field, ValidationError

@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    backoff_seconds: float = 0.5

class Principal(BaseModel):
    id: str
    roles: frozenset[str] = Field(default_factory=frozenset)

p = Principal(id="u1", roles={"agent", "user"})
print(p.roles)                       # frozenset({'agent', 'user'})

try:
    Principal(id=1)                  # 类型错误
except ValidationError as exc:
    print(exc.error_count())         # 1
```

`frozen=True` 防止配置被中途改写；`slots=True` 减少内存占用并阻止动态加属性。
Pydantic v2 用 `model_dump()`，不是 v1 的 `dict()`。

**本项目在哪**

- `agent_platform/gateway/mcp_client.py:49` 的 `MCPServerSpec` 是 `@dataclass(frozen=True)`，
  它是启动时读入的静态配置，不需要每次校验。
- `agent_platform/gateway/policy.py` 的 `Principal`、`audit.py` 的 `AuditEvent` 是 Pydantic
  模型：一个来自 JWT，一个要落审计，都属于边界数据。
- `agent_platform/gateway/config.py:82` 的 `KnowledgeSettings` 用
  `@model_validator(mode="after")` 在字段都解析完后推导默认端点，这是 Pydantic 做「配置
  归一化」的典型用法。
- 工具参数模型 `ReadFileArgs`、`WriteFileArgs` 在 `core/agent.py:72` 起，Pydantic 生成的
  JSON Schema 会直接成为模型看到的工具参数定义。

---

## 5. Protocol 与鸭子类型：依赖行为，不依赖继承

**概念**

`typing.Protocol` 定义「只要你有这些方法，我就认你」。实现类不需要显式继承协议，这叫结构化
子类型（static duck typing）。`mypy` 会按方法签名检查，运行时通常不做 `isinstance`。

**为什么需要**

Agent 平台要能在测试里替换掉真实依赖：真实限流器换成一个永远放行的假对象，真实语义缓存
换成一个空实现。如果上层依赖具体类，替换就得改继承树；依赖 Protocol，任何满足方法的对象
都能注入。

**最小代码**

```python
from typing import Protocol

class Limiter(Protocol):
    async def aallow(self, key: str) -> bool: ...

class AlwaysAllow:
    async def aallow(self, key: str) -> bool:
        return True

class DenyAll:
    async def aallow(self, key: str) -> bool:
        return False

async def call_tool(limiter: Limiter, user: str) -> str:
    if not await limiter.aallow(user):
        raise RuntimeError("rate limited")
    return "ok"
```

`AlwaysAllow` 和 `DenyAll` 都没有继承 `Limiter`，但都能传进 `call_tool`。测试里不需要
启动 Redis，也不需要 monkeypatch 全局对象。

**本项目在哪**

- `agent_platform/gateway/ratelimit.py:44` 定义 `Limiter`，`RateLimiter`（进程内令牌桶）和
  `RedisRateLimiter`（跨副本滑动窗口）都满足它，网关只依赖 `aallow`。
- `agent_platform/core/agent.py:49` 定义 `ResponseCache` 协议，`SemanticCache` 和
  `NullCache` 都能注入。
- `agent_platform/api/app.py` 定义 `ChatAgent` 协议，API 层不 import LangChain 具体类型，
  测试可以传一个假 Agent。
- 注意：Protocol 只在类型检查时生效。如果你需要运行时 `isinstance`，要加
  `@runtime_checkable`，但本项目没有这么做，因为不需要。

---

## 6. 异常与上下文管理器：保证资源一定被释放

**概念**

自定义异常用于区分「预期内的业务失败」和「真正的崩溃」。上下文管理器（`with`）保证进入和
退出成对执行，即使中间抛异常。实现方式有两种：类里写 `__enter__` / `__exit__`，或者用
`@contextmanager` 装饰一个生成器。

**为什么需要**

数据库连接、文件句柄、MCP 子进程、`ContextVar` 的 token，都必须在结束时清理。手写
`try/finally` 容易漏；`with` 把清理逻辑绑定在语法结构上，漏不掉。异常类型则决定了上层
能不能做针对性处理：限流失败应该返回 429，权限失败应该返回 403，而不是统一 500。

**最小代码**

```python
from contextlib import contextmanager
from contextvars import ContextVar

current: ContextVar[str | None] = ContextVar("current", default=None)

@contextmanager
def using(value: str):
    token = current.set(value)
    try:
        yield value
    finally:
        current.reset(token)       # 即使 with 块抛异常也会执行

with using("u1"):
    print(current.get())           # u1
print(current.get())               # None
```

类式写法：

```python
class Resource:
    def __enter__(self) -> "Resource":
        print("open")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        print("close")
        # 返回 None/False：异常继续向上抛；返回 True 才会吞掉异常
```

`__exit__` 的返回值很关键。返回 `True` 表示「异常已处理」，会静默吞掉错误。本项目
`SqliteCheckpointSaver.__exit__` 返回 `None`，所以异常照常传播。

**本项目在哪**

- `agent_platform/core/checkpoint.py:104` 的 `__enter__` / `:107` 的 `__exit__` 让
  `SqliteCheckpointSaver` 支持 `with`，退出时关闭 SQLite 连接。
- `agent_platform/context.py:43` 用 `@contextmanager` 封装 `principal_context`，
  `finally` 里 `reset(token)`，防止身份泄漏到下一个请求。
- `agent_platform/gateway/mcp_client.py` 用 `AsyncExitStack` 管理多个 MCP 连接：
  任一连接启动失败时，已启动的连接会被统一关闭，不会留下孤儿进程。
- 异常层次在 `agent_platform/gateway/errors.py`：`RateLimitExceeded`、`PermissionDenied`、
  `UnknownTool` 分别对应不同的 HTTP 状态码，API 层按类型映射。

---

## 7. 包管理与 uv：锁文件是复现的前提

**概念**

`pyproject.toml` 声明依赖范围和工具配置，`uv.lock` 锁定每个包的精确版本与哈希。uv 负责
解析、下载、创建虚拟环境、运行命令。`uv sync` 让环境与锁文件一致，`uv run` 在项目环境里
执行命令，不需要手动 `activate`。

**为什么需要**

Agent 生态发版极快，LangChain 从 0.x 到 1.x 有破坏性变更。只写 `>=` 会在某天悄悄升到大版本；
只写 `==` 又拿不到安全补丁。本项目的策略是「下限贴稳定线，上限卡大版本」：`langgraph>=1.2,<2`。
锁文件保证你和 CI、和同学拿到完全相同的依赖树。

**最小命令**

```bash
uv sync                        # 安装项目 + 默认 dev 组
uv sync --extra prod           # 额外安装 Postgres checkpoint / Redis
uv sync --frozen               # CI：严格按 lock 安装，不允许改锁
uv lock --check                # 只检查 lock 是否与 pyproject 一致
uv run pytest -q               # 在项目环境里跑测试
uv run python scripts/serve.py # 启动服务
uv add "httpx>=0.28"           # 加依赖，同时更新 pyproject 与 lock
```

**本项目在哪**

- `pyproject.toml:6` 要求 Python `>=3.13`；`:12` 起是运行时依赖；`:33` 起是
  `optional-dependencies`（`prod` / `eval` / `sandbox` / `a2a` / `observability`）。
- `pyproject.toml:54` 的 `[dependency-groups] dev` 是 PEP 735 的开发依赖组，和运行时
  extra 分开：`pytest`、`ruff`、`mypy` 不应该进生产镜像。
- `pyproject.toml:69` 的 `[tool.uv] default-groups = ["dev"]` 让 `uv sync` 默认装开发组，
  所以 clone 下来直接能跑测试。
- `pyproject.toml:72` 起配置 pytest：`testpaths = ["tests"]`、`asyncio_mode = "auto"`。
- `pyproject.toml:82` 起配置 ruff lint，`select` 里特意加了 `ASYNC`、`S`、`PERF`：异步误用、
  安全问题、性能退化是 Agent 项目的高发区。

---

## 8. pytest 基础：测试是重构的安全带

**概念**

pytest 用 `test_` 开头的函数收集测试，用普通 `assert` 断言，失败时自动展示表达式细节。
`tmp_path` 是内置 fixture，提供每个测试独立的临时目录；`pytest.raises` 断言异常；
`pytest-asyncio` 让 `async def` 测试可以被执行。

**为什么需要**

Agent 的行为依赖模型输出，真机测试既慢又不稳定。正确做法是把模型替换成脚本化的假对象，
把外部依赖替换成内存实现，然后断言「工具是否走了网关」「越权是否被拒绝」「重启后 checkpoint
是否还在」。这些测试不需要 API key，能在 CI 里几秒跑完。

**最小代码**

```python
import pytest

from agent_platform.gateway.ratelimit import RateLimiter

def test_token_bucket_blocks_after_capacity() -> None:
    limiter = RateLimiter(capacity=2, refill_per_second=0.0)
    assert limiter.allow("u1") is True
    assert limiter.allow("u1") is True
    assert limiter.allow("u1") is False

async def test_async_tool(tmp_path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("hi", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "hi"

def test_permission_denied() -> None:
    with pytest.raises(PermissionError):
        raise PermissionError("denied")
```
本项目 `asyncio_mode = "auto"`，所以 `async def test_...` 不需要手动加
`@pytest.mark.asyncio`。`tmp_path` 是 `pathlib.Path`，测试结束自动清理。

**本项目在哪**

- `tests/test_gateway.py` 用内存 `ToolRegistry` + `AuditLogger` 测「允许 / 拒绝 / 未知工具」
  三条路径，并断言审计事件的 `outcome`。
- `tests/test_checkpoint.py:38` 用 `tmp_path` 建 SQLite 文件，先写入、关闭连接，再用新实例
  读回，验证「进程重启后状态还在」。
- `tests/test_agent.py` 的 `_ScriptedModel` 按脚本依次返回「工具调用」和「最终答复」，
  这样不用真实模型也能测完整的 ReAct 循环。
- `tests/test_agent.py:66` 用 `principal_context(guest)` 验证越权工具调用抛
  `PermissionDenied`，这是治理链路的回归测试。

---

## 9. 读本项目代码前必须会的 10 个 Python 特性

1. **`from __future__ import annotations`**：把注解延迟为字符串，避免运行时求值，也让
   前向引用更自由。本项目几乎每个模块第一行都有。
2. **`X | None` 与内置泛型**：`str | None`、`list[str]`、`dict[str, Any]`。读
   `gateway/config.py` 时到处是这种写法。
3. **`TypedDict` + `NotRequired`**：描述「一个字典有哪些键」，用于 LangGraph 的状态
   schema。见 `core/workflow.py:43` 的 `TicketState`。
4. **`Literal`**：把字符串限制为几个合法值。`ClassifyResult.intent` 只能是
   `"sensitive"` 或 `"query"`；审计结果只能是四种 `Outcome`。
5. **`frozenset` 与集合运算**：角色集合不可变、可哈希，`principal.roles & rule.roles`
   判断是否有交集。见 `gateway/policy.py`。
6. **`@dataclass(frozen=True, slots=True)`**：不可变、省内存的内部配置对象。见
   `gateway/mcp_client.py:49`。
7. **Pydantic `BaseModel` + `Field(default_factory=...)`**：边界校验与可变默认值。
   `Principal.roles`、`AuditEvent.arguments` 都用了 `default_factory`。
8. **`async def` / `await` / `async for` / `AsyncIterator`**：异步函数、异步迭代器、
   异步生成器。见 `Agent.stream()` 和 `api/streaming.py`。
9. **`ContextVar` + `@contextmanager`**：请求级隐式状态与配对清理。见
   `agent_platform/context.py`。
10. **`Protocol`**：结构化类型，用来解耦实现与测试替身。见 `Limiter`、`ResponseCache`、
    `ChatAgent`。

补充一个 3.13 语法：`tools/filesystem.py` 里的
`def _sandbox_tool[**P, R](fn: Callable[P, R]) -> Callable[P, R]` 是 PEP 695 泛型语法，
等价于旧写法 `TypeVar` + `ParamSpec`。

---

## 自测题

1. **`from __future__ import annotations` 解决了什么问题？**
   答：让注解在运行时不被求值，避免前向引用报错，也减少 import 开销；类型检查器仍然能读到。

2. **为什么 `asyncio.gather` 能缩短总耗时？**
   答：它把多个协程并发注册到事件循环，I/O 等待期间互相让出，总耗时接近最慢的那个，而不是
   所有耗时相加。

3. **`ContextVar` 和全局变量在并发请求下有什么区别？**
   答：每个 asyncio task 有独立的上下文副本，`set` 不会影响其他 task；全局变量会被并发请求
   互相覆盖。规范用法是 `set` 后用 `reset(token)` 恢复。

4. **什么时候用 dataclass，什么时候用 Pydantic？**
   答：内部、可信、不可变的结构用 dataclass；来自 HTTP、环境变量、模型输出等不可信边界，
   需要校验和转换时用 Pydantic。

5. **`Protocol` 和抽象基类（ABC）有什么区别？**
   答：Protocol 是结构化类型，实现类不需要显式继承；ABC 要求继承并实现抽象方法。Protocol
   更适合给测试替身和第三方实现留接口。

6. **`__exit__` 返回 `True` 会发生什么？**
   答：异常被吞掉，不会继续向上传播。只有确实处理了异常才应该返回 `True`，否则会掩盖 bug。

7. **`uv sync` 和 `uv sync --frozen` 的区别？**
   答：前者允许根据 `pyproject.toml` 更新锁文件；后者严格按现有 `uv.lock` 安装，锁不一致
   就失败。CI 应该用 `--frozen`。

---

## 面试追问

1. **「你在项目里怎么传递用户身份？为什么不用函数参数？」**
   要点：工具函数签名由 LangGraph 决定，无法插入 `principal` 参数；用 `ContextVar` 按请求
   隔离；在 `Agent.chat` 进入时 `set`，工具执行时 `get`；`finally` 里 `reset`，防止泄漏。
   可以补一句：这让 gateway 不依赖 HTTP 层，身份从 JWT 到工具执行是一条连续的链。

2. **「async 代码里有一个同步阻塞调用，会怎么样？」**
   要点：阻塞的是事件循环线程，所有协程都停住；表现为吞吐骤降、健康检查超时。解决方式是换
   异步驱动，或用 `asyncio.to_thread` 把阻塞调用丢到线程池；不能靠加线程数糊过去。

3. **「为什么用 Protocol 而不是抽象基类？」**
   要点：上层只依赖行为（`aallow` / `get` / `set`），不关心继承关系；测试替身不需要继承
   真实类；第三方实现（如 Redis 版限流器）可以零侵入接入。ABC 适合需要共享实现的场景。

4. **「你的项目怎么保证依赖可复现？」**
   要点：`pyproject.toml` 锁大版本范围，`uv.lock` 锁精确版本与哈希；CI 用
   `uv sync --frozen` + `uv lock --check`；运行时 extra 与 dev 组分离，生产镜像不装 pytest。

5. **「Pydantic v2 和 dataclass 的取舍？」**
   要点：Pydantic 在构造时校验、能生成 JSON Schema（工具参数就靠它）、能和 FastAPI /
   LangChain 集成；代价是性能与依赖。内部热点路径用 dataclass，边界用 Pydantic。
   注意 v2 的 `model_dump()`、`model_validate()` 与 v1 不同。

6. **「你怎么测试一个依赖 LLM 的 Agent？」**
   要点：把模型替换成脚本化假对象（`_ScriptedModel`），按顺序返回工具调用和最终答复；
   工具走真实网关但文件系统用 `tmp_path` 沙箱；断言工具调用次数、审计结果、最终文本；
   真机评测放到单独的 golden dataset 流程，不放进单元测试。
