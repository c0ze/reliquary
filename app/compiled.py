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


def _emit_frontmatter(info: "PageInfo") -> str:
    """Minimal one-way YAML frontmatter for Obsidian/serving. The registry's JSON
    sidecar is the source of truth; this is never parsed back."""
    def scalar(v: object) -> str:
        return "" if v is None else str(v)

    lines = ["---"]
    for key in ("slug", "title", "domain", "hall", "room", "topic", "status", "kind"):
        val = getattr(info, key)
        if val:
            lines.append(f"{key}: {scalar(val)}")
    for key in ("derived_from", "supersedes"):
        vals = getattr(info, key)
        if vals:
            lines.append(f"{key}: [{', '.join(scalar(v) for v in vals)}]")
    lines.append("---")
    return "\n".join(lines)


def assemble_markdown(info: "PageInfo", body: str) -> str:
    return f"{_emit_frontmatter(info)}\n\n{body.strip()}\n"


class PageRegistry:
    def __init__(self, registry_dir: str, blobs: "BlobStore") -> None:
        self.registry_dir = registry_dir
        self.blobs = blobs
        os.makedirs(self.registry_dir, exist_ok=True)

    # --- paths ---
    def _shard_dir(self, slug: str) -> str:
        return os.path.join(self.registry_dir, slug[:2])

    def _path(self, slug: str) -> str:
        return os.path.join(self._shard_dir(slug), f"{slug}.json")

    # --- read ---
    def get(self, slug: str) -> "PageInfo | None":
        # Path-traversal guard: only an already-clean slug maps to a file. Callers
        # pass arbitrary strings here (e.g. mem0_fetch forwards the raw MCP `id`),
        # so reject anything that isn't its own slugify() output before touching the
        # filesystem. This is the read root for read_body/history too.
        if not slug or slugify(slug) != slug:
            return None
        try:
            with open(self._path(slug), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            fields = {k: v for k, v in data.items() if k in PageInfo.__dataclass_fields__}
            return PageInfo(**fields)
        except (FileNotFoundError, ValueError, TypeError):
            return None

    def read_body(self, slug: str) -> "tuple[str, str] | None":
        info = self.get(slug)
        if info is None:
            return None
        result = self.blobs.get(info.current_blob)
        if result is None:
            return None
        data, _mime = result
        return data.decode("utf-8"), info.current_blob

    # --- write ---
    def _save(self, info: "PageInfo") -> None:
        os.makedirs(self._shard_dir(info.slug), exist_ok=True)
        path = self._path(info.slug)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(info), fh)
        os.replace(tmp, path)

    def put_revision(self, slug: str, body: str, frontmatter: dict) -> "PageInfo":
        slug = slugify(slug)
        if not slug:
            raise ValueError("empty slug")
        now = time.time()
        with _REGISTRY_LOCK:
            existing = self.get(slug)
            info = existing or PageInfo(slug=slug, current_blob="", created_at=now)
            for key in ("title", "domain", "hall", "room", "topic", "status", "kind"):
                if frontmatter.get(key) is not None:
                    setattr(info, key, frontmatter[key])
            for key in ("derived_from", "supersedes"):
                if frontmatter.get(key) is not None:
                    setattr(info, key, [str(v) for v in frontmatter[key]])
            info.updated_at = now
            if not info.created_at:
                info.created_at = now
            blob_info = self.blobs.put(assemble_markdown(info, body).encode("utf-8"),
                                       mimetype="text/markdown")
            if existing and existing.current_blob and existing.current_blob != blob_info.id:
                info.history.append(existing.current_blob)
            info.current_blob = blob_info.id
            self._save(info)
            return info

    def _iter_pages(self):
        for shard in os.listdir(self.registry_dir):
            shard_path = os.path.join(self.registry_dir, shard)
            if not os.path.isdir(shard_path):
                continue
            for name in os.listdir(shard_path):
                if name.endswith(".json"):
                    info = self.get(name[:-5])
                    if info is not None:
                        yield info

    def list(self, *, domain: str | None = None, status: str | None = None) -> "list[PageInfo]":
        out = []
        for info in self._iter_pages():
            if domain is not None and info.domain != domain:
                continue
            if status is not None and info.status != status:
                continue
            out.append(info)
        return sorted(out, key=lambda p: p.updated_at, reverse=True)

    def history(self, slug: str) -> "list[str]":
        info = self.get(slug)
        return list(info.history) if info else []

    def set_status(self, slug: str, status: str) -> "PageInfo | None":
        with _REGISTRY_LOCK:
            info = self.get(slug)
            if info is None:
                return None
            info.status = status
            info.updated_at = time.time()
            self._save(info)
            return info

    def set_memory_id(self, slug: str, memory_id: str) -> None:
        with _REGISTRY_LOCK:
            info = self.get(slug)
            if info is None:
                return
            info.memory_id = memory_id
            self._save(info)

    def pages_deriving_from(self, *, ids=(), domain: str | None = None,
                            topic: str | None = None) -> "list[PageInfo]":
        """Pages whose synthesis derives from any of ``ids``, or (when there is no
        id match) whose ``domain`` AND ``topic`` both match. Pass either or both."""
        idset = set(ids)
        out = []
        for info in self._iter_pages():
            if idset and idset.intersection(info.derived_from or ()):
                out.append(info)
            elif domain and topic and info.domain == domain and info.topic == topic:
                out.append(info)
        return out
