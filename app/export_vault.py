#!/usr/bin/env python3
"""One-way export of the compiled (synthesis) layer to an Obsidian-style vault.

Writes each page as ``<out>/<domain>/<slug>.md`` (markdown + YAML frontmatter,
exactly the stored revision). One-way only (Reliquary -> vault); it never reads
the vault back. Run:

    python app/export_vault.py --out vault/

Reads MEM0_COMPILED_DIR / MEM0_BLOB_DIR (overridable by flags), like app/lint.py.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from blobs import BlobStore  # noqa: E402
from compiled import PageRegistry, slugify  # noqa: E402


def export_vault(registry: PageRegistry, out_dir: str) -> int:
    """Write every page's current revision into ``out_dir`` as markdown.

    Layout: ``<out_dir>/<domain-or-_>/<slug>.md``. Returns the number of files
    written. Uses an atomic tmp+replace write so a re-export never leaves a
    partially-written file.
    """
    written = 0
    for page in registry.list():
        body = registry.read_body(page.slug)
        if body is None:
            continue
        text, _blob_id = body
        domain_dir = slugify(page.domain or "") or "_"
        dest_dir = os.path.join(out_dir, domain_dir)
        os.makedirs(dest_dir, exist_ok=True)
        path = os.path.join(dest_dir, f"{page.slug}.md")
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
        written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the compiled synthesis layer to a browsable vault (one-way)."
    )
    parser.add_argument("--out", required=True, help="Output vault directory.")
    parser.add_argument("--compiled-dir", default=os.getenv("MEM0_COMPILED_DIR", "/data/compiled"))
    parser.add_argument("--blob-dir", default=os.getenv("MEM0_BLOB_DIR", "/data/blobs"))
    args = parser.parse_args(argv)

    blobs = BlobStore(blob_dir=args.blob_dir, signing_key=b"export", max_bytes=0)
    registry = PageRegistry(registry_dir=args.compiled_dir, blobs=blobs)
    written = export_vault(registry, args.out)
    print(f"Exported {written} page(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
