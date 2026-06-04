"""Unit tests for the fixed-window per-key rate limiter."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from ratelimit import RateLimiter  # noqa: E402


def test_within_limit_allows():
    rl = RateLimiter(limit=3, window=60.0)
    assert rl.allow("key1") is True
    assert rl.allow("key1") is True
    assert rl.allow("key1") is True


def test_over_limit_blocks():
    rl = RateLimiter(limit=2, window=60.0)
    assert rl.allow("key1") is True
    assert rl.allow("key1") is True
    assert rl.allow("key1") is False


def test_window_reset_allows_again():
    now = [0.0]

    def fake_clock():
        return now[0]

    rl = RateLimiter(limit=2, window=60.0, clock=fake_clock)
    assert rl.allow("key1") is True
    assert rl.allow("key1") is True
    assert rl.allow("key1") is False

    # Advance clock past the window boundary
    now[0] = 61.0
    assert rl.allow("key1") is True
    assert rl.allow("key1") is True
    assert rl.allow("key1") is False


def test_limit_zero_always_allows():
    rl = RateLimiter(limit=0, window=60.0)
    for _ in range(100):
        assert rl.allow("key1") is True


def test_limit_negative_always_allows():
    rl = RateLimiter(limit=-5, window=60.0)
    for _ in range(50):
        assert rl.allow("any") is True


def test_different_keys_are_independent():
    rl = RateLimiter(limit=1, window=60.0)
    assert rl.allow("a") is True
    assert rl.allow("b") is True
    # Both keys are now at their limit
    assert rl.allow("a") is False
    assert rl.allow("b") is False
    # A new key is still allowed
    assert rl.allow("c") is True
