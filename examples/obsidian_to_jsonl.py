#!/usr/bin/env python3
"""Convert an Obsidian vault (a tree of Markdown notes) into the JSONL corpus
format Reliquary ingests: one ``{"id", "text", "metadata"}`` record per note.

This is a *starting point*, not a one-size-fits-all importer — every vault is
organised differently. The interesting decisions are:

* **id** — a stable hash of the note's path, so re-running updates the same
  record instead of duplicating it (Reliquary de-dupes by ``id`` too).
* **text** — what actually gets embedded and returned. We prepend a small header
  (vault, path, title, frontmatter) so the model sees provenance, then the body.
* **metadata.title** — used as the result title.
* **metadata.source_ref** — the on-disk path; also used as a document URL.
* **metadata.{domain,hall,room,topic}** — the retrieval *taxonomy*. Reliquary
  routes a query to a narrower pool when it mentions a known value, before
  falling back to global search. Here we derive them from the folder hierarchy;
  adapt ``taxonomy_for`` to your own layout.

Usage:
    python obsidian_to_jsonl.py ~/Documents/obsidian/myvault -o corpus.jsonl
    python obsidian_to_jsonl.py ~/vault --exclude diary/private --exclude .trash

Then ingest:
    python app/ingest.py corpus.jsonl --config config.yaml --user-id me
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path

# Optional: PyYAML gives proper frontmatter parsing. Without it we fall back to
# a minimal key: value reader, which is enough for titles/tags.
try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()


def split_frontmatter(raw: str) -> tuple[dict, str]:
    """Return (frontmatter_dict, body) for a Markdown note."""
    match = FRONTMATTER_RE.match(raw)
    if not match:
        return {}, raw
    block, body = match.group(1), raw[match.end():]
    if yaml is not None:
        try:
            data = yaml.safe_load(block) or {}
            return (data if isinstance(data, dict) else {}), body
        except yaml.YAMLError:
            return {}, body
    # Minimal fallback: top-level "key: value" lines only.
    data = {}
    for line in block.splitlines():
        if ":" in line and not line.startswith((" ", "-")):
            key, _, val = line.partition(":")
            data[key.strip()] = val.strip()
    return data, body


def taxonomy_for(rel_path: Path, title: str) -> dict[str, str]:
    """Map a vault-relative path to domain/hall/room/topic.

    Convention used here (tweak to taste):
      <domain>/<hall>/.../<note>.md
    The first folder is the domain, the second (if any) is the hall, and the
    note's own slug is the room/topic. Notes at the vault root get no domain
    and fall back to global search.
    """
    parts = list(rel_path.parts[:-1])  # drop the filename
    note_slug = slugify(rel_path.stem)
    tax: dict[str, str] = {}
    if parts:
        tax["domain"] = slugify(parts[0])
    if len(parts) >= 2:
        tax["hall"] = slugify(parts[1])
    tax["room"] = note_slug
    tax["topic"] = note_slug
    return tax


def build_record(vault: Path, md_file: Path) -> dict:
    raw = md_file.read_text(encoding="utf-8", errors="replace")
    front, body = split_frontmatter(raw)
    rel = md_file.relative_to(vault)
    title = str(front.get("title") or md_file.stem)

    # The header gives the model provenance; the body is the note content.
    header = [
        "Source: Obsidian note",
        f"Vault: {vault.name}",
        f"Path: {rel.as_posix()}",
        f"Title: {title}",
    ]
    if front:
        header.append("\nFrontmatter:")
        header.append(json.dumps(front, ensure_ascii=False, default=str, indent=2))
    text = "\n".join(header) + "\n\nBody:\n" + body.strip()

    metadata = {
        "source": "obsidian",
        "kind": "note",
        "title": title,
        "source_ref": str(md_file),
        "vault": vault.name,
        "relative_path": rel.as_posix(),
        **taxonomy_for(rel, title),
    }
    if "tags" in front:
        metadata["tags"] = front["tags"]

    return {
        # Stable id from the relative path → re-running updates, never duplicates.
        "id": hashlib.sha1(rel.as_posix().encode("utf-8")).hexdigest()[:16],
        "text": text,
        "metadata": metadata,
        # Transient: the note body length (excludes the provenance header), so
        # --min-chars filters on real content. Popped before writing.
        "_body_chars": len(body.strip()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Convert an Obsidian vault to Reliquary JSONL.")
    ap.add_argument("vault", type=Path, help="Path to the Obsidian vault root.")
    ap.add_argument("-o", "--output", type=Path, default=Path("corpus.jsonl"), help="Output JSONL path.")
    ap.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="SUBPATH",
        help="Vault-relative folder to skip (repeatable). E.g. private diaries, .trash.",
    )
    ap.add_argument("--min-chars", type=int, default=1, help="Skip notes whose body is shorter than this.")
    args = ap.parse_args()

    vault = args.vault.expanduser().resolve()
    if not vault.is_dir():
        raise SystemExit(f"Not a directory: {vault}")
    excludes = [e.strip("/").lower() for e in args.exclude]

    count = 0
    with args.output.open("w", encoding="utf-8") as out:
        for md_file in sorted(vault.rglob("*.md")):
            rel = md_file.relative_to(vault).as_posix().lower()
            if any(rel == ex or rel.startswith(ex + "/") for ex in excludes):
                continue
            record = build_record(vault, md_file)
            if record.pop("_body_chars") < args.min_chars:
                continue
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1

    print(f"Wrote {count} record(s) to {args.output}")


if __name__ == "__main__":
    main()
