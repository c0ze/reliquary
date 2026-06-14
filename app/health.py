"""Pure health-check functions for the compiled (synthesis) layer.

Dependency-light: stdlib + plain data (PageInfo lists, count dicts, record
iterables). Shared by the mem0://needs-review resource and the lint CLI. Every
check is read-only and only *proposes* — a human decides; nothing auto-applies.
"""

from __future__ import annotations

from typing import Any, Iterable


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
        meta = getattr(rec, "metadata", None)
        rid = getattr(rec, "import_record_id", None)
        if meta is None and isinstance(rec, dict):
            meta = rec.get("metadata")
            rid = rec.get("id")
        meta = meta or {}
        if not meta.get("domain"):
            out.append({"kind": "orphan", "id": rid, "reason": "raw record has no domain (unrouteable)"})
            if len(out) >= limit:
                break
    return out


def run_all(pages: Iterable[Any], *, raw_counts: dict[str, int], min_count: int,
            records: Iterable[Any] = ()) -> dict[str, list[dict[str, Any]]]:
    pages = list(pages)
    return {
        "stale_pages": stale_pages(pages),
        "coverage_gaps": coverage_gaps(pages, raw_counts, min_count=min_count),
        "supersession": supersession(pages),
        "orphans": orphans(records),
    }
