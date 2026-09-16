"""可观测性单元测试：端点解析与 TracingCallback 的 span 生命周期（不依赖真实导出）。"""

from types import SimpleNamespace

from agent_platform.observability.tracing import TracingCallback, _otlp_endpoint


def test_otlp_endpoint_prefers_explicit_env(monkeypatch) -> None:
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4318/v1/traces")
    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse:3000")
    assert _otlp_endpoint() == "http://otel:4318/v1/traces"


def test_otlp_endpoint_falls_back_to_langfuse(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse:3000/")
    assert _otlp_endpoint() == "http://langfuse:3000/api/public/otel/v1/traces"


def test_otlp_endpoint_empty_when_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    assert _otlp_endpoint() == ""


def test_tracing_callback_span_lifecycle() -> None:
    cb = TracingCallback()
    run_id = SimpleNamespace()  # 任意可 str 化的对象

    cb.on_llm_start(
        {"kwargs": {"model": "deepseek-chat"}},
        ["hi"],
        run_id=run_id,
    )
    assert str(run_id) in cb._spans

    response = SimpleNamespace(
        llm_output={"token_usage": {"prompt_tokens": 12, "completion_tokens": 7}}
    )
    cb.on_llm_end(response, run_id=run_id)
    assert str(run_id) not in cb._spans  # span 已 end 并清理


def test_tracing_callback_ignores_unknown_run_id() -> None:
    cb = TracingCallback()
    # 没有 on_llm_start 的 on_llm_end 不应报错
    cb.on_llm_end(SimpleNamespace(llm_output={}), run_id=SimpleNamespace())
    assert cb._spans == {}
