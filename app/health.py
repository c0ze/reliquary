"""Pure health-check functions for the compiled (synthesis) layer.

Dependency-light: stdlib + plain data (PageInfo lists, count dicts, record
iterables). Shared by the reliquary://needs-review resource and the lint CLI. Every
check is read-only and only *proposes* — a human decides; nothing auto-applies.
"""

from __future__ import annotations

from typing import Any, Iterable

# Below this many total retrieval events the log is too sparse to trust for archive
# proposals: with few events, records that simply haven't been hit yet look identical
# to genuinely cold ones, so cold_records would flag live corpus for deletion.
COLD_RECORD_MIN_EVENTS = 200


def _id_and_meta(rec: Any) -> tuple[Any, dict[str, Any]]:
    """Extract (id, metadata) from a catalog record in either shape.

    Object shape uses `.import_record_id` / `.metadata`; dict shape uses
    `"id"` / `"metadata"`. Single source of truth for orphans() and cold_records().
    """
    meta = getattr(rec, "metadata", None)
    rid = getattr(rec, "import_record_id", None)
    if meta is None and isinstance(rec, dict):
        meta = rec.get("metadata")
        rid = rec.get("id")
    return rid, (meta or {})


def stale_pages(pages: Iterable[Any]) -> list[dict[str, Any]]:
    return [
        {"kind": "stale_page", "slug": p.slug,
         "reason": "flagged stale by ingest; refresh from current sources"}
        for p in pages if getattr(p, "status", None) == "stale"
    ]


def coverage_gaps(pages: Iterable[Any], raw_counts: dict[str, int], *, min_count: int) -> list[dict[str, Any]]:
    pages = list(pages)
    covered = {p.domain for p in pages if p.status == "current" and p.domain}
    out: list[dict[str, Any]] = []
    for domain, count in sorted(raw_counts.items()):
        if domain and count >= min_count and domain not in covered:
            out.append({"kind": "coverage_gap", "domain": domain, "count": count,
                        "reason": f"{count} raw records in domain {domain!r} with no current synthesis"})
    return out


def supersession(pages: Iterable[Any]) -> list[dict[str, Any]]:
    pages = list(pages)
    current = {p.slug for p in pages if p.status == "current"}
    out: list[dict[str, Any]] = []
    for p in pages:
        if p.status != "current":
            continue
        for sup in (getattr(p, "supersedes", None) or []):
            if sup in current:
                out.append({"kind": "supersession", "slug": p.slug, "supersedes": sup,
                            "reason": f"{p.slug!r} supersedes still-current {sup!r}"})
    return out


def orphans(records: Iterable[Any], *, limit: int = 50) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for rec in records:
        rid, meta = _id_and_meta(rec)
        if not meta.get("domain"):
            out.append({"kind": "orphan", "id": rid, "reason": "raw record has no domain (unrouteable)"})
            if len(out) >= limit:
                break
    return out


def cold_records(records: Iterable[Any], stats_by_id: dict[str, dict[str, Any]],
                  *, limit: int = 50) -> list[dict[str, Any]]:
    # No evidence collected yet (stats disabled / log empty) means absence-of-retrieval
    # can't be distinguished from absence-of-data — flagging everything as cold here
    # would be a false archive-candidate flood, so bail out instead.
    if not stats_by_id:
        return []
    out: list[dict[str, Any]] = []
    for rec in records:
        rid, meta = _id_and_meta(rec)
        if not rid:
            continue
        if rid in stats_by_id:
            continue
        out.append({"kind": "cold_record", "id": rid, "domain": meta.get("domain"),
                    "reason": "never retrieved since stats collection began; candidate for archive"})
        if len(out) >= limit:
            break
    return out


def hot_topics_without_synthesis(pages: Iterable[Any], stats_by_topic: dict[str, int],
                                  *, min_retrievals: int) -> list[dict[str, Any]]:
    pages = list(pages)
    covered = {(p.domain, p.topic) for p in pages if p.status == "current" and p.domain and p.topic}
    out: list[dict[str, Any]] = []
    for key, count in stats_by_topic.items():
        parts = key.split("\t", 1)
        if len(parts) != 2:
            continue
        domain, topic = parts
        if not domain or not topic:
            continue
        if count < min_retrievals:
            continue
        if (domain, topic) in covered:
            continue
        out.append({"kind": "hot_topic", "domain": domain, "topic": topic, "count": count,
                    "reason": f"{count} retrievals in topic {topic!r} (domain {domain!r}) "
                              "with no current synthesis; candidate to compile"})
    out.sort(key=lambda item: (-item["count"], item["domain"], item["topic"]))
    return out


def run_all(pages: Iterable[Any], *, raw_counts: dict[str, int], min_count: int,
            records: Iterable[Any] = (), stats: dict[str, Any] | None = None,
            hot_topic_min: int | None = None,
            cold_min_events: int | None = None) -> dict[str, list[dict[str, Any]]]:
    pages = list(pages)
    records = list(records)
    by_id = (stats or {}).get("by_id", {})
    by_topic = (stats or {}).get("by_topic", {})
    events = (stats or {}).get("events", 0)
    # Only trust cold_records once the log has enough events; on sparse stats a
    # not-yet-retrieved record is indistinguishable from a genuinely cold one, so
    # suppress the (destructive) archive proposals entirely below the floor.
    cold_floor = cold_min_events if cold_min_events is not None else COLD_RECORD_MIN_EVENTS
    return {
        "stale_pages": stale_pages(pages),
        "coverage_gaps": coverage_gaps(pages, raw_counts, min_count=min_count),
        "supersession": supersession(pages),
        "orphans": orphans(records),
        "cold_records": cold_records(records, by_id) if events >= cold_floor else [],
        "hot_topics": hot_topics_without_synthesis(
            pages, by_topic,
            min_retrievals=hot_topic_min if hot_topic_min is not None else min_count,
        ),
    }
