"""LangGraph 工单审批工作流：意图识别 → 路由 → 工具执行 → 人工审批（HITL）。

这是「工作流优先」编排（区别于 core.agent 的自主 ReAct）：图结构确定性更强，
适合「敏感操作必须人工审批」这类合规场景。核心机制：

- 敏感操作在**执行前**进入审批节点，`interrupt()` 暂停图运行（checkpoint 落盘），
  由外部（API/前端）携带 `Command(resume=...)` 恢复后，才真正调用网关工具；
- 普通查询跳过审批，直接执行 + 答复。

身份与工具：复用 core.agent 的 `current_principal`（contextvar）按请求解析调用方，
工具始终经 Gateway 走鉴权/权限/限流/审计，治理不旁路。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal, NotRequired, TypedDict, cast

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from agent_platform.core.agent import build_model, current_principal, principal_context
from agent_platform.core.checkpoint import build_checkpointer
from agent_platform.gateway.config import ModelSettings
from agent_platform.gateway.gateway import Gateway
from agent_platform.gateway.policy import Principal
from agent_platform.router.cost import CostCallbackHandler, CostTracker


class ClassifyResult(BaseModel):
    """意图识别结果：意图 + 要调用的工具与参数。"""

    intent: Literal["sensitive", "query"]
    tool_name: str
    tool_args: dict[str, Any] = Field(default_factory=dict)


class TicketState(TypedDict):
    """工单工作流的状态（含可被 checkpoint 持久化的全部字段）。"""

    request: str
    intent: NotRequired[str]
    tool_name: NotRequired[str]
    tool_args: NotRequired[dict[str, Any]]
    approval: NotRequired[dict[str, Any]]
    tool_result: NotRequired[Any]
    reply: NotRequired[str]


class TicketRun(BaseModel):
    """一次工单运行的结果视图（供 API 返回）。

    - status=interrupted：图停在审批节点，interrupt 携带审批所需上下文；
    - status=completed：已跑完，reply 为最终答复。
    """

    thread_id: str
    status: Literal["interrupted", "completed"]
    interrupt: dict[str, Any] | None = None
    reply: str | None = None


ClassifyFn = Callable[[str], Awaitable[ClassifyResult]]
AnswerFn = Callable[[str, Any], Awaitable[str]]


def _default_classify(model: BaseChatModel, tool_names: list[str]) -> ClassifyFn:
    """默认意图识别：LLM 结构化输出（function calling，DeepSeek 不支持 response_format）。"""
    structured = model.with_structured_output(ClassifyResult, method="function_calling")

    async def classify(request: str) -> ClassifyResult:
        return cast(ClassifyResult, await structured.ainvoke(
            [
                SystemMessage(
                    "你是工单意图识别器。把请求分成两类："
                    "「sensitive」= 写/删/改/覆盖文件或配置的敏感操作；"
                    "「query」= 读/列/查等普通查询。"
                    "同时提取要调用的工具名（仅限以下之一）与参数（JSON）。"
                    f"可用工具：{', '.join(tool_names)}。"
                ),
                HumanMessage(request),
            ]
        ))

    return classify


def _default_answer(model: BaseChatModel) -> AnswerFn:
    """默认答复生成：LLM 用一句话向用户说明执行结果。"""

    async def answer(request: str, tool_result: Any) -> str:
        msg = await model.ainvoke(
            [
                SystemMessage("你是企业助手，用一句话向用户简洁、准确地说明操作结果。"),
                HumanMessage(f"用户请求：{request}\n工具执行结果：{tool_result}"),
            ]
        )
        return str(msg.content)

    return answer


class TicketWorkflow:
    """对编译好的工单审批图的高层封装。"""

    def __init__(
        self,
        graph: CompiledStateGraph[Any, Any, Any, Any],
        *,
        cost_tracker: CostTracker | None = None,
    ) -> None:
        self._graph = graph
        self._cost_tracker = cost_tracker

    async def start(
        self, request: str, *, principal: Principal, thread_id: str
    ) -> TicketRun:
        """发起工单；敏感操作会停在审批节点（status=interrupted）。"""
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "callbacks": _cost_callbacks(self._cost_tracker, principal),
        }
        with principal_context(principal):
            result = await self._graph.ainvoke({"request": request}, config)
        return _to_run(result, thread_id)

    async def resume(
        self,
        thread_id: str,
        *,
        principal: Principal,
        approve: bool,
        note: str = "",
    ) -> TicketRun:
        """以审批意见恢复被暂停的工单。"""
        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id},
            "callbacks": _cost_callbacks(self._cost_tracker, principal),
        }
        with principal_context(principal):
            result = await self._graph.ainvoke(
                Command(resume={"approve": approve, "note": note}), config
            )
        return _to_run(result, thread_id)


def _cost_callbacks(
    tracker: CostTracker | None, principal: Principal
) -> list[BaseCallbackHandler]:
    if tracker is None:
        return []
    return [CostCallbackHandler(tracker, principal_id=principal.id)]


def _to_run(result: dict[str, Any], thread_id: str) -> TicketRun:
    interrupts = result.get("__interrupt__")
    if interrupts:
        return TicketRun(
            thread_id=thread_id,
            status="interrupted",
            interrupt=interrupts[0].value,
        )
    return TicketRun(thread_id=thread_id, status="completed", reply=result.get("reply", ""))


def build_ticket_workflow(
    gateway: Gateway,
    *,
    model: BaseChatModel | None = None,
    classify: ClassifyFn | None = None,
    answer: AnswerFn | None = None,
    cost_tracker: CostTracker | None = None,
    checkpointer: Any | None = None,
) -> TicketWorkflow:
    """组装工单审批图。测试可注入 classify/answer（无需真实模型）。

    checkpointer 必须由调用方传入持久化后端：工单停在 ``interrupt()`` 等人审批，
    人可能几小时后才点「同意」，用内存 saver 会因重启丢掉整个审批上下文。
    """
    if classify is None:
        model = model or build_model(ModelSettings())
        classify = _default_classify(model, sorted(gateway.tool_names()))
    if answer is None:
        model = model or build_model(ModelSettings())
        answer = _default_answer(model)

    async def classify_node(state: TicketState) -> dict[str, Any]:
        result = await classify(state["request"])
        return {
            "intent": result.intent,
            "tool_name": result.tool_name,
            "tool_args": result.tool_args,
        }

    async def approval_node(state: TicketState) -> dict[str, Any]:
        # 关键：interrupt() 暂停图并返回审批上下文；恢复时这里返回 resume 值。
        decision = interrupt(
            {
                "type": "approval",
                "request": state["request"],
                "action": state["tool_name"],
                "arguments": state["tool_args"],
            }
        )
        return {"approval": decision}

    async def execute_node(state: TicketState) -> dict[str, Any]:
        principal = current_principal.get()
        if principal is None:
            raise RuntimeError("工作流执行时未设置 principal")
        result = await gateway.call(principal, state["tool_name"], state["tool_args"])
        return {"tool_result": result}

    async def answer_node(state: TicketState) -> dict[str, Any]:
        approval = state.get("approval")
        if state["intent"] == "sensitive" and approval and not approval.get("approve"):
            return {"reply": "已按审批意见拒绝该操作。"}
        reply = await answer(state["request"], state.get("tool_result"))
        return {"reply": reply}

    def route(state: TicketState) -> str:
        return "approval" if state["intent"] == "sensitive" else "execute"

    def route_after_approval(state: TicketState) -> str:
        approval = state.get("approval")
        if approval and approval.get("approve"):
            return "execute"
        return "answer"

    graph = StateGraph(TicketState)
    graph.add_node("classify", classify_node)
    graph.add_node("approval", approval_node)
    graph.add_node("execute", execute_node)
    graph.add_node("answer", answer_node)
    graph.add_edge(START, "classify")
    graph.add_conditional_edges("classify", route, {"approval": "approval", "execute": "execute"})
    graph.add_conditional_edges(
        "approval", route_after_approval, {"execute": "execute", "answer": "answer"}
    )
    graph.add_edge("execute", "answer")
    graph.add_edge("answer", END)

    return TicketWorkflow(
        graph.compile(
            checkpointer=checkpointer
            if checkpointer is not None
            else build_checkpointer("memory")
        )
    )
