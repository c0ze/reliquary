from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from blobs import BlobStore  # noqa: E402
from compiled import PageRegistry  # noqa: E402
from ingest import import_metadata, ingest_records, record_content_hash  # noqa: E402


class RecordingMemory:
    def __init__(self, existing=None):
        self.records = list(existing or [])
        self.added = []
        self.updated = []

    def get_all(self, user_id=None, **kwargs):
        rows = [record for record in self.records if user_id is None or record.get("user_id") == user_id]
        return {"results": rows}

    def add(self, text, *, user_id=None, metadata=None, infer=False, **kwargs):
        new_id = f"new-{len(self.added) + 1}"
        self.added.append({"text": text, "user_id": user_id, "metadata": metadata, "infer": infer})
        self.records.append({"id": new_id, "memory": text, "metadata": metadata or {}, "user_id": user_id})
        return {"results": [{"id": new_id, "event": "ADD"}]}

    def update(self, memory_id, data, metadata=None):
        self.updated.append({"id": memory_id, "data": data, "metadata": metadata})
        for record in self.records:
            if record.get("id") == memory_id:
                record["memory"] = data
                record["metadata"] = metadata or {}
                return {"id": memory_id}
        raise KeyError(memory_id)


def _record(record_id, text, title):
    return {"id": record_id, "text": text, "metadata": {"title": title}}


def test_record_content_hash_ignores_import_bookkeeping():
    base = _record("same", "Text", "Title")
    with_bookkeeping = {
        "id": "same",
        "text": "Text",
        "metadata": {
            "title": "Title",
            "import_record_id": "same",
            "import_content_hash": "old",
            "source_group": "imported",
        },
    }

    assert record_content_hash(base) == record_content_hash(with_bookkeeping)


def test_import_metadata_handles_none_metadata_defensively():
    item = {"id": "missing-meta", "text": "Text", "metadata": None}
    metadata = import_metadata(item)

    assert metadata["import_record_id"] == "missing-meta"
    assert metadata["source_group"] == "imported"
    assert metadata["import_content_hash"] == record_content_hash(item)


def test_incremental_ingest_skips_unchanged_updates_changed_and_adds_new():
    unchanged = _record("unchanged", "Same text", "Same")
    changed_old = _record("changed", "Old text", "Old")
    changed_new = _record("changed", "New text", "New")
    memory = RecordingMemory([
        {
            "id": "mem-unchanged",
            "memory": unchanged["text"],
            "metadata": {
                "title": "Same",
                "import_record_id": "unchanged",
                "import_content_hash": record_content_hash(unchanged),
            },
            "user_id": "default",
        },
        {
            "id": "mem-changed",
            "memory": changed_old["text"],
            "metadata": {
                "title": "Old",
                "import_record_id": "changed",
                "import_content_hash": record_content_hash(changed_old),
            },
            "user_id": "default",
        },
    ])

    summary = ingest_records(
        memory,
        [unchanged, changed_new, _record("new", "New record", "Fresh")],
        user_id="default",
        infer=False,
        incremental=True,
    )

    assert summary == {"selected": 3, "added": 1, "updated": 1, "skipped": 1}
    assert [item["metadata"]["import_record_id"] for item in memory.added] == ["new"]
    assert memory.updated == [
        {
            "id": "mem-changed",
            "data": "New text",
            "metadata": {
                "title": "New",
                "import_record_id": "changed",
                "import_content_hash": record_content_hash(changed_new),
                "source_group": "imported",
            },
        }
    ]


def _page_registry(tmp_path):
    blobs = BlobStore(blob_dir=str(tmp_path / "blobs"), signing_key=b"k", max_bytes=0)
    return PageRegistry(registry_dir=str(tmp_path / "compiled"), blobs=blobs)


def _record_with_tax(record_id, text, *, domain, topic):
    return {"id": record_id, "text": text, "metadata": {"domain": domain, "topic": topic}}


def test_bulk_ingest_flags_matching_page_stale(tmp_path):
    registry = _page_registry(tmp_path)
    registry.put_revision("deities", "deities synthesis",
                          {"domain": "pagan", "topic": "deities", "status": "current"})
    assert registry.get("deities").status == "current"

    memory = RecordingMemory()
    summary = ingest_records(
        memory,
        [_record_with_tax("rec-1", "A new deity fact", domain="pagan", topic="deities")],
        user_id="u",
        page_registry=registry,
    )

    assert summary["added"] == 1
    assert registry.get("deities").status == "stale"


def test_bulk_ingest_flags_page_by_derived_id(tmp_path):
    registry = _page_registry(tmp_path)
    # Page derives from a raw id; re-importing that id should flag it stale even
    # without a taxonomy match.
    registry.put_revision("brigid", "brigid synthesis",
                          {"derived_from": ["rec-7"], "status": "current"})

    memory = RecordingMemory()
    ingest_records(
        memory,
        [{"id": "rec-7", "text": "updated brigid fact", "metadata": {}}],
        user_id="u",
        page_registry=registry,
    )
    assert registry.get("brigid").status == "stale"


def test_bulk_ingest_unrelated_taxonomy_leaves_page_current(tmp_path):
    registry = _page_registry(tmp_path)
    registry.put_revision("deities", "deities synthesis",
                          {"domain": "pagan", "topic": "deities", "status": "current"})

    memory = RecordingMemory()
    ingest_records(
        memory,
        [_record_with_tax("rec-2", "unrelated fact", domain="infra", topic="containers")],
        user_id="u",
        page_registry=registry,
    )
    assert registry.get("deities").status == "current"


def test_bulk_ingest_without_registry_is_unchanged():
    # Passing page_registry=None (the default) must not crash and must behave
    # exactly as before.
    memory = RecordingMemory()
    summary = ingest_records(
        memory,
        [_record_with_tax("rec-3", "a fact", domain="pagan", topic="deities")],
        user_id="u",
        page_registry=None,
    )
    assert summary == {"selected": 1, "added": 1, "updated": 0, "skipped": 0}
