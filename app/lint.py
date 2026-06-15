#!/usr/bin/env python3
"""Stateless, cron-friendly lint for the compiled (synthesis) layer.

Reports proposals (stale pages, coverage gaps, supersession, orphans) from
app/health.py. PROPOSES only — never rewrites or auto-applies. Reliquary runs no
internal scheduler; invoke this from external cron:

    python app/lint.py [--json] [--strict]

Reads MEM0_COMPILED_DIR, MEM0_BLOB_DIR, MEM0_DATASET_PATH, MEM0_LINT_COVERAGE_MIN
(overridable by the matching flags).
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


def build_report(*, compiled_dir: str, blob_dir: str, dataset_path: str | None, min_count: int) -> dict:
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
        except Exception:
            pass
    return health.run_all(pages, raw_counts=raw_counts, min_count=min_count, records=records)


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
    parser.add_argument("--compiled-dir", default=os.getenv("MEM0_COMPILED_DIR", "/data/compiled"))
    parser.add_argument("--blob-dir", default=os.getenv("MEM0_BLOB_DIR", "/data/blobs"))
    parser.add_argument("--dataset-path", default=os.getenv("MEM0_DATASET_PATH"))
    parser.add_argument("--coverage-min", type=int, default=int(os.getenv("MEM0_LINT_COVERAGE_MIN", "8")))
    args = parser.parse_args(argv)

    report = build_report(compiled_dir=args.compiled_dir, blob_dir=args.blob_dir,
                          dataset_path=args.dataset_path, min_count=args.coverage_min)
    print(json.dumps(report, indent=2) if args.json else format_report(report))
    total = sum(len(v) for v in report.values())
    return 1 if (args.strict and total) else 0


if __name__ == "__main__":
    raise SystemExit(main())
