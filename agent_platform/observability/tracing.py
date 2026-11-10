"""OpenTelemetry 插桩：模型调用 span → OTLP/HTTP 导出。

设计要点：
- **零开销降级**：未配置 ``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``LANGFUSE_HOST`` 时，
  OTel 返回 no-op tracer，span 不出门，不影响本地开发与测试。
- **按 run_id 隔离**：回调跨并发请求共享，span 用 ``run_id`` 建索引，避免并发串扰。
- **GenAI 语义约定**：记录模型名、token 用量、延迟、流式首 token 延迟（TTFT），
  落到 Langfuse/Jaeger 均可看。
- **agent.* 约定**：工具调用用 :func:`start_tool_span` 打 ``agent.tool.name`` 与
  ``agent.principal.id``，让工具调用在 trace 里可检索、可归因到用户。
"""

from __future__ import annotations

import os
import time
from typing import Any
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import ChatGenerationChunk, GenerationChunk, LLMResult
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span

SERVICE_NAME = "agent-platform"


def _otlp_endpoint() -> str:
    """解析 OTLP 导出端点：优先 OTEL_ 环境变量，其次 Langfuse 自托管 OTLP 接收端。"""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if endpoint:
        return endpoint
    langfuse_host = os.environ.get("LANGFUSE_HOST", "").strip()
    if langfuse_host:
        return f"{langfuse_host.rstrip('/')}/api/public/otel/v1/traces"
    return ""


def init_tracing(service_name: str = SERVICE_NAME) -> None:
    """初始化 OTel provider。配置了 endpoint 才真正导出，否则仍是 no-op。"""
    endpoint = _otlp_endpoint()
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
    trace.set_tracer_provider(provider)


def start_tool_span(name: str, principal_id: str | None) -> Span:
    """给一次工具调用开一个 ``agent.*`` span。未配置 OTel 时是 no-op span，零开销。

    语义约定：``agent.tool.name``（工具名）+ ``agent.principal.id``（调用方身份）。
    """
    span = trace.get_tracer(SERVICE_NAME).start_span("agent.tool.call")
    span.set_attribute("agent.tool.name", name)
    if principal_id is not None:
        span.set_attribute("agent.principal.id", principal_id)
    return span


class TracingCallback(BaseCallbackHandler):
    """给每次模型调用打一个 OTel span（模型名 / token 用量 / 延迟 / 首 token 延迟）。"""

    def __init__(self) -> None:
        self._tracer = trace.get_tracer(SERVICE_NAME)
        self._spans: dict[str, tuple[Any, float]] = {}
        self._first_token: dict[str, float] = {}

    def on_llm_start(
        self, serialized: dict[str, Any], prompts: list[str], **kwargs: Any
    ) -> None:
        run_id = kwargs.get("run_id")
        if run_id is None:
            return
        span = self._tracer.start_span("llm.call")
        model = (serialized.get("kwargs") or {}).get("model")
        if model:
            span.set_attribute("gen_ai.request.model", str(model))
        self._spans[str(run_id)] = (span, time.perf_counter())

    def on_llm_new_token(
        self,
        token: str | list[str | dict[str, Any]],
        *,
        chunk: GenerationChunk | ChatGenerationChunk | None = None,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """流式首个 token 到达时记录时间，用于 TTFT（time to first token）。"""
        key = str(run_id)
        if key in self._spans and key not in self._first_token:
            self._first_token[key] = time.perf_counter()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        run_id = kwargs.get("run_id")
        if run_id is None:
            return
        entry = self._spans.pop(str(run_id), None)
        if entry is None:
            return
        span, start = entry
        llm_output = response.llm_output or {}
        usage = llm_output.get("token_usage") or {}
        span.set_attribute(
            "gen_ai.usage.input_tokens", int(usage.get("prompt_tokens") or 0)
        )
        span.set_attribute(
            "gen_ai.usage.output_tokens", int(usage.get("completion_tokens") or 0)
        )
        first = self._first_token.pop(str(run_id), None)
        if first is not None:
            span.set_attribute(
                "gen_ai.response.time_to_first_token_ms", (first - start) * 1000
            )
        span.set_attribute("llm.duration_ms", (time.perf_counter() - start) * 1000)
        span.end()
