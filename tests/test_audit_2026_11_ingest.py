"""Regression tests for the 2026-11 audit: corpus import (app/ingest.py),
catalog parsing and taxonomy slugs (app/catalog.py), source dates in the
recency tiebreaker, and the example Obsidian converter."""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import string
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from blobs import BlobStore  # noqa: E402
from catalog import CorpusCatalog, slugify_value  # noqa: E402
from compiled import PageRegistry  # noqa: E402
from ingest import (  # noqa: E402
    build_parser,
    import_metadata,
    ingest_records,
    load_config,
    read_jsonl,
    record_content_hash,
)
from retrieval_quality import _hit_timestamp  # noqa: E402


class Mem0LikeMemory:
    """In-memory stand-in that behaves like Mem0 2.x where it matters here:
    reads take entity ids in `filters`, get_all defaults to 20 rows, metadata
    filters are exact matches, and add/update rewrite the top-level dates."""

    def __init__(self, now: str = "2026-11-11T00:00:00+00:00") -> None:
        self.rows: dict[str, dict] = {}
        self.now = now
        self.adds = 0
        self.updates = 0

    def add(self, text, *, user_id=None, metadata=None, infer=True, **kwargs):
        assert isinstance(text, str) and text
        self.adds += 1
        memory_id = f"m{self.adds}"
        metadata = dict(metadata or {})
        created = metadata.pop("created_at", None) or self.now
        metadata.pop("updated_at", None)
        self.rows[memory_id] = {"id": memory_id, "memory": text, "user_id": user_id, "metadata": metadata,
                                "created_at": created, "updated_at": created}
        return {"results": [{"id": memory_id, "event": "ADD"}]}

    def update(self, memory_id, data, metadata=None):
        self.updates += 1
        row = self.rows[memory_id]
        metadata = dict(metadata or {})
        metadata.pop("created_at", None)
        metadata.pop("updated_at", None)
        row.update(memory=data, metadata=metadata, updated_at=self.now)
        return {"message": "Memory updated successfully!"}

    def _matches(self, row, filters):
        for key, value in filters.items():
            if key == "user_id":
                if row["user_id"] != value:
                    return False
            elif key == "AND":
                if not all(self._matches(row, part) for part in value):
                    return False
            elif row["metadata"].get(key) != value:
                return False
        return True

    def get_all(self, *, filters=None, top_k=20, **kwargs):
        if kwargs:
            raise ValueError(f"top-level entity parameters are not supported: {sorted(kwargs)}")
        rows = [row for row in self.rows.values() if self._matches(row, filters or {})]
        return {"results": rows[:top_k]}

    def search(self, query, *, top_k=20, filters=None, threshold=0.1, **kwargs):
        return self.get_all(filters=filters, top_k=top_k)


CORPUS = [
    {"id": "a", "text": "alpha note about the nas", "metadata": {"title": "A"}},
    {"id": "b", "text": "beta note about the nas", "metadata": {"title": "B", "domain": "infra"}},
    {"id": "c", "text": "gamma note", "metadata": {"title": "C"}},
]


# --- Finding 1: re-running the documented ingest command duplicates the corpus


def test_ingest_cli_defaults_to_incremental():
    assert build_parser().parse_args(["corpus.jsonl"]).incremental is True
    assert build_parser().parse_args(["corpus.jsonl", "--incremental"]).incremental is True
    assert build_parser().parse_args(["corpus.jsonl", "--no-incremental"]).incremental is False


def test_rerunning_ingest_with_default_options_does_not_duplicate():
    memory = Mem0LikeMemory()
    ingest_records(memory, CORPUS, user_id="default")
    summary = ingest_records(memory, CORPUS, user_id="default")
    assert len(memory.rows) == 3
    assert summary == {"selected": 3, "added": 0, "updated": 0, "skipped": 3}


def test_ingest_user_id_defaults_from_environment(monkeypatch):
    monkeypatch.setenv("RELIQUARY_USER_ID", "household")
    assert build_parser().parse_args(["corpus.jsonl"]).user_id == "household"
    monkeypatch.setenv("RELIQUARY_USER_ID", "")
    assert build_parser().parse_args(["corpus.jsonl"]).user_id == "default"
    monkeypatch.delenv("RELIQUARY_USER_ID")
    assert build_parser().parse_args(["corpus.jsonl", "--user-id", "x"]).user_id == "x"


