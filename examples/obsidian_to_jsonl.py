#!/usr/bin/env python3
"""Convert an Obsidian vault (a tree of Markdown notes) into the JSONL corpus
format Reliquary ingests: one ``{"id", "text", "metadata"}`` record per note.

This is a *starting point*, not a one-size-fits-all importer — every vault is
organised differently. The interesting decisions are:

* **id** — a stable hash of the note's path. Reliquary's importer matches
  records by ``id`` (incremental by default), so re-running the converter and
  the importer updates changed notes and skips unchanged ones instead of
  duplicating them. Renaming or moving a note changes its id: the old record
  stays until you remove it.
* **text** — what actually gets embedded and returned. We prepend a small header
  (vault, path, title, frontmatter) so the model sees provenance, then the body.
* **metadata.title** — used as the result title.
* **metadata.source_ref** — the on-disk path. Only an ``http(s)://`` value
  becomes the document URL; a file path is kept as provenance.
* **metadata.{domain,hall,room,topic}** — the retrieval *taxonomy*. Reliquary
  routes a query to a narrower pool when it mentions a known value, before
  falling back to global search. Here we derive them from the folder hierarchy;
  adapt ``taxonomy_for`` to your own layout. The importer stores these as slugs
  (case-folded, letters and digits of any script, other runs become ``-``).

Usage:
    python obsidian_to_jsonl.py ~/Documents/obsidian/myvault -o corpus.jsonl
    python obsidian_to_jsonl.py ~/vault --exclude diary/private --exclude .trash

Then ingest (re-running it later updates instead of duplicating):
    python app/ingest.py corpus.jsonl --config config.yaml --user-id default
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
import unicodedata
from pathlib import Path

# Optional: PyYAML gives proper frontmatter parsing. Without it we fall back to
# a minimal key: value reader, which is enough for titles/tags.
try:
    import yaml  # type: ignore
except ImportError:  # pragma: no cover
    yaml = None

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def slugify(value: str) -> str:
    """Same rule as Reliquary's catalog: NFKC, case-folded, letters/marks/digits
    of any script kept, every other run becomes one '-'."""
    value = unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", value).casefold())
    out: list[str] = []
    for char in value:
        if unicodedata.category(char)[0] in {"L", "M", "N"}:
            out.append(char)
        elif out and out[-1] != "-":
            out.append("-")
    return "".join(out).strip("-")


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
        # Stable id from the relative path: re-importing updates this record.
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
    output = args.output.expanduser()
    # Write to a temporary file next to the output and rename it into place, so
    # a failure part-way through never leaves a truncated corpus behind.
    fd, tmp_name = tempfile.mkstemp(dir=output.parent or Path("."), prefix=output.name + ".", suffix=".tmp")
    umask = os.umask(0)
    os.umask(umask)
    os.chmod(tmp_name, 0o666 & ~umask)  # mkstemp creates 0600; keep the usual mode
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            for md_file in sorted(vault.rglob("*.md")):
                if not md_file.is_file():
                    continue
                rel = md_file.relative_to(vault).as_posix().lower()
                if any(rel == ex or rel.startswith(ex + "/") for ex in excludes):
                    continue
                record = build_record(vault, md_file)
                if record.pop("_body_chars") < args.min_chars:
                    continue
                # default=str: YAML frontmatter can hold dates (tags: [2024-01-02]).
                out.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
                count += 1
        os.replace(tmp_name, output)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise

    print(f"Wrote {count} record(s) to {output}")


if __name__ == "__main__":
    main()
