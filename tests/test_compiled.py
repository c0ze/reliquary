from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from blobs import BlobStore  # noqa: E402
from compiled import PageInfo, PageRegistry, slugify  # noqa: E402


def test_slugify_normalizes():
    assert slugify("Brigid: Goddess of the Forge!") == "brigid-goddess-of-the-forge"
    assert slugify("  Multiple   Spaces ") == "multiple-spaces"
    assert slugify("already-a-slug") == "already-a-slug"


def test_pageinfo_defaults():
    info = PageInfo(slug="x", current_blob="abc")
    assert info.status == "current"
    assert info.kind == "synthesis"
    assert info.derived_from == [] and info.history == []


def _registry(tmp_path):
    blobs = BlobStore(blob_dir=str(tmp_path / "blobs"), signing_key=b"k", max_bytes=0)
    return PageRegistry(registry_dir=str(tmp_path / "reg"), blobs=blobs)


def test_create_get_read_roundtrip(tmp_path):
    reg = _registry(tmp_path)
    info = reg.put_revision("brigid", "# Brigid\n\nForge goddess.",
                            {"title": "Brigid", "domain": "pagan", "derived_from": ["r1", "r2"]})
    assert info.slug == "brigid"
    assert info.title == "Brigid" and info.domain == "pagan"
    assert info.derived_from == ["r1", "r2"]
    assert info.created_at > 0 and info.updated_at == info.created_at
    got = reg.get("brigid")
    assert got is not None and got.current_blob == info.current_blob
    body, blob_id = reg.read_body("brigid")
    assert "Forge goddess." in body and blob_id == info.current_blob
    assert body.startswith("---\n") and "title: Brigid" in body  # frontmatter prepended


def test_get_unknown_returns_none(tmp_path):
    reg = _registry(tmp_path)
    assert reg.get("missing") is None
    assert reg.read_body("missing") is None
