import contextvars
import threading
from collections import defaultdict
from typing import Any

request_id_context: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="-"
)
preview_id_context: contextvars.ContextVar[str] = contextvars.ContextVar(
    "preview_id", default="-"
)


class MetricRegistry:
    """进程内指标快照；多实例由监控平台分别采集并聚合。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, int] = defaultdict(int)
        for name in (
            "http_requests_total",
            "http_errors_total",
            "preview_success_total",
            "preview_failure_total",
            "send_task_success_total",
            "send_task_failure_total",
            "send_recipient_success_total",
            "send_recipient_failure_total",
            "feishu_rate_limited_total",
            "token_refresh_failure_total",
            "redis_errors_total",
            "rate_limit_rejected_total",
            "csrf_rejected_total",
        ):
            self._counters[name] = 0
        self._durations: dict[str, tuple[int, float]] = {}
        self._gauges: dict[str, int | float] = {"stalled_send_tasks": 0}

    def increment(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self._counters[name] += amount

    def observe(self, name: str, seconds: float) -> None:
        with self._lock:
            count, total = self._durations.get(name, (0, 0.0))
            self._durations[name] = (count + 1, total + seconds)

    def set_gauge(self, name: str, value: int | float) -> None:
        with self._lock:
            self._gauges[name] = value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = dict(sorted(self._counters.items()))
            durations = {
                name: {"count": count, "sumSeconds": round(total, 6)}
                for name, (count, total) in sorted(self._durations.items())
            }
            gauges = dict(sorted(self._gauges.items()))
        return {"counters": counters, "gauges": gauges, "durations": durations}
