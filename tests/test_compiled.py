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
    assert body.startswith("---\n") and 'title: "Brigid"' in body  # YAML-safe quoted frontmatter


def test_get_unknown_returns_none(tmp_path):
    reg = _registry(tmp_path)
    assert reg.get("missing") is None
    assert reg.read_body("missing") is None


def test_get_rejects_path_traversal(tmp_path):
    reg = _registry(tmp_path)
    reg.put_revision("real", "body", {"title": "Real"})
    # Traversal / absolute / non-slug ids must never map to a filesystem read.
    for bad in ("../real", "/etc/passwd", "a/../../b", "..", "Real"):
        assert reg.get(bad) is None
        assert reg.read_body(bad) is None
    assert reg.get("real") is not None  # the genuine slug still resolves


def test_put_revision_normalizes_scalar_list_fields(tmp_path):
    reg = _registry(tmp_path)
    info = reg.put_revision("p", "body", {"derived_from": "single-id"})
    assert info.derived_from == ["single-id"]  # not ['s', 'i', 'n', ...]


def test_status_validation_rejects_invalid(tmp_path):
    import pytest
    reg = _registry(tmp_path)
    with pytest.raises(ValueError):
        reg.put_revision("p", "body", {"status": "bogus"})
    reg.put_revision("p", "body", {"status": "current"})
    with pytest.raises(ValueError):
        reg.set_status("p", "nonsense")


def test_frontmatter_is_yaml_safe_for_special_chars(tmp_path):
    reg = _registry(tmp_path)
    reg.put_revision("p", "body", {"title": "Brigid: Goddess, of [Fire]"})
    text, _ = reg.read_body("p")
    # Colon/comma/brackets stay inside a quoted scalar (json.dumps) rather than
    # turning the line into an accidental YAML mapping/sequence.
    assert 'title: "Brigid: Goddess, of [Fire]"' in text


def test_update_creates_revision_and_history(tmp_path, monkeypatch):
    reg = _registry(tmp_path)
    import compiled
    t = [1000.0]
    monkeypatch.setattr(compiled.time, "time", lambda: t[0])
    v1 = reg.put_revision("brigid", "v1 body", {"title": "Brigid"})
    t[0] = 2000.0
    v2 = reg.put_revision("brigid", "v2 body", {"title": "Brigid"})
    assert v2.current_blob != v1.current_blob
    assert v1.current_blob in v2.history
    body, _ = reg.read_body("brigid")
    assert "v2 body" in body and "v1 body" not in body


def test_identical_refile_is_noop_revision(tmp_path):
    reg = _registry(tmp_path)
    a = reg.put_revision("p", "same", {"title": "P"})
    b = reg.put_revision("p", "same", {"title": "P"})
    assert a.current_blob == b.current_blob
    assert b.history == []  # identical content => no new revision, even at a later time


def test_list_history_status_provenance(tmp_path):
    reg = _registry(tmp_path)
    reg.put_revision("brigid", "b", {"domain": "pagan", "topic": "deities", "derived_from": ["r1"]})
    reg.put_revision("morrigan", "m", {"domain": "pagan", "topic": "deities", "derived_from": ["r2"]})
    reg.put_revision("docker", "d", {"domain": "infra", "topic": "containers", "derived_from": ["r3"]})

    assert {p.slug for p in reg.list(domain="pagan")} == {"brigid", "morrigan"}

    reg.set_status("brigid", "stale")
    assert reg.get("brigid").status == "stale"
    assert {p.slug for p in reg.list(status="stale")} == {"brigid"}
    assert reg.get("brigid").history == []  # status flag did NOT mint a revision

    reg.set_memory_id("brigid", "mem-99")
    assert reg.get("brigid").memory_id == "mem-99"

    by_id = {p.slug for p in reg.pages_deriving_from(ids=["r2"])}
    assert by_id == {"morrigan"}
    by_tax = {p.slug for p in reg.pages_deriving_from(domain="pagan", topic="deities")}
    assert by_tax == {"brigid", "morrigan"}