# --- Finding 6: Mem0 overwrites the corpus dates; recency used import time


def _ts(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


def test_import_metadata_keeps_source_dates_under_namespaced_keys():
    item = {"id": "n", "text": "t", "metadata": {"created_at": "2019-01-02", "updated_at": "2020-03-04"}}
    metadata = import_metadata(item)
    assert metadata["source_created_at"] == "2019-01-02"
    assert metadata["source_updated_at"] == "2020-03-04"
    only_timestamp = import_metadata({"id": "n", "text": "t", "metadata": {"timestamp": "2018-05-05"}})
    assert only_timestamp["source_created_at"] == "2018-05-05"
    assert "source_updated_at" not in only_timestamp
    explicit = import_metadata({"id": "n", "text": "t",
                                "metadata": {"updated_at": "2020-01-01", "source_updated_at": "2001-01-01"}})
    assert explicit["source_updated_at"] == "2001-01-01"


def test_recency_uses_corpus_dates_not_mem0_import_time():
    memory = Mem0LikeMemory(now="2026-11-11T00:00:00+00:00")
    ingest_records(memory, [{"id": "old", "text": "ancient note",
                             "metadata": {"timestamp": "2019-05-05"}}], user_id="default")
    hit = memory.search("ancient", filters={"user_id": "default"})["results"][0]
    assert hit["updated_at"] == "2026-11-11T00:00:00+00:00"  # what Mem0 reports
    assert _hit_timestamp(hit) == _ts("2019-05-05")

    ingest_records(memory, [{"id": "u", "text": "edited note",
                             "metadata": {"created_at": "2020-01-01", "updated_at": "2024-06-01"}}],
                   user_id="default")
    hit = next(row for row in memory.rows.values() if row["memory"] == "edited note")
    assert hit["updated_at"] == hit["created_at"] == "2020-01-01"  # Mem0 forced updated = created
    assert _hit_timestamp(hit) == _ts("2024-06-01")


def test_hit_timestamp_unchanged_for_records_without_source_dates():
    hit = {"updated_at": "2025-01-01T00:00:00+00:00", "metadata": {"date": "2010-01-01"}}
    assert _hit_timestamp(hit) == _ts("2025-01-01")


def _old_content_hash(item):
    clean = {k: v for k, v in (item.get("metadata") or {}).items()
             if k not in {"import_record_id", "import_content_hash", "source_group"}}
    payload = {"text": item.get("text") or "", "metadata": clean}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def test_content_hash_is_unchanged_by_derived_fields():
    item = {"id": "x", "text": "body", "metadata": {"domain": "Work Notes", "topic": "Budget 2026",
                                                   "updated_at": "2020-01-01"}}
    assert record_content_hash(item) == _old_content_hash(item)
    assert import_metadata(item)["import_content_hash"] == _old_content_hash(item)


def _stored_before_fix(memory, item, *, user_id="default"):
    """A record as the e326072 importer stored it: raw metadata, no derived keys."""
    metadata = dict(item["metadata"])
    metadata.update(import_record_id=item["id"], import_content_hash=_old_content_hash(item),
                    source_group="imported")
    memory.add(item["text"], user_id=user_id, metadata=metadata, infer=False)


def test_existing_imports_get_derived_fields_once_without_marking_pages_stale(tmp_path):
    plain = {"id": "plain", "text": "no dates, slug taxonomy", "metadata": {"domain": "infra", "topic": "nas"}}
    dated = {"id": "dated", "text": "dated note", "metadata": {"domain": "infra", "topic": "nas",
                                                               "updated_at": "2019-01-01"}}
    raw_tax = {"id": "raw", "text": "raw taxonomy", "metadata": {"domain": "Work Notes", "topic": "budget"}}
    memory = Mem0LikeMemory()
    for item in (plain, dated, raw_tax):
        _stored_before_fix(memory, item)
    registry = PageRegistry(registry_dir=str(tmp_path / "compiled"),
                            blobs=BlobStore(blob_dir=str(tmp_path / "blobs"), signing_key=b"k", max_bytes=0))
    registry.put_revision("nas", "synthesis", {"domain": "infra", "topic": "nas", "status": "current",
                                               "derived_from": ["dated"]})

    first = ingest_records(memory, [plain, dated, raw_tax], user_id="default", page_registry=registry)
    assert first == {"selected": 3, "added": 0, "updated": 2, "skipped": 1}
    assert registry.get("nas").status == "current"
    by_id = {row["metadata"]["import_record_id"]: row for row in memory.rows.values()}
    assert by_id["dated"]["metadata"]["source_updated_at"] == "2019-01-01"
    assert by_id["raw"]["metadata"]["domain"] == "work-notes"

    second = ingest_records(memory, [plain, dated, raw_tax], user_id="default")
    assert second == {"selected": 3, "added": 0, "updated": 0, "skipped": 3}
    assert len(memory.rows) == 3


# --- Finding 7: taxonomy slugs


def _old_slugify(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()


EXISTING_VALUES = [
    "pagan", "deities", "infra", "work-notes", "chat-card", "text-export", "notebooklm",
    "Work Notes", "Budget 2026", "ARDA-RELIQUARY-IMAGE-20260605-01", "my_project", "  spaced  out ",
    "a--b", "-lead-trail-", "C++ / Rust", "v1.2.3", "", "---", "MixedCase42", "tabs\tand\nlines",
]


def test_slugify_is_byte_identical_for_ascii_input():
    rng = random.Random(20261111)
    alphabet = string.ascii_letters + string.digits + string.punctuation + " \t"
    sample = list(EXISTING_VALUES)
    sample += ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24))) for _ in range(5000)]
    for value in sample:
        assert slugify_value(value) == _old_slugify(value), value
        assert slugify_value(value).encode() == _old_slugify(value).encode()


