"""Append-only JSONL retrieval-event log (stdlib-only, thread-safe). Disabled when path is falsy.

Best-effort telemetry, NOT a security record: unlike audit.py's AuditLog, record() must
never raise into its caller — a stats-write failure can never be allowed to break a live
search/fetch, so any error is swallowed and debug-logged instead. The hot path only
appends; aggregation (reducing the JSONL back into counts) is done offline by lint.py.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)


class RetrievalStatsLog:
    def __init__(self, path: str | None, clock=time.time) -> None:
        self.path = path or None
        self._clock = clock
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
        except Exception:
            logger.debug("retrieval_stats: failed to write to %s", self.path, exc_info=True)


def aggregate(path: str | None) -> dict:
    """Pure reducer: read the JSONL log back and fold it into summary counts.

    Tolerant of a missing file, an unreadable path, or malformed/id-less lines — those
    are skipped rather than raising. Streams the file line-by-line rather than loading
    it all into memory.
    """
    result = {"by_id": {}, "by_domain": {}, "by_topic": {}, "events": 0}
    if not path:
        return result

    try:
        # errors="replace" so one corrupt byte can't blow up the whole aggregation;
        # a garbled line just fails json.loads below and is skipped like any bad line.
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return result

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

    return result
