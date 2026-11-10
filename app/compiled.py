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
from contextlib import contextmanager
from typing import TYPE_CHECKING

from persistence import atomic_write

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

if TYPE_CHECKING:
    from blobs import BlobStore

_REGISTRY_LOCK = threading.Lock()

VALID_STATUSES = ("current", "stale", "draft", "archived")

# Slugs become file names (<slug>.json); keep them well under NAME_MAX (255).
MAX_SLUG_LENGTH = 120


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
    # Per-superseded-revision metadata, parallel to `history` (same order): each
    # entry is {"blob", "ts", "status"} captured when that revision was replaced.
    # Older registries predate this field, so it can be shorter than `history`.
    revisions_meta: list[dict] = field(default_factory=list)
    memory_id: str | None = None


def _emit_frontmatter(info: "PageInfo") -> str:
    """Minimal one-way YAML frontmatter for Obsidian/serving. The registry's JSON
    sidecar is the source of truth; this is never parsed back."""
    lines = ["---"]
    for key in ("slug", "title", "domain", "hall", "room", "topic", "status", "kind"):
        val = getattr(info, key)
        if val:
            # json.dumps yields a double-quoted scalar that is valid YAML and safe
            # for values containing ':', commas, brackets, or newlines (Obsidian-ready).
            lines.append(f"{key}: {json.dumps(str(val), ensure_ascii=False)}")
    for key in ("derived_from", "supersedes"):
        vals = getattr(info, key)
        if vals:
            lines.append(f"{key}: {json.dumps([str(v) for v in vals], ensure_ascii=False)}")
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
        # pass arbitrary strings here (e.g. reliquary_fetch forwards the raw MCP `id`),
        # so reject anything that isn't its own slugify() output before touching the
        # filesystem. This is the read root for read_body/history too.
        if not slug or slugify(slug) != slug:
            return None
        try:
            with open(self._path(slug), "r", encoding="utf-8") as fh:
                data = json.load(fh)
            fields = {k: v for k, v in data.items() if k in PageInfo.__dataclass_fields__}
            return PageInfo(**fields)
        except (OSError, ValueError, TypeError):
            # OSError covers missing files and names the filesystem rejects
            # (ENAMETOOLONG for an over-long id passed in by fetch).
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
        atomic_write(self._path(info.slug), json.dumps(asdict(info)).encode("utf-8"))

    @contextmanager
    def _locked(self):
        """Serialize registry read-modify-write cycles across threads and, via
        an advisory lock file, across processes (the ingest CLI marks pages
        stale while the server may be filing them)."""
        with _REGISTRY_LOCK:
            if fcntl is None:
                yield
                return
            fd = os.open(os.path.join(self.registry_dir, ".lock"), os.O_RDWR | os.O_CREAT, 0o666)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)

    def put_revision(self, slug: str, body: str, frontmatter: dict) -> "PageInfo":
        slug = slugify(slug)
        if not slug:
            raise ValueError("empty slug")
        now = time.time()
        with self._locked():
            existing = self.get(slug)
            if existing is None and len(slug) > MAX_SLUG_LENGTH:
                raise ValueError(f"slug longer than {MAX_SLUG_LENGTH} characters")
            # Keep the superseded revision intact while constructing its successor.
            info = PageInfo(**asdict(existing)) if existing else PageInfo(slug=slug, current_blob="", created_at=now)
            for key in ("title", "domain", "hall", "room", "topic", "status", "kind"):
                if frontmatter.get(key) is not None:
                    if key == "status" and frontmatter[key] not in VALID_STATUSES:
                        raise ValueError(f"invalid status: {frontmatter[key]!r}")
                    setattr(info, key, frontmatter[key])
            for key in ("derived_from", "supersedes"):
                vals = frontmatter.get(key)
                if vals is not None:
                    # A bare string would otherwise be split into characters.
                    if isinstance(vals, (str, bytes)):
                        vals = [vals]
                    elif not isinstance(vals, (list, tuple, set)):
                        raise ValueError(f"{key} must be a sequence of ids")
                    setattr(info, key, [str(v) for v in vals])
            info.updated_at = now
            if not info.created_at:
                info.created_at = now
            blob_info = self.blobs.put(assemble_markdown(info, body).encode("utf-8"),
                                       mimetype="text/markdown")
            if existing and existing.current_blob == blob_info.id:
                # Identical re-file: put() bumped the ref for a blob this page
                # already counts once; release the extra ref so delete() stays
                # balanced and the blob can actually be reclaimed later.
                self.blobs.delete(blob_info.id)
            if existing and existing.current_blob and existing.current_blob != blob_info.id:
                info.history.append(existing.current_blob)
                # Capture the superseded revision's timestamp + status so
                # page_history can report when/how each revision changed.
                info.revisions_meta.append({
                    "blob": existing.current_blob,
                    "ts": existing.updated_at,
                    "status": existing.status,
                })
            info.current_blob = blob_info.id
            try:
                self._save(info)
            except BaseException:
                # Don't leak the reference put() took for a revision that was
                # never recorded (an identical re-file already released it).
                if not (existing and existing.current_blob == blob_info.id):
                    self.blobs.delete(blob_info.id)
                raise
            return info

    def rollback_revision(self, slug: str, previous: "PageInfo | None", new_blob: str) -> None:
        """Undo a put_revision whose index write failed: restore the previous
        registry entry (or remove a page that did not exist before) and release
        the reference put_revision took on ``new_blob``."""
        with self._locked():
            if previous is None:
                try:
                    os.remove(self._path(slug))
                except FileNotFoundError:
                    pass
            else:
                self._save(previous)
            # An identical re-file already balanced its own reference.
            if new_blob and (previous is None or previous.current_blob != new_blob):
                self.blobs.delete(new_blob)

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

    def history_detailed(self, slug: str) -> "list[dict] | None":
        """Per-revision rows, newest first: the current revision followed by the
        superseded ones. Each row is {blob, ts, status, current}. Revisions filed
        before revision metadata existed report ts=None/status=None."""
        info = self.get(slug)
        if info is None:
            return None
        rows = [{"blob": info.current_blob, "ts": info.updated_at,
                 "status": info.status, "current": True}]
        # Metadata covers the newest suffix of history for legacy registries.
        # Match by occurrence: reverting to old bytes repeats a blob id.
        missing = len(info.history) - len(info.revisions_meta)
        for index in reversed(range(len(info.history))):
            blob = info.history[index]
            m = info.revisions_meta[index - missing] if index >= missing else {}
            rows.append({"blob": blob, "ts": m.get("ts"),
                         "status": m.get("status"), "current": False})
        return rows

    def delete(self, slug: str) -> "PageInfo | None":
        """Hard-delete a page: unlink its current + historical revision blobs
        (ref-counted) and remove the registry entry. Returns the deleted PageInfo
        (so the caller can clean up the indexed memory) or None if absent."""
        with self._locked():
            info = self.get(slug)
            if info is None:
                return None
            for blob in [info.current_blob, *info.history]:
                if blob:
                    try:
                        self.blobs.delete(blob)
                    except Exception:
                        pass  # best-effort; a missing/shared blob must not block delete
            try:
                os.remove(self._path(slug))
            except FileNotFoundError:
                pass
            return info

    def set_status(self, slug: str, status: str) -> "PageInfo | None":
        with self._locked():
            if status not in VALID_STATUSES:
                raise ValueError(f"invalid status: {status!r}")
            info = self.get(slug)
            if info is None:
                return None
            info.status = status
            info.updated_at = time.time()
            self._save(info)
            return info

    def set_memory_id(self, slug: str, memory_id: str) -> None:
        with self._locked():
            info = self.get(slug)
            if info is None:
                return
            info.memory_id = memory_id
            self._save(info)

    def pages_deriving_from(self, *, ids=(), domain: str | None = None,
                            topic: str | None = None) -> "list[PageInfo]":
        """Pages whose synthesis derives from any of ``ids``. Pages that declare
        no ``derived_from`` at all fall back to a ``domain`` AND ``topic`` match;
        a page that declares its sources is judged on them alone (#69)."""
        idset = set(ids)
        out = []
        for info in self._iter_pages():
            if idset and idset.intersection(info.derived_from or ()):
                out.append(info)
            elif not info.derived_from and domain and topic \
                    and info.domain == domain and info.topic == topic:
                out.append(info)
        return out
