"""Minimal Prometheus text-format metrics (stdlib-only, thread-safe)."""

from __future__ import annotations

import threading
import time


class Metrics:
    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._tool_calls: dict[tuple[str, str], int] = {}  # (tool, status) -> count
        self._rate_limited = 0
        self._started = clock()

    def record_tool(self, tool: str, ok: bool) -> None:
        with self._lock:
            key = (tool, "ok" if ok else "error")
            self._tool_calls[key] = self._tool_calls.get(key, 0) + 1

    def record_rate_limited(self) -> None:
        with self._lock:
            self._rate_limited += 1

    def render(self, *, memory_count: int | None = None) -> str:
        lines = [
            "# HELP reliquary_tool_calls_total MCP tool calls by tool and status.",
            "# TYPE reliquary_tool_calls_total counter",
        ]
        with self._lock:
            items = sorted(self._tool_calls.items())
            rate_limited = self._rate_limited
            uptime = self._clock() - self._started
        for (tool, status), count in items:
            lines.append(f'reliquary_tool_calls_total{{tool="{tool}",status="{status}"}} {count}')
        lines += [
            "# HELP reliquary_rate_limited_total Requests rejected by the rate limiter.",
            "# TYPE reliquary_rate_limited_total counter",
            f"reliquary_rate_limited_total {rate_limited}",
            "# HELP reliquary_uptime_seconds Process uptime in seconds.",
            "# TYPE reliquary_uptime_seconds gauge",
            f"reliquary_uptime_seconds {uptime:.0f}",
        ]
        if memory_count is not None:
            lines += [
                "# HELP reliquary_memory_count Approximate stored memory count.",
                "# TYPE reliquary_memory_count gauge",
                f"reliquary_memory_count {memory_count}",
            ]
        return "\n".join(lines) + "\n"
