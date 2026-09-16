"""可观测层：OpenTelemetry 插桩 → OTLP 导出。

对应 PLAN.md Phase 3。用 OTel 的 GenAI 语义约定给模型调用打 span（模型名 / token
用量 / 延迟），经 OTLP/HTTP 导出到 Langfuse（自托管，支持 OTLP 接收）或 Jaeger。
未配置 endpoint 时是零开销 no-op，不影响本地开发与单元测试。
"""
