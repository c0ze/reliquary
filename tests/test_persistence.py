import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from persistence import JsonFileStore  # noqa: E402


def test_save_load_roundtrip(tmp_path):
    store = JsonFileStore(str(tmp_path / "s.json"))
    assert store.load() == {}
    store.save({"a": 1, "b": {"c": 2}})
    assert store.load() == {"a": 1, "b": {"c": 2}}


def test_corrupt_file_loads_empty(tmp_path):
    p = tmp_path / "s.json"
    p.write_text("not json{")
    assert JsonFileStore(str(p)).load() == {}


def test_save_is_atomic_no_tmp_left(tmp_path):
    store = JsonFileStore(str(tmp_path / "s.json"))
    store.save({"x": 1})
    leftovers = [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_state_dir_created_eagerly_at_proxy_init(make_proxy, tmp_path):
    """An unwritable/missing state_dir mount should fail fast at startup, the
    same way BlobStore/PageRegistry fail fast on their dirs — not silently
    defer directory creation to the first OAuth token write mid-request."""
    state_dir = tmp_path / "nested" / "state"
    assert not state_dir.exists()

    make_proxy(state_dir=str(state_dir))

    assert state_dir.is_dir()
