"""Append-only JSONL retrieval-event log (stdlib-only, thread-safe). Disabled when path is falsy.

Best-effort telemetry, NOT a security record: unlike audit.py's AuditLog, record() must
never raise into its caller — a stats-write failure can never be allowed to break a live
search/fetch, so any error is swallowed and debug-logged instead. The hot path only
appends; aggregation (reducing the JSONL back into counts) is done by lint.py and, off
the event loop, by the needs-review resource.

The log rotates once it passes ``max_bytes``: the current file becomes ``<path>.1``
(replacing the previous one), so the log and every aggregation over it stay bounded
at about twice ``max_bytes``. Older events age out.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

# ~150k events per file at ~110 bytes per event.
DEFAULT_MAX_BYTES = 16 * 1024 * 1024


class RetrievalStatsLog:
    def __init__(self, path: str | None, clock=time.time, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        self.path = path or None
        self._clock = clock
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def record(self, event: str, items: list[dict]) -> None:
        if not self.path:
            return
        # This runs in the live search/fetch hot path and must NEVER raise, so the ENTIRE
        # body is guarded: a raising clock(), an un-serializable id (its __str__ can raise
        # past json.dumps's default=str), a bad items shape (None / non-dict members), or
        # an IO error all get swallowed and debug-logged so telemetry can't break a request.
        try:
            ts = self._clock()
            lines = []
            for item in items:
                entry = {"ts": ts, "event": event, "id": item.get("id")}
                domain = item.get("domain")
                topic = item.get("topic")
                if domain is not None:
                    entry["domain"] = domain
                if topic is not None:
                    entry["topic"] = topic
                lines.append(json.dumps(entry, default=str) + "\n")
            if not lines:
                return
            with self._lock:
                directory = os.path.dirname(self.path) or "."
                os.makedirs(directory, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.writelines(lines)
                    size = fh.tell()
                if self.max_bytes and size > self.max_bytes:
                    os.replace(self.path, f"{self.path}.1")
        except Exception:
            logger.debug("retrieval_stats: failed to write to %s", self.path, exc_info=True)


def aggregate(path: str | None) -> dict:
    """Pure reducer: read the JSONL log (the rotated ``<path>.1`` first, then ``path``)
    back and fold it into summary counts.

    Tolerant of a missing file, an unreadable path, or malformed/id-less lines — those
    are skipped rather than raising. Streams the file line-by-line rather than loading
    it all into memory.
    """
    result = {"by_id": {}, "by_domain": {}, "by_topic": {}, "events": 0}
    if not path:
        return result
    _fold(f"{path}.1", result)
    _fold(path, result)
    return result


def _fold(path: str, result: dict) -> None:
    try:
        # errors="replace" so one corrupt byte can't blow up the whole aggregation;
        # a garbled line just fails json.loads below and is skipped like any bad line.
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return

    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            item_id = entry.get("id")
            if not item_id:
                continue

            result["events"] += 1
            ts = entry.get("ts")
            domain = entry.get("domain")
            topic = entry.get("topic")
            # domain/topic are caller/importer-controlled (via metadata); a non-scalar
            # value (list/dict) must not become an unhashable dict key below, which
            # would crash lint and the live needs-review resource read. Treat it as
            # absent, same as a missing value.
            if not isinstance(domain, str):
                domain = None
            if not isinstance(topic, str):
                topic = None

            record = result["by_id"].get(item_id)
            if record is None:
                record = {"count": 0, "last_ts": ts, "domain": None, "topic": None}
                result["by_id"][item_id] = record
            record["count"] += 1
            # last_ts is a true max; domain/topic track the values from the most-recent
            # event BY ts (gated on the same condition), so they stay consistent with
            # last_ts even if the file's lines are out of ts order.
            if ts is not None and (record["last_ts"] is None or ts >= record["last_ts"]):
                record["last_ts"] = ts
                if domain is not None:
                    record["domain"] = domain
                if topic is not None:
                    record["topic"] = topic

            if domain is not None:
                result["by_domain"][domain] = result["by_domain"].get(domain, 0) + 1
                if topic is not None:
                    # Key joins domain and topic with a tab. This is unambiguous as long as
                    # neither value contains a literal tab; domains/topics are slug-like, so
                    # embedded tabs don't occur in practice.
                    key = f"{domain}\t{topic}"
                    result["by_topic"][key] = result["by_topic"].get(key, 0) + 1
