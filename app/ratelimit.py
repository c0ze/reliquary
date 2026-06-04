"""Fixed-window per-key rate limiter (stdlib-only, thread-safe)."""

from __future__ import annotations

import threading
import time


class RateLimiter:
    def __init__(self, limit: int, window: float = 60.0, clock=time.monotonic) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._lock = threading.Lock()
        # key -> [window_start: float, count: int]
        self._buckets: dict[str, list[float | int]] = {}

    def allow(self, key: str) -> bool:
        """True if the call is within the per-key window budget. limit<=0 disables."""
        if self.limit <= 0:
            return True
        now = self._clock()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None or (now - bucket[0]) >= self.window:
                self._buckets[key] = [now, 1]
                return True
            if bucket[1] < self.limit:
                bucket[1] += 1
                return True
            return False