@pytest.mark.parametrize("raw, slug", [
    ("günlük", "günlük"),
    ("Günlük Notlar", "günlük-notlar"),
    ("日記", "日記"),
    ("ＡＢＣ　１２", "abc-12"),  # NFKC folds full-width forms
    ("Straße", "strasse"),
    ("हिन्दी नोट्स", "हिन्दी-नोट्स"),  # combining vowel signs stay inside the word
])
def test_slugify_keeps_letters_and_digits_of_any_script(raw, slug):
    assert slugify_value(raw) == slug
    assert slugify_value(slug) == slug


def test_import_metadata_slugs_taxonomy_like_the_catalog(tmp_path):
    corpus = [
        {"id": "w1", "text": "meeting minutes about budget",
         "metadata": {"domain": "Work Notes", "topic": "Budget 2026"}},
        {"id": "w2", "text": "grocery list", "metadata": {"domain": "home", "topic": "shopping"}},
        {"id": "w3", "text": "sunucu yedekleme", "metadata": {"domain": "günlük", "topic": "yedek"}},
    ]
    path = tmp_path / "c.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in corpus), encoding="utf-8")
    memory = Mem0LikeMemory()
    ingest_records(memory, read_jsonl(path), user_id="default")
    catalog = CorpusCatalog.from_path(str(path))
    assert catalog.routeable_domains == ["günlük", "home", "work-notes"]

    for query, expected in (("work notes budget 2026", "meeting minutes about budget"),
                            ("günlük yedek", "sunucu yedekleme")):
        routes = [route for route in catalog.build_routes(query) if route.filters]
        assert routes, query
        for route in routes:
            hits = memory.search(query, filters={**route.filters, "user_id": "default"})["results"]
            assert [hit["memory"] for hit in hits] == [expected], route.description


# --- Finding 8: encodings


def test_catalog_survives_bom_and_invalid_utf8(tmp_path):
    lines = [json.dumps({"id": f"r{i}", "text": f"text {i}", "metadata": {"title": f"T{i}"}}) for i in range(3)]
    path = tmp_path / "corpus.jsonl"
    raw = b"\xef\xbb\xbf" + lines[0].encode() + b"\n" + lines[1].encode()[:-2] + b"\xff\"}\n" + lines[2].encode()
    path.write_bytes(raw)
    catalog = CorpusCatalog.from_path(str(path))
    assert "r0" in catalog.records_by_id  # the BOM line is kept
    assert "r2" in catalog.records_by_id  # the bad byte costs at most its own record

    in_string = tmp_path / "in_string.jsonl"
    in_string.write_bytes(b'{"id": "s", "text": "caf\xe9", "metadata": {}}\n')
    assert CorpusCatalog.from_path(str(in_string)).records_by_id["s"].text == "caf�"


