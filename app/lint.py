#!/usr/bin/env python3
"""Stateless, cron-friendly lint for the compiled (synthesis) layer.

Reports proposals (stale pages, coverage gaps, supersession, orphans, cold
records, hot topics) from app/health.py. PROPOSES only — never rewrites or
auto-applies. Reliquary runs no internal scheduler; invoke this from external cron:

    python app/lint.py [--json] [--strict]

Reads RELIQUARY_COMPILED_DIR, RELIQUARY_BLOB_DIR, RELIQUARY_DATASET_PATH, RELIQUARY_LINT_COVERAGE_MIN,
RELIQUARY_RETRIEVAL_STATS_PATH (overridable by the matching flags).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import health  # noqa: E402
from blobs import BlobStore  # noqa: E402
from compiled import PageRegistry  # noqa: E402
from retrieval_stats import aggregate  # noqa: E402


def build_report(*, compiled_dir: str, blob_dir: str, dataset_path: str | None, min_count: int,
                 stats_path: str | None = None, cold_min_events: int | None = None,
                 hot_topic_min: int | None = None) -> dict:
    stats = aggregate(stats_path)
    try:
        blobs = BlobStore(blob_dir=blob_dir, signing_key=b"lint", max_bytes=0)
        registry = PageRegistry(registry_dir=compiled_dir, blobs=blobs)
        pages = registry.list()
    except OSError:
        # Compiled dirs are missing or unwritable (layer not set up here) — there is
        # nothing to lint, so degrade gracefully instead of crashing under cron.
        pages = []
    raw_counts: dict[str, int] = {}
    records: list = []
    if dataset_path:
        try:
            from catalog import CorpusCatalog
            catalog = CorpusCatalog.from_path(dataset_path)
            raw_counts = {k: v for k, v in catalog.value_counts["domain"].items()}
            records = list(catalog.records_by_id.values())
        except Exception as exc:
            # Surface the failure: a dropped dataset silently produces an
            # incomplete report (and a misleadingly clean --strict result).
            print(f"[lint] dataset load failed for {dataset_path!r}: {exc}", file=sys.stderr)
    return health.run_all(pages, raw_counts=raw_counts, min_count=min_count, records=records,
                          stats=stats, hot_topic_min=hot_topic_min, cold_min_events=cold_min_events)


def format_report(report: dict) -> str:
    lines: list[str] = []
    total = 0
    for category, items in report.items():
        lines.append(f"## {category} ({len(items)})")
        for item in items:
            total += 1
            lines.append(f"  - {item.get('reason', item)}")
    lines.append(f"\n{total} proposal(s). Suggestions only — nothing was changed.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lint the compiled synthesis layer (proposes refreshes; never rewrites).")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any proposals exist (cron/CI alerting).")
    parser.add_argument("--compiled-dir", default=os.getenv("RELIQUARY_COMPILED_DIR", "/data/compiled"))
    parser.add_argument("--blob-dir", default=os.getenv("RELIQUARY_BLOB_DIR", "/data/blobs"))
    parser.add_argument("--dataset-path", default=os.getenv("RELIQUARY_DATASET_PATH"))
    parser.add_argument("--coverage-min", type=int, default=int(os.getenv("RELIQUARY_LINT_COVERAGE_MIN", "8")))
    parser.add_argument("--stats-path", default=os.getenv("RELIQUARY_RETRIEVAL_STATS_PATH"),
                        help="Retrieval-event JSONL written by the server (RELIQUARY_RETRIEVAL_STATS_PATH); "
                             "enables cold-record/hot-topic proposals. Unset = those checks stay empty.")
    parser.add_argument("--cold-min-events", type=int,
                        default=int(os.getenv("RELIQUARY_LINT_COLD_MIN_EVENTS", "200")),
                        help="Minimum total retrieval events before proposing never-retrieved records for "
                             "archive (guards against sparse-data false positives).")
    parser.add_argument("--hot-topic-min", type=int, default=None,
                        help="Min retrievals for a topic with no synthesis to be proposed for compilation "
                             "(default: --coverage-min).")
    args = parser.parse_args(argv)

    report = build_report(compiled_dir=args.compiled_dir, blob_dir=args.blob_dir,
                          dataset_path=args.dataset_path, min_count=args.coverage_min,
                          stats_path=args.stats_path, cold_min_events=args.cold_min_events,
                          hot_topic_min=args.hot_topic_min)
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    total = sum(len(v) for v in report.values())
    return 1 if (args.strict and total) else 0


if __name__ == "__main__":
    raise SystemExit(main())
