"""Unit tests for the filesystem blob store."""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import pytest  # noqa: E402

from blobs import BlobStore, BlobTooLarge, sniff_mimetype  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16


def make_store(tmp_path, **kw) -> BlobStore:
    return BlobStore(blob_dir=str(tmp_path), signing_key=b"test-key", **kw)


def test_sniff_known_and_unknown():
    assert sniff_mimetype(PNG) == ("image/png", "png")
    assert sniff_mimetype(JPG) == ("image/jpeg", "jpg")
    assert sniff_mimetype(WEBP) == ("image/webp", "webp")
    assert sniff_mimetype(b"not an image") == ("application/octet-stream", "bin")


def test_put_get_roundtrip(tmp_path):
    store = make_store(tmp_path)
    info = store.put(PNG)
    assert info.id == hashlib.sha256(PNG).hexdigest()
    assert info.mimetype == "image/png"
    assert info.ext == "png"
    assert info.size == len(PNG)
    assert info.ref_count == 1
    got = store.get(info.id)
    assert got == (PNG, "image/png")


def test_put_uses_caller_mimetype_only_as_fallback(tmp_path):
    store = make_store(tmp_path)
    # Recognisable bytes: caller hint is ignored in favour of the sniff.
    assert store.put(PNG, mimetype="application/x-lie").mimetype == "image/png"
    # Unrecognisable bytes: caller hint is used.
    info = store.put(b"opaque-bytes", mimetype="image/svg+xml")
    assert info.mimetype == "image/svg+xml"


def test_dedup_increments_ref_count(tmp_path):
    store = make_store(tmp_path)
    first = store.put(PNG)
    second = store.put(PNG)
    assert first.id == second.id
    assert second.ref_count == 2
    # Only one set of files on disk.
    assert len(list(Path(tmp_path).rglob("*.png"))) == 1


def test_delete_decrements_then_unlinks(tmp_path):
    store = make_store(tmp_path)
    store.put(PNG)
    store.put(PNG)  # ref_count == 2
    assert store.delete("missing") is None
    blob_id = store.put(PNG).id  # ref_count == 3
    assert store.delete(blob_id) is False  # -> 2, still present
    assert store.delete(blob_id) is False  # -> 1, still present
    assert store.get(blob_id) is not None
    assert store.delete(blob_id) is True   # -> 0, unlinked
    assert store.get(blob_id) is None
    assert list(Path(tmp_path).rglob("*.png")) == []


def test_sign_verify_roundtrip(tmp_path):
    store = make_store(tmp_path)
    exp, sig = store.sign("abc123", ttl_seconds=60)
    assert store.verify("abc123", exp, sig) is True
    assert store.verify("abc123", exp, "tampered") is False
    assert store.verify("other-id", exp, sig) is False


def test_verify_rejects_expired(tmp_path):
    store = make_store(tmp_path)
    past = int(time.time()) - 10
    sig = store._sig("abc123", past)
    assert store.verify("abc123", past, sig) is False


def test_size_cap(tmp_path):
    store = make_store(tmp_path, max_bytes=4)
    with pytest.raises(BlobTooLarge):
        store.put(b"too many bytes")
    # 0 disables the cap.
    nolimit = make_store(tmp_path, max_bytes=0)
    assert nolimit.put(b"too many bytes").size == 14
