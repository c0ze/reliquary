"""Unit tests for the append-only JSONL retrieval-stats log and aggregator."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from retrieval_stats import RetrievalStatsLog, aggregate  # noqa: E402


def test_none_path_is_noop(tmp_path):
    rs = RetrievalStatsLog(None)
    rs.record("search", [{"id": "a"}])
    assert list(tmp_path.iterdir()) == []


def test_empty_string_path_is_noop(tmp_path):
    rs = RetrievalStatsLog("")
    rs.record("search", [{"id": "a"}])
    assert list(tmp_path.iterdir()) == []


def test_aggregate_none_path_returns_empty_shape():
    result = aggregate(None)
    assert result == {"by_id": {}, "by_domain": {}, "by_topic": {}, "events": 0}


def test_aggregate_missing_file_returns_empty_shape(tmp_path):
    result = aggregate(str(tmp_path / "does_not_exist.jsonl"))
    assert result == {"by_id": {}, "by_domain": {}, "by_topic": {}, "events": 0}


def test_record_appends_one_line_per_item(tmp_path):
    log_path = str(tmp_path / "stats.jsonl")
    rs = RetrievalStatsLog(log_path, clock=lambda: 1000.0)
    rs.record("search", [{"id": "a", "domain": "work", "topic": "billing"}, {"id": "b"}])

    lines = Path(log_path).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2

    entry_a = json.loads(lines[0])
    assert entry_a["ts"] == 1000.0
    assert entry_a["event"] == "search"
    assert entry_a["id"] == "a"
    assert entry_a["domain"] == "work"
    assert entry_a["topic"] == "billing"

    entry_b = json.loads(lines[1])
    assert entry_b["id"] == "b"
    assert "domain" not in entry_b
    assert "topic" not in entry_b


def test_record_second_call_appends_more_lines(tmp_path):
    log_path = str(tmp_path / "stats.jsonl")
    rs = RetrievalStatsLog(log_path, clock=lambda: 1.0)
    rs.record("search", [{"id": "a"}])
    rs.record("fetch", [{"id": "b"}, {"id": "c"}])

    lines = Path(log_path).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["event"] == "search"
    assert json.loads(lines[1])["event"] == "fetch"
    assert json.loads(lines[2])["id"] == "c"


def test_record_creates_parent_directory(tmp_path):
    log_path = str(tmp_path / "subdir" / "nested" / "stats.jsonl")
    rs = RetrievalStatsLog(log_path, clock=lambda: 1.0)
    rs.record("fetch", [{"id": "a"}])
    assert Path(log_path).exists()


def test_record_omits_none_domain_topic(tmp_path):
    log_path = str(tmp_path / "stats.jsonl")
    rs = RetrievalStatsLog(log_path, clock=lambda: 1.0)
    rs.record("search", [{"id": "a", "domain": None, "topic": None}])

    entry = json.loads(Path(log_path).read_text(encoding="utf-8").strip())
    assert "domain" not in entry
    assert "topic" not in entry


def test_record_does_not_raise_when_open_fails(tmp_path, monkeypatch):
    log_path = str(tmp_path / "stats.jsonl")
    rs = RetrievalStatsLog(log_path, clock=lambda: 1.0)

    def boom(*args, **kwargs):
        raise OSError("disk on fire")

    monkeypatch.setattr("builtins.open", boom)
    rs.record("search", [{"id": "a"}])  # must not raise


def test_record_does_not_raise_when_parent_is_a_file(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    log_path = str(blocker / "stats.jsonl")
    rs = RetrievalStatsLog(log_path, clock=lambda: 1.0)
    rs.record("search", [{"id": "a"}])  # must not raise, file cannot be created


def test_record_does_not_raise_when_clock_raises(tmp_path):
    log_path = str(tmp_path / "stats.jsonl")
    rs = RetrievalStatsLog(log_path, clock=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    rs.record("search", [{"id": "a"}])  # must not raise
    # clock blew up before any write, so nothing was appended
    assert not Path(log_path).exists()


def test_record_does_not_raise_when_id_str_raises(tmp_path):
    log_path = str(tmp_path / "stats.jsonl")

    class BadId:
        def __str__(self):
            raise ValueError("un-stringable id")

        __repr__ = __str__

    rs = RetrievalStatsLog(log_path, clock=lambda: 1.0)
    rs.record("search", [{"id": BadId()}])  # json.dumps default=str would re-raise; must be swallowed


def test_record_does_not_raise_when_items_is_none(tmp_path):
    log_path = str(tmp_path / "stats.jsonl")
    rs = RetrievalStatsLog(log_path, clock=lambda: 1.0)
    rs.record("search", None)  # iterating None raises TypeError; must be swallowed


def test_record_does_not_raise_on_non_dict_item(tmp_path):
    log_path = str(tmp_path / "stats.jsonl")
    rs = RetrievalStatsLog(log_path, clock=lambda: 1.0)
    rs.record("search", ["not-a-dict"])  # .get on a str raises AttributeError; must be swallowed


def test_aggregate_reduces_multiple_events(tmp_path):
    log_path = str(tmp_path / "stats.jsonl")
    rs = RetrievalStatsLog(log_path, clock=lambda: 1.0)
    rs.record("search", [{"id": "a", "domain": "work", "topic": "billing"}])

    rs2 = RetrievalStatsLog(log_path, clock=lambda: 2.0)
    rs2.record("fetch", [{"id": "a", "domain": "work", "topic": "invoices"}])

    rs3 = RetrievalStatsLog(log_path, clock=lambda: 3.0)
    rs3.record("search", [{"id": "b", "domain": "home"}])

    result = aggregate(log_path)

    assert result["events"] == 3
    assert result["by_id"]["a"]["count"] == 2
    assert result["by_id"]["a"]["last_ts"] == 2.0
    # most-recent non-null domain/topic for id "a"
    assert result["by_id"]["a"]["domain"] == "work"
    assert result["by_id"]["a"]["topic"] == "invoices"

    assert result["by_id"]["b"]["count"] == 1
    assert result["by_id"]["b"]["last_ts"] == 3.0
    assert result["by_id"]["b"]["domain"] == "home"
    assert result["by_id"]["b"]["topic"] is None

    assert result["by_domain"] == {"work": 2, "home": 1}
    # by_topic only counts events with both domain and topic
    assert result["by_topic"] == {"work\tbilling": 1, "work\tinvoices": 1}


def test_aggregate_keeps_most_recent_non_null_domain_topic(tmp_path):
    log_path = str(tmp_path / "stats.jsonl")
    rs1 = RetrievalStatsLog(log_path, clock=lambda: 1.0)
    rs1.record("search", [{"id": "a", "domain": "work", "topic": "billing"}])
    rs2 = RetrievalStatsLog(log_path, clock=lambda: 2.0)
    rs2.record("fetch", [{"id": "a"}])  # no domain/topic on this event

    result = aggregate(log_path)
    # last event had no domain/topic, so most-recent non-null values are retained
    assert result["by_id"]["a"]["count"] == 2
    assert result["by_id"]["a"]["last_ts"] == 2.0
    assert result["by_id"]["a"]["domain"] == "work"
    assert result["by_id"]["a"]["topic"] == "billing"


def test_aggregate_tolerates_malformed_and_idless_lines(tmp_path):
    log_path = tmp_path / "stats.jsonl"
    rs = RetrievalStatsLog(str(log_path), clock=lambda: 1.0)
    rs.record("search", [{"id": "a", "domain": "work", "topic": "billing"}])

    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write("not valid json at all\n")
        fh.write(json.dumps({"ts": 2.0, "event": "search", "domain": "work"}) + "\n")  # no id
        fh.write("\n")  # blank line

    rs2 = RetrievalStatsLog(str(log_path), clock=lambda: 3.0)
    rs2.record("fetch", [{"id": "b"}])

    result = aggregate(str(log_path))

    assert result["events"] == 2  # only the two valid, id-having lines
    assert set(result["by_id"].keys()) == {"a", "b"}
    assert result["by_domain"] == {"work": 1}


def test_aggregate_by_domain_skips_events_without_domain(tmp_path):
    log_path = str(tmp_path / "stats.jsonl")
    rs = RetrievalStatsLog(log_path, clock=lambda: 1.0)
    rs.record("search", [{"id": "a"}])

    result = aggregate(log_path)
    assert result["by_domain"] == {}
    assert result["by_topic"] == {}
    assert result["by_id"]["a"]["domain"] is None
    assert result["by_id"]["a"]["topic"] is None


def test_aggregate_by_topic_skips_events_missing_topic(tmp_path):
    log_path = str(tmp_path / "stats.jsonl")
    rs = RetrievalStatsLog(log_path, clock=lambda: 1.0)
    rs.record("search", [{"id": "a", "domain": "work"}])  # domain but no topic

    result = aggregate(log_path)
    assert result["by_domain"] == {"work": 1}
    assert result["by_topic"] == {}


def test_aggregate_tolerates_invalid_utf8_line(tmp_path):
    log_path = tmp_path / "stats.jsonl"
    rs = RetrievalStatsLog(str(log_path), clock=lambda: 1.0)
    rs.record("search", [{"id": "a", "domain": "work", "topic": "billing"}])

    # A raw invalid-UTF-8 byte sequence: decoding must not blow up the whole read.
    with open(log_path, "ab") as fh:
        fh.write(b"\xff\xfe not utf-8\n")

    rs2 = RetrievalStatsLog(str(log_path), clock=lambda: 2.0)
    rs2.record("fetch", [{"id": "b"}])

    result = aggregate(str(log_path))
    assert result["events"] == 2  # both good lines counted; garbled line skipped
    assert set(result["by_id"].keys()) == {"a", "b"}


def test_aggregate_tolerates_non_scalar_domain_and_topic(tmp_path):
    # domain/topic ultimately come from caller/importer-controlled metadata. If a
    # retrieved memory's metadata.domain (or topic) is a list/dict, the event line
    # persists it verbatim; aggregate() must not use it as a dict key (unhashable ->
    # TypeError) and must treat it as absent instead, same as a missing value.
    log_path = tmp_path / "stats.jsonl"
    with open(log_path, "w", encoding="utf-8") as fh:
        # well-formed event, should aggregate normally
        fh.write(json.dumps({"ts": 1.0, "event": "search", "id": "a", "domain": "work", "topic": "billing"}) + "\n")
        # domain is a list (unhashable) -- must not raise, must not be counted
        fh.write(json.dumps({"ts": 2.0, "event": "search", "id": "b", "domain": ["work", "home"], "topic": "billing"}) + "\n")
        # topic is a dict (unhashable) -- must not raise, must not be counted
        fh.write(json.dumps({"ts": 3.0, "event": "search", "id": "c", "domain": "work", "topic": {"x": 1}}) + "\n")
        # another well-formed event, should still aggregate normally
        fh.write(json.dumps({"ts": 4.0, "event": "search", "id": "d", "domain": "home", "topic": "chores"}) + "\n")

    result = aggregate(str(log_path))  # must not raise TypeError: unhashable type

    assert result["events"] == 4

    # well-formed events aggregate as usual
    assert result["by_id"]["a"]["domain"] == "work"
    assert result["by_id"]["a"]["topic"] == "billing"
    assert result["by_id"]["d"]["domain"] == "home"
    assert result["by_id"]["d"]["topic"] == "chores"

    # non-scalar domain/topic are treated as absent: not stored on by_id...
    assert result["by_id"]["b"]["domain"] is None
    assert result["by_id"]["b"]["topic"] == "billing"  # topic on "b" was a valid string
    assert result["by_id"]["c"]["domain"] == "work"  # domain on "c" was a valid string
    assert result["by_id"]["c"]["topic"] is None

    # ...and not counted in by_domain/by_topic.
    assert result["by_domain"] == {"work": 2, "home": 1}  # "b"'s list-domain not counted
    assert result["by_topic"] == {"work\tbilling": 1, "home\tchores": 1}  # "c"'s dict-topic not counted


def test_aggregate_domain_topic_track_most_recent_by_ts_out_of_order(tmp_path):
    # Write lines out of ts order: the newer-by-ts event appears FIRST in the file.
    log_path = tmp_path / "stats.jsonl"
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": 2.0, "event": "fetch", "id": "a", "domain": "work", "topic": "invoices"}) + "\n")
        fh.write(json.dumps({"ts": 1.0, "event": "search", "id": "a", "domain": "work", "topic": "billing"}) + "\n")

    result = aggregate(str(log_path))
    # last_ts is the true max, and domain/topic reflect that same most-recent-by-ts event,
    # not the last line read from the file.
    assert result["by_id"]["a"]["count"] == 2
    assert result["by_id"]["a"]["last_ts"] == 2.0
    assert result["by_id"]["a"]["topic"] == "invoices"
