"""Slug-keyed page registry for Reliquary's compiled (synthesis) layer.

Dependency-light: stdlib + a BlobStore for revision bytes. No Mem0/Qdrant/server
imports, so it is unit-testable in isolation like blobs.py / catalog.py.

A *page* is the mutable unit of the compiled layer: a stable slug whose content is
a sequence of immutable revisions. Each revision's bytes (markdown + YAML
frontmatter) live in the content-addressed BlobStore; the registry holds the
mutable pointer to the current revision plus frontmatter and status. Flagging a
page ``stale`` is a registry write and does NOT mint a new revision.

Layout under ``registry_dir`` (sharded by the first two chars of the slug):

    <registry_dir>/<sl>/<slug>.json     PageInfo as JSON
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blobs import BlobStore

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_REGISTRY_LOCK = threading.Lock()

VALID_STATUSES = ("current", "stale", "draft", "archived")


def slugify(value: str) -> str:
    """Lowercase, collapse runs of non-alphanumerics to single hyphens, trim."""
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


@dataclass
class PageInfo:
    slug: str
    current_blob: str
    title: str = ""
    domain: str | None = None
    hall: str | None = None
    room: str | None = None
    topic: str | None = None
    derived_from: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    status: str = "current"
    kind: str = "synthesis"
    created_at: float = 0.0
    updated_at: float = 0.0
    history: list[str] = field(default_factory=list)
    memory_id: str | None = None


class PageRegistry:
    """Stub — fleshed out in Task 1.2."""
