"""Content-addressed filesystem blob store for Reliquary.

Self-contained: depends only on the stdlib and a configured directory + signing
key. No Mem0/Qdrant/server imports, so it is unit-testable in isolation.

Blobs are addressed by the sha256 of their content (the id), giving natural
dedup and a stable handle. Layout under ``blob_dir`` (sharded by the first two
hex chars of the id to avoid one huge flat directory):

    <blob_dir>/<ab>/<sha256>.<ext>     raw bytes, ext for human browsability
    <blob_dir>/<ab>/<sha256>.json      sidecar: mimetype, size, ext, ref_count, created
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field

_BLOB_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_BLOB_ID_RE = re.compile(r"^[0-9a-zA-Z_\-]+$")


def _validate_blob_id(blob_id: str) -> None:
    if not _SAFE_BLOB_ID_RE.match(blob_id):
        raise ValueError(f"invalid blob_id: {blob_id!r}")


_STORE_LOCK = threading.Lock()


class BlobTooLarge(Exception):
    """Raised by ``BlobStore.put`` when data exceeds the configured cap."""

    def __init__(self, size: int, max_bytes: int) -> None:
        super().__init__(f"blob is {size} bytes, exceeds max {max_bytes}")
        self.size = size
        self.max_bytes = max_bytes


@dataclass
class BlobInfo:
    id: str
    mimetype: str
    size: int
    ext: str
    ref_count: int
    created: float
    owners: list[str] = field(default_factory=list)


# (signature, mimetype, extension). RIFF/WEBP is special-cased in sniff_mimetype.
_MAGIC: list[tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
    (b"%PDF-", "application/pdf", "pdf"),
]

# Used to pick an extension when the caller supplies a mimetype we couldn't sniff.
_MIME_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "application/pdf": "pdf",
    "image/svg+xml": "svg",
}


def sniff_mimetype(data: bytes) -> tuple[str, str]:
    """Return ``(mimetype, ext)`` from magic bytes.

    Falls back to ``("application/octet-stream", "bin")`` for unrecognised data.
    """
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    for sig, mime, ext in _MAGIC:
        if data.startswith(sig):
            return mime, ext
    return "application/octet-stream", "bin"


class BlobStore:
    def __init__(self, blob_dir: str, signing_key: bytes, max_bytes: int = 0) -> None:
        self.blob_dir = blob_dir
        self.signing_key = signing_key
        self.max_bytes = max_bytes
        os.makedirs(self.blob_dir, exist_ok=True)

    # --- paths ---------------------------------------------------------------
    def _shard_dir(self, blob_id: str) -> str:
        return os.path.join(self.blob_dir, blob_id[:2])

    def _blob_path(self, blob_id: str, ext: str) -> str:
        return os.path.join(self._shard_dir(blob_id), f"{blob_id}.{ext}")

    def _sidecar_path(self, blob_id: str) -> str:
        return os.path.join(self._shard_dir(blob_id), f"{blob_id}.json")

    # --- sidecar -------------------------------------------------------------
    def info(self, blob_id: str) -> BlobInfo | None:
        _validate_blob_id(blob_id)
        try:
            with open(self._sidecar_path(blob_id), "r", encoding="utf-8") as fh:
                return BlobInfo(**json.load(fh))
        except (FileNotFoundError, ValueError, TypeError):
            return None

    def _write_sidecar(self, info: BlobInfo) -> None:
        path = self._sidecar_path(info.id)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(info), fh)
        os.replace(tmp, path)

    # --- store / fetch -------------------------------------------------------
    def put(self, data: bytes, *, mimetype: str | None = None) -> BlobInfo:
        if self.max_bytes and len(data) > self.max_bytes:
            raise BlobTooLarge(len(data), self.max_bytes)

        blob_id = hashlib.sha256(data).hexdigest()

        with _STORE_LOCK:
            existing = self.info(blob_id)
            if existing is not None:
                existing.ref_count += 1
                self._write_sidecar(existing)
                return existing

            sniffed_mime, ext = sniff_mimetype(data)
            if sniffed_mime == "application/octet-stream" and mimetype:
                final_mime = mimetype
                ext = _MIME_EXT.get(mimetype, "bin")
            else:
                final_mime = sniffed_mime

            os.makedirs(self._shard_dir(blob_id), exist_ok=True)
            blob_path = self._blob_path(blob_id, ext)
            tmp = f"{blob_path}.tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, blob_path)

            info = BlobInfo(
                id=blob_id,
                mimetype=final_mime,
                size=len(data),
                ext=ext,
                ref_count=1,
                created=time.time(),
            )
            self._write_sidecar(info)
            return info

    def get(self, blob_id: str) -> tuple[bytes, str] | None:
        _validate_blob_id(blob_id)
        info = self.info(blob_id)
        if info is None:
            return None
        try:
            with open(self._blob_path(blob_id, info.ext), "rb") as fh:
                return fh.read(), info.mimetype
        except FileNotFoundError:
            return None

    def register_owner(self, blob_id: str, memory_id: str) -> None:
        """Record that ``memory_id`` is an authoritative owner of ``blob_id``.

        Idempotent: registering the same owner twice is a no-op. Silently
        returns if the blob is unknown (already deleted).
        """
        _validate_blob_id(blob_id)
        with _STORE_LOCK:
            info = self.info(blob_id)
            if info is None:
                return
            if memory_id not in info.owners:
                info.owners.append(memory_id)
                self._write_sidecar(info)

    def is_owner(self, blob_id: str, memory_id: str) -> bool:
        """Return ``True`` iff ``memory_id`` is a registered owner of ``blob_id``."""
        _validate_blob_id(blob_id)
        info = self.info(blob_id)
        return bool(info and memory_id in info.owners)

    def delete(self, blob_id: str, *, owner: str | None = None) -> bool | None:
        """Decrement the ref count; unlink bytes + sidecar when it reaches zero.

        Returns ``None`` if unknown, ``True`` if the blob was unlinked, ``False``
        if it was only decremented (still referenced).

        If ``owner`` is provided and present in ``info.owners``, it is removed
        before the ref_count logic runs. The ref_count still drives unlinking.
        """
        _validate_blob_id(blob_id)
        with _STORE_LOCK:
            info = self.info(blob_id)
            if info is None:
                return None
            if owner is not None and owner in info.owners:
                info.owners.remove(owner)
            info.ref_count -= 1
            if info.ref_count > 0:
                self._write_sidecar(info)
                return False
            for path in (self._blob_path(blob_id, info.ext), self._sidecar_path(blob_id)):
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
            return True

    # --- signed URLs ---------------------------------------------------------
    def _sig(self, blob_id: str, exp: int) -> str:
        msg = f"{blob_id}|{exp}".encode("utf-8")
        return hmac.new(self.signing_key, msg, hashlib.sha256).hexdigest()

    def sign(self, blob_id: str, ttl_seconds: int) -> tuple[int, str]:
        _validate_blob_id(blob_id)
        exp = int(time.time()) + ttl_seconds
        return exp, self._sig(blob_id, exp)

    def verify(self, blob_id: str, exp: int, sig: str) -> bool:
        _validate_blob_id(blob_id)
        if exp < int(time.time()):
            return False
        return hmac.compare_digest(self._sig(blob_id, exp), sig)