def test_read_jsonl_accepts_a_byte_order_mark(tmp_path):
    path = tmp_path / "bom.jsonl"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps(CORPUS[0]).encode() + b"\n")
    assert read_jsonl(path) == [CORPUS[0]]


# --- Finding 9: read_jsonl validates types before any write


@pytest.mark.parametrize("record, reason", [
    ({"id": "x", "text": {"a": 1}, "metadata": {}}, "text must be a non-empty string"),
    ({"id": "x", "text": [], "metadata": {}}, "text must be a non-empty string"),
    ({"id": "x", "text": None, "metadata": {}}, "text must be a non-empty string"),
    ({"id": "x", "text": "   ", "metadata": {}}, "text must be a non-empty string"),
    ({"id": ["x"], "text": "t", "metadata": {}}, "id must be a non-empty string"),
    ({"id": 1, "text": "t", "metadata": {}}, "id must be a non-empty string"),
    ({"id": "", "text": "t", "metadata": {}}, "id must be a non-empty string"),
    ({"id": "x", "text": "t", "metadata": None}, "metadata must be an object"),
    ({"id": "x", "text": "t"}, "required keys are id, text, metadata"),
    (["not", "an", "object"], "expected a JSON object"),
])
def test_read_jsonl_rejects_bad_types_with_line_number(tmp_path, record, reason):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(CORPUS[0]) + "\n\n" + json.dumps(record) + "\n", encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        read_jsonl(path)
    assert str(excinfo.value) == f"Invalid record in {path} at line 3: {reason}"


def test_read_jsonl_reports_invalid_json_line(tmp_path):
    path = tmp_path / "broken.jsonl"
    path.write_text(json.dumps(CORPUS[0]) + "\n{not json\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="Invalid JSON in .* at line 2"):
        read_jsonl(path)


def test_read_jsonl_returns_records_in_order(tmp_path):
    path = tmp_path / "ok.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in CORPUS) + "\n\n", encoding="utf-8")
    assert read_jsonl(path) == CORPUS


def test_ingest_records_validates_every_record_before_writing():
    memory = Mem0LikeMemory()
    with pytest.raises(ValueError, match="record #2: text must be a non-empty string"):
        ingest_records(memory, [CORPUS[0], {"id": "bad", "text": [], "metadata": {}}, CORPUS[1]],
                       user_id="default")
    assert memory.rows == {}


# --- Finding 10: catalog duplicates and null ids


def test_catalog_counts_a_duplicate_id_once_and_last_wins(tmp_path):
    path = tmp_path / "dup.jsonl"
    rows = [
        {"id": "same", "text": "old", "metadata": {"domain": "old-domain"}},
        {"id": "same", "text": "new", "metadata": {"domain": "new-domain"}},
        {"id": "other", "text": "x", "metadata": {"domain": "new-domain"}},
    ]
    path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    catalog = CorpusCatalog.from_path(str(path))
    assert catalog.records_by_id["same"].text == "new"
    assert dict(catalog.value_counts["domain"]) == {"new-domain": 2}
    assert catalog.routeable_domains == ["new-domain"]


@pytest.mark.parametrize("bad_id", [None, "", True, ["x"], {"a": 1}])
def test_catalog_skips_records_without_a_usable_id(tmp_path, bad_id):
    path = tmp_path / "ids.jsonl"
    path.write_text(json.dumps({"id": bad_id, "text": "t", "metadata": {}}) + "\n"
                    + json.dumps({"id": "ok", "text": "t", "metadata": {}}) + "\n", encoding="utf-8")
    catalog = CorpusCatalog.from_path(str(path))
    assert list(catalog.records_by_id) == ["ok"]
    assert "None" not in catalog.records_by_id


# --- Finding 13: load_config


def test_load_config_expands_env_vars_and_home(tmp_path, monkeypatch):
    monkeypatch.setenv("RELIQUARY_TEST_QDRANT", "qdrant.internal")
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text(
        "vector_store:\n"
        "  provider: qdrant\n"
        "  config:\n"
        "    host: ${RELIQUARY_TEST_QDRANT}\n"
        "    port: 6333\n"
        "    path: ~/qdrant\n"
        "    tags: [$RELIQUARY_TEST_QDRANT]\n"
        "embedder:\n"
        "  provider: ollama\n"
        "  config: {model: nomic-embed-text}\n",
        encoding="utf-8",
    )
    config = load_config(str(path))
    vector = config["vector_store"]["config"]
    assert vector["host"] == "qdrant.internal"
    assert vector["port"] == 6333  # non-strings are untouched
    assert vector["path"] == str(tmp_path / "qdrant")
    assert vector["tags"] == ["qdrant.internal"]
    assert config["embedder"] == {"provider": "ollama", "config": {"model": "nomic-embed-text"}}


def test_load_config_reads_json_and_synthesises_an_embedder_from_env(tmp_path, monkeypatch):
    for name in ("MODEL", "BASE_URL", "API_KEY"):
        monkeypatch.delenv(f"RELIQUARY_EMBEDDER_{name}", raising=False)
    monkeypatch.setenv("RELIQUARY_EMBEDDER_PROVIDER", "ollama")
    monkeypatch.setenv("RELIQUARY_EMBEDDER_DIMS", "768")
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"vector_store": {"provider": "qdrant", "config": {"host": "q"}}}), encoding="utf-8")
    config = load_config(str(path))
    assert config["embedder"] == {"provider": "ollama", "config": {"embedding_dims": 768}}
    assert config["vector_store"]["config"]["embedding_model_dims"] == 768


