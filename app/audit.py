"""Append-only JSONL write-audit log (stdlib-only, thread-safe). Disabled when path is falsy."""

from __future__ import annotations

import json
import os
import threading
import time


class AuditLog:
    def __init__(self, path: str | None, clock=time.time) -> None:
        self.path = path or None
        self._clock = clock
        self._lock = threading.Lock()

    def record(self, **fields) -> None:
        if not self.path:
            return
        entry = {"ts": self._clock(), **fields}
        line = json.dumps(entry, default=str) + "\n"
        with self._lock:
            directory = os.path.dirname(self.path) or "."
            os.makedirs(directory, exist_ok=True)
            # Created owner-only: the log names memory ids and user ids.
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)
