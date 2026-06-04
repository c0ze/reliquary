from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from ingest import ingest_records, record_content_hash  # noqa: E402


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
