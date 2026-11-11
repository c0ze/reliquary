"""Rules for metadata that callers supply on write paths.

Some metadata keys are set only by the server (blob ownership, import
bookkeeping, upload slots, the record's source group) and some ``kind`` values
mark records the server creates through dedicated paths. Callers must not be
able to set either: a forged ``blob_ref`` breaks every read of the record, and a
forged ``import_record_id`` makes a user-write pass for an imported record.

Taxonomy values (domain/hall/room/topic) are normalised the same way the
catalog normalises them for routing, so a write with ``domain="Dev"`` is
reachable through the advertised ``domain="dev"``.
"""

from __future__ import annotations

import re
from typing import Any

from catalog import slugify_value

# Keys only the server sets. Server-side paths stamp them after the caller's
# metadata has been cleaned, so their own values still get through.
SYSTEM_METADATA_KEYS = frozenset({
    "blob_ref", "blob_mime", "blob_size",            # image writes / compiled pages
    "import_record_id", "import_content_hash",       # ingest
    "source_group",                                  # every write path
    "upload_id",                                     # commit_image_upload
})
# kind values the server assigns through dedicated tools.
RESERVED_KINDS = frozenset({"image", "synthesis", "correction", "live_chat_turn"})
TAXONOMY_KEYS = ("domain", "hall", "room", "topic")
DEFAULT_KIND = "memory_note"


def normalize_taxonomy_value(value: Any) -> str:
    """Slug form used by the catalog; values with no ASCII letters or digits
    (e.g. Japanese) are kept, stripped and lowercased, instead of vanishing."""
    text = str(value).strip()
    return slugify_value(text) or text.lower()


def taxonomy_match_keys(value: Any) -> frozenset[str]:
    """Keys under which a stored taxonomy value matches another: its slug form
    and the ASCII-only slug that versions before Unicode-aware slugs stored.
    Every historical spelling of one value shares a key ("Work Notes" and
    "work-notes"; "Günlük", "günlük" and "g-nl-k"), so values match when their
    key sets intersect. Distinct non-ASCII values can share an ASCII key
    ("日記 notes", "旅行 notes"); that only errs towards a match."""
    text = str(value or "").strip()
    if not text:
        return frozenset()
    ascii_slug = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return frozenset(key for key in (normalize_taxonomy_value(text), ascii_slug) if key)


def normalize_taxonomy(metadata: dict[str, Any]) -> dict[str, Any]:
    """Normalise taxonomy string values in place; returns ``metadata``."""
    for key in TAXONOMY_KEYS:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            metadata[key] = normalize_taxonomy_value(value)
    return metadata


def is_reserved_kind(value: Any) -> bool:
    return str(value or "").strip().lower() in RESERVED_KINDS


def caller_kind(value: Any) -> str:
    """The ``kind`` argument of add_memory: free text, except reserved values."""
    kind = str(value or "").strip()
    return kind if kind and not is_reserved_kind(kind) else DEFAULT_KIND


def clean_caller_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """A copy of caller-supplied metadata without system-managed keys or a
    reserved ``kind``, with taxonomy values normalised."""
    cleaned = {key: value for key, value in metadata.items() if key not in SYSTEM_METADATA_KEYS}
    if "kind" in cleaned and is_reserved_kind(cleaned["kind"]):
        del cleaned["kind"]
    return normalize_taxonomy(cleaned)


def honours_import_id(metadata: dict[str, Any]) -> bool:
    """Whether a stored record's ``import_record_id`` can be trusted: only
    imports carry one. Imports made before ``source_group`` existed have no
    source group at all; user-writes and compiled pages always have one."""
    return metadata.get("source_group") in (None, "", "imported")
