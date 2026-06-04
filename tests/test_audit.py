"""Unit tests for the append-only JSONL audit log."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from audit import AuditLog  # noqa: E402


def test_record_writes_jsonl_line(tmp_path):
    log_path = str(tmp_path / "audit.jsonl")
    al = AuditLog(log_path, clock=lambda: 1000.0)
    al.record(action="mem0_add_memory", endpoint="claude", user_id="alice")

    lines = Path(log_path).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["ts"] == 1000.0
    assert entry["action"] == "mem0_add_memory"
    assert entry["endpoint"] == "claude"
    assert entry["user_id"] == "alice"


def test_record_appends_multiple_lines(tmp_path):
    log_path = str(tmp_path / "audit.jsonl")
    al = AuditLog(log_path, clock=lambda: 0.0)
    al.record(action="mem0_add_memory", user_id="u1")
    al.record(action="mem0_delete", user_id="u1")

    lines = Path(log_path).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["action"] == "mem0_add_memory"
    assert json.loads(lines[1])["action"] == "mem0_delete"


def test_none_path_is_noop(tmp_path):
    al = AuditLog(None)
    al.record(action="mem0_add_memory", user_id="u1")
    # No file should have been created anywhere in tmp_path
    assert list(tmp_path.iterdir()) == []


def test_empty_string_path_is_noop(tmp_path):
    al = AuditLog("")
    al.record(action="mem0_add_memory", user_id="u1")
    assert list(tmp_path.iterdir()) == []


def test_creates_parent_directory(tmp_path):
    log_path = str(tmp_path / "subdir" / "nested" / "audit.jsonl")
    al = AuditLog(log_path, clock=lambda: 1.0)
    al.record(action="test")
    assert Path(log_path).exists()
    entry = json.loads(Path(log_path).read_text().strip())
    assert entry["action"] == "test"
