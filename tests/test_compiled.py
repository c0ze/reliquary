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