def test_load_config_rejects_missing_and_non_mapping_files(tmp_path):
    with pytest.raises(SystemExit, match="Config file not found"):
        load_config(str(tmp_path / "missing.yaml"))
    path = tmp_path / "list.json"
    path.write_text("[1, 2]", encoding="utf-8")
    with pytest.raises(SystemExit, match="mapping/object"):
        load_config(str(path))


# --- Finding 2: Mem0 telemetry is off unless explicitly enabled


@pytest.mark.parametrize("module", ["ingest", "server"])
@pytest.mark.parametrize("preset, expected", [(None, "false"), ("true", "true"), ("False", "False")])
def test_mem0_telemetry_defaults_off_on_import(module, preset, expected):
    env = {k: v for k, v in os.environ.items() if k != "MEM0_TELEMETRY"}
    if preset is not None:
        env["MEM0_TELEMETRY"] = preset
    code = f"import os, sys; sys.path.insert(0, 'app'); import {module}; print(os.environ['MEM0_TELEMETRY'])"
    out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True,
                         check=True)
    assert out.stdout.strip() == expected


# --- Finding 14: examples/obsidian_to_jsonl.py


def _run_example(vault: Path, output: Path):
    return subprocess.run([sys.executable, str(ROOT / "examples" / "obsidian_to_jsonl.py"), str(vault),
                           "-o", str(output)], capture_output=True, text=True)


def test_obsidian_example_handles_yaml_dates_in_tags(tmp_path):
    pytest.importorskip("yaml")
    vault = tmp_path / "vault"
    (vault / "Günlük").mkdir(parents=True)
    (vault / "Günlük" / "day.md").write_text("---\ntags: [2024-01-02, journal]\n---\nbody\n", encoding="utf-8")
    output = tmp_path / "out.jsonl"
    result = _run_example(vault, output)
    assert result.returncode == 0, result.stderr
    [record] = read_jsonl(output)
    assert record["metadata"]["tags"] == ["2024-01-02", "journal"]
    assert record["metadata"]["domain"] == "günlük"


def test_obsidian_example_keeps_previous_output_when_a_note_fails(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "ok.md").write_text("fine\n", encoding="utf-8")
    output = tmp_path / "out.jsonl"
    output.write_text("previous corpus\n", encoding="utf-8")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root can read a mode-000 file")
    broken = vault / "broken.md"
    broken.write_text("unreadable\n", encoding="utf-8")
    broken.chmod(0)
    try:
        result = _run_example(vault, output)
    finally:
        broken.chmod(0o600)
    assert result.returncode != 0
    assert output.read_text(encoding="utf-8") == "previous corpus\n"
    assert not list(tmp_path.glob("out.jsonl.*"))
