"""轻量 Prometheus 指标：零第三方依赖实现文本暴露格式。

为什么不用 ``prometheus-client``？
- 依赖越多，演示环境越容易起不来；Agent 平台只需要 counter / gauge / histogram
  这三种原语，Prometheus 的文本暴露格式（text exposition format）本身是稳定的
  公开规范，几十行就能实现，且完全可测。
- 换来的是 ``/metrics`` 端点能在任何环境（含离线 CI）稳定工作。

设计要点：
- 单进程内注册表（模块级 ``METRICS``），避免各处 new 出孤儿指标；
- 指标值按 label 组合分桶，暴露时按名称排序，保证输出可复现（便于做快照测试）；
- 所有操作加锁：ASGI 是多协程的，Counter 自增必须原子。
"""

from __future__ import annotations

import math
import threading
from collections.abc import Iterable, Mapping
from typing import Literal

MetricType = Literal["counter", "gauge", "histogram"]

#: 延迟直方图默认桶（秒）。覆盖「毫秒级工具调用」到「分钟级长任务」。
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
)


def _escape_label(value: str) -> str:
    """转义 label 值中的反斜杠、双引号与换行（Prometheus 文本格式要求）。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _format_labels(labels: Mapping[str, str]) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape_label(v)}"' for k, v in sorted(labels.items()))
    return f"{{{inner}}}"


class Counter:
    """单调递增计数器（只增不减）。"""

    def __init__(self, name: str, help_text: str, *, labels: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help = help_text
        self.type: MetricType = "counter"
        self.label_names = labels
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def inc(self, value: float = 1.0, **labels: str) -> None:
        key = tuple(labels.get(k, "") for k in self.label_names)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def value(self, **labels: str) -> float:
        key = tuple(labels.get(k, "") for k in self.label_names)
        with self._lock:
            return self._values.get(key, 0.0)

    def samples(self) -> list[tuple[dict[str, str], float]]:
        with self._lock:
            keys = sorted(self._values)
            return [
                (dict(zip(self.label_names, key, strict=True)), self._values[key])
                for key in keys
            ]


class Gauge:
    """可增可减的仪表值（当前活跃数、最近一次评分等）。"""

    def __init__(self, name: str, help_text: str, *, labels: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help = help_text
        self.type: MetricType = "gauge"
        self.label_names = labels
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = threading.Lock()

    def set(self, value: float, **labels: str) -> None:
        key = tuple(labels.get(k, "") for k in self.label_names)
        with self._lock:
            self._values[key] = value

    def inc(self, value: float = 1.0, **labels: str) -> None:
        key = tuple(labels.get(k, "") for k in self.label_names)
        with self._lock:
            self._values[key] = self._values.get(key, 0.0) + value

    def dec(self, value: float = 1.0, **labels: str) -> None:
        self.inc(-value, **labels)

    def value(self, **labels: str) -> float:
        key = tuple(labels.get(k, "") for k in self.label_names)
        with self._lock:
            return self._values.get(key, 0.0)

    def samples(self) -> list[tuple[dict[str, str], float]]:
        with self._lock:
            keys = sorted(self._values)
            return [
                (dict(zip(self.label_names, key, strict=True)), self._values[key])
                for key in keys
            ]


class Histogram:
    """累积直方图：按桶累计计数，并维护总和与样本数。"""

    def __init__(
        self,
        name: str,
        help_text: str,
        *,
        labels: tuple[str, ...] = (),
        buckets: Iterable[float] = DEFAULT_BUCKETS,
    ) -> None:
        self.name = name
        self.help = help_text
        self.type: MetricType = "histogram"
        self.label_names = labels
        self.buckets = tuple(sorted(buckets))
        self._counts: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = {}
        self._totals: dict[tuple[str, ...], int] = {}
        self._lock = threading.Lock()

    def observe(self, value: float, **labels: str) -> None:
        key = tuple(labels.get(k, "") for k in self.label_names)
        with self._lock:
            counts = self._counts.setdefault(key, [0] * len(self.buckets))
            for i, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[i] += 1
            self._sums[key] = self._sums.get(key, 0.0) + value
            self._totals[key] = self._totals.get(key, 0) + 1

    def count(self, **labels: str) -> int:
        key = tuple(labels.get(k, "") for k in self.label_names)
        with self._lock:
            return self._totals.get(key, 0)

    def sum(self, **labels: str) -> float:
        key = tuple(labels.get(k, "") for k in self.label_names)
        with self._lock:
            return self._sums.get(key, 0.0)

    def snapshot(self) -> list[tuple[dict[str, str], list[int], float, int]]:
        """一次性快照：``(labels, 各桶累计计数, sum, count)`` 列表，供渲染使用。"""
        with self._lock:
            keys = sorted(self._counts)
            return [
                (
                    dict(zip(self.label_names, key, strict=True)),
                    list(self._counts[key]),
                    self._sums.get(key, 0.0),
                    self._totals.get(key, 0),
                )
                for key in keys
            ]


class Registry:
    """指标注册表：登记 + 渲染成 Prometheus 文本格式。"""

    def __init__(self) -> None:
        self._metrics: dict[str, Counter | Gauge | Histogram] = {}
        self._lock = threading.Lock()

    def counter(self, name: str, help_text: str, *, labels: tuple[str, ...] = ()) -> Counter:
        with self._lock:
            existing = self._metrics.get(name)
            # 同名指标只保留首次注册的实例，避免模块级常量被重复构造后各写各的。
            if isinstance(existing, Counter):
                return existing
            metric = Counter(name, help_text, labels=labels)
            self._metrics[name] = metric
            return metric

    def gauge(self, name: str, help_text: str, *, labels: tuple[str, ...] = ()) -> Gauge:
        with self._lock:
            existing = self._metrics.get(name)
            if isinstance(existing, Gauge):
                return existing
            metric = Gauge(name, help_text, labels=labels)
            self._metrics[name] = metric
            return metric

    def histogram(
        self,
        name: str,
        help_text: str,
        *,
        labels: tuple[str, ...] = (),
        buckets: Iterable[float] = DEFAULT_BUCKETS,
    ) -> Histogram:
        with self._lock:
            existing = self._metrics.get(name)
            if isinstance(existing, Histogram):
                return existing
            metric = Histogram(name, help_text, labels=labels, buckets=buckets)
            self._metrics[name] = metric
            return metric

    def render(self) -> str:
        """渲染为 Prometheus ``text/plain; version=0.0.4`` 格式。"""
        with self._lock:
            metrics = [self._metrics[name] for name in sorted(self._metrics)]
        lines: list[str] = []
        for metric in metrics:
            lines.append(f"# HELP {metric.name} {metric.help}")
            lines.append(f"# TYPE {metric.name} {metric.type}")
            if isinstance(metric, Histogram):
                lines.extend(_render_histogram(metric))
            else:
                for labels, value in metric.samples():
                    lines.append(
                        f"{metric.name}{_format_labels(labels)} {_format_number(value)}"
                    )
        return "\n".join(lines) + "\n"


def _render_histogram(histogram: Histogram) -> list[str]:
    lines: list[str] = []
    for base, cumulative, total_sum, total in histogram.snapshot():
        running = 0
        for bound, observed in zip(histogram.buckets, cumulative, strict=True):
            running += observed
            bucket_labels = {**base, "le": _format_number(bound)}
            lines.append(
                f"{histogram.name}_bucket{_format_labels(bucket_labels)} {running}"
            )
        inf_labels = {**base, "le": "+Inf"}
        lines.append(f"{histogram.name}_bucket{_format_labels(inf_labels)} {total}")
        lines.append(
            f"{histogram.name}_sum{_format_labels(base)} {_format_number(total_sum)}"
        )
        lines.append(f"{histogram.name}_count{_format_labels(base)} {total}")
    return lines


def _format_number(value: float) -> str:
    """整数不带小数点，小数保留足够精度（避免科学计数法让 Prometheus 困惑）。"""
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


#: 进程级指标注册表。
METRICS = Registry()

# ── 应用级指标（模块级单例：全进程共享，避免各处重复注册）────────────────────

#: HTTP 请求计数与耗时（按路由模板，而非原始路径，避免 label 基数爆炸）。
HTTP_REQUESTS = METRICS.counter(
    "agent_http_requests_total",
    "HTTP requests by method, route and status code",
    labels=("method", "route", "status"),
)
HTTP_DURATION = METRICS.histogram(
    "agent_http_request_duration_seconds",
    "HTTP request latency by method and route",
    labels=("method", "route"),
)

#: 模型调用：次数 / token / 成本 / 延迟。成本是「token 经济学」的直接证据。
LLM_CALLS = METRICS.counter(
    "agent_llm_calls_total",
    "LLM calls by model and outcome",
    labels=("model", "status"),
)
LLM_TOKENS = METRICS.counter(
    "agent_llm_tokens_total",
    "LLM tokens by model and kind",
    labels=("model", "kind"),
)
LLM_COST = METRICS.counter(
    "agent_llm_cost_yuan_total",
    "Estimated LLM spend in CNY by model",
    labels=("model",),
)
LLM_LATENCY = METRICS.histogram(
    "agent_llm_latency_seconds",
    "LLM call latency by model",
    labels=("model",),
)

#: 工具调用：按工具名与治理结论（allowed/denied/rate_limited/error）分解。
TOOL_CALLS = METRICS.counter(
    "agent_tool_calls_total",
    "Gateway tool calls by tool and governance outcome",
    labels=("tool", "outcome"),
)
TOOL_LATENCY = METRICS.histogram(
    "agent_tool_latency_seconds",
    "Gateway tool call latency by tool",
    labels=("tool",),
)

#: 安全：护栏拦截、预算拒绝、语义缓存命中率。
GUARD_BLOCKS = METRICS.counter(
    "agent_guard_blocks_total",
    "Guardrail blocks by stage and reason",
    labels=("stage", "reason"),
)
BUDGET_REJECTS = METRICS.counter(
    "agent_budget_rejects_total",
    "Requests rejected by cost or token budget",
)
CACHE_EVENTS = METRICS.counter(
    "agent_semantic_cache_total",
    "Semantic cache lookups by result (hit/miss/bypass)",
    labels=("result",),
)

#: 评测：最近一次评测的均分与通过率（CI 与 /metrics 双向可查）。
EVAL_SCORE = METRICS.gauge(
    "agent_eval_score", "Latest evaluation average score", labels=("suite",)
)
EVAL_PASS_RATE = METRICS.gauge(
    "agent_eval_pass_rate", "Latest evaluation pass rate", labels=("suite",)
)

#: 运行时：活跃会话数、熔断器状态（0=closed, 1=half_open, 2=open）。
ACTIVE_SESSIONS = METRICS.gauge("agent_active_sessions", "In-flight agent sessions")
CIRCUIT_STATE = METRICS.gauge(
    "agent_circuit_breaker_state",
    "Circuit breaker state (0=closed, 1=half_open, 2=open)",
    labels=("target",),
)
