"""Unit tests for the Prometheus metrics renderer."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from metrics import Metrics  # noqa: E402


def test_record_tool_ok_appears_in_render():
    m = Metrics(clock=lambda: 0.0)
    m.record_tool("reliquary_search", ok=True)
    output = m.render()
    assert 'reliquary_tool_calls_total{tool="reliquary_search",status="ok"} 1' in output


def test_record_tool_error_appears_in_render():
    m = Metrics(clock=lambda: 0.0)
    m.record_tool("reliquary_add_memory", ok=False)
    output = m.render()
    assert 'reliquary_tool_calls_total{tool="reliquary_add_memory",status="error"} 1' in output


def test_record_tool_counts_accumulate():
    m = Metrics(clock=lambda: 0.0)
    m.record_tool("search", ok=True)
    m.record_tool("search", ok=True)
    m.record_tool("search", ok=False)
    output = m.render()
    assert 'reliquary_tool_calls_total{tool="search",status="ok"} 2' in output
    assert 'reliquary_tool_calls_total{tool="search",status="error"} 1' in output


def test_render_with_memory_count_includes_gauge():
    m = Metrics(clock=lambda: 0.0)
    output = m.render(memory_count=5)
    assert "reliquary_memory_count 5" in output
    assert "# HELP reliquary_memory_count" in output


def test_render_without_memory_count_omits_gauge():
    m = Metrics(clock=lambda: 0.0)
    output = m.render()
    assert "reliquary_memory_count" not in output


def test_record_rate_limited_reflected():
    m = Metrics(clock=lambda: 0.0)
    m.record_rate_limited()
    m.record_rate_limited()
    output = m.render()
    assert "reliquary_rate_limited_total 2" in output


def test_uptime_is_present():
    tick = [0.0]

    def clock():
        return tick[0]

    m = Metrics(clock=clock)
    tick[0] = 42.0
    output = m.render()
    assert "reliquary_uptime_seconds 42" in output


def test_render_ends_with_newline():
    m = Metrics(clock=lambda: 0.0)
    assert m.render().endswith("\n")
