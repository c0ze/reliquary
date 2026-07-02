from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import health  # noqa: E402
from compiled import PageInfo  # noqa: E402


def _page(slug, *, domain=None, topic=None, status="current", supersedes=None, derived_from=None):
    return PageInfo(
        slug=slug,
        current_blob="x",
        domain=domain,
        topic=topic,
        status=status,
        supersedes=list(supersedes or []),
        derived_from=list(derived_from or []),
    )


# --- stale_pages -----------------------------------------------------------


def test_stale_pages_filters_by_status():
    pages = [_page("a", status="current"), _page("b", status="stale"), _page("c", status="draft")]
    out = health.stale_pages(pages)
    assert [item["slug"] for item in out] == ["b"]
    assert out[0]["kind"] == "stale_page"
    assert "reason" in out[0]


def test_stale_pages_empty_when_none_stale():
    assert health.stale_pages([_page("a"), _page("b")]) == []


# --- coverage_gaps ---------------------------------------------------------


def test_coverage_gaps_flags_uncovered_domain_over_min():
    pages = [_page("p", domain="infra", status="current")]
    counts = {"pagan": 10, "infra": 5}
    out = health.coverage_gaps(pages, counts, min_count=8)
    domains = {item["domain"] for item in out}
    assert domains == {"pagan"}  # infra is covered; pagan over min and uncovered
    assert out[0]["kind"] == "coverage_gap"
    assert out[0]["count"] == 10


def test_coverage_gaps_respects_min_count():
    out = health.coverage_gaps([], {"pagan": 3}, min_count=8)
    assert out == []  # below min_count


def test_coverage_gaps_includes_exact_min_count():
    out = health.coverage_gaps([], {"pagan": 8}, min_count=8)
    assert {item["domain"] for item in out} == {"pagan"}  # boundary is inclusive (>=)


def test_coverage_gaps_skips_domain_with_current_page():
    pages = [_page("p", domain="pagan", status="current")]
    out = health.coverage_gaps(pages, {"pagan": 50}, min_count=8)
    assert out == []  # already has a current synthesis


def test_coverage_gaps_stale_page_does_not_count_as_covered():
    pages = [_page("p", domain="pagan", status="stale")]
    out = health.coverage_gaps(pages, {"pagan": 50}, min_count=8)
    assert {item["domain"] for item in out} == {"pagan"}  # stale != covered


def test_coverage_gaps_skips_empty_domain_key():
    out = health.coverage_gaps([], {"": 100}, min_count=8)
    assert out == []


# --- supersession ----------------------------------------------------------


def test_supersession_flags_current_superseding_current():
    pages = [
        _page("new", status="current", supersedes=["old"]),
        _page("old", status="current"),
    ]
    out = health.supersession(pages)
    assert len(out) == 1
    assert out[0]["kind"] == "supersession"
    assert out[0]["slug"] == "new"
    assert out[0]["supersedes"] == "old"


def test_supersession_ignores_superseded_archived():
    pages = [
        _page("new", status="current", supersedes=["old"]),
        _page("old", status="archived"),
    ]
    assert health.supersession(pages) == []  # superseded page no longer current


def test_supersession_ignores_superseded_absent():
    pages = [_page("new", status="current", supersedes=["ghost"])]
    assert health.supersession(pages) == []  # superseded slug absent entirely


def test_supersession_ignores_when_superseder_not_current():
    pages = [
        _page("new", status="stale", supersedes=["old"]),
        _page("old", status="current"),
    ]
    assert health.supersession(pages) == []  # superseder itself not current


# --- orphans ---------------------------------------------------------------


class _Rec:
    def __init__(self, rid, metadata):
        self.import_record_id = rid
        self.metadata = metadata


def test_orphans_flags_records_without_domain():
    records = [
        _Rec("r1", {"domain": "pagan"}),
        _Rec("r2", {"title": "no domain"}),
        _Rec("r3", {}),
    ]
    out = health.orphans(records)
    ids = {item["id"] for item in out}
    assert ids == {"r2", "r3"}
    assert out[0]["kind"] == "orphan"


def test_orphans_respects_limit():
    records = [_Rec(f"r{i}", {}) for i in range(100)]
    out = health.orphans(records, limit=5)
    assert len(out) == 5


def test_orphans_handles_dict_records():
    records = [
        {"id": "d1", "metadata": {"domain": "infra"}},
        {"id": "d2", "metadata": {}},
    ]
    out = health.orphans(records)
    assert {item["id"] for item in out} == {"d2"}


# --- cold_records ------------------------------------------------------------


def test_cold_records_flags_ids_absent_from_stats():
    records = [
        _Rec("r1", {"domain": "pagan"}),
        _Rec("r2", {"domain": "infra"}),
    ]
    stats_by_id = {"r1": {"count": 3, "last_ts": 1.0, "domain": "pagan", "topic": "t"}}
    out = health.cold_records(records, stats_by_id)
    assert [item["id"] for item in out] == ["r2"]
    assert out[0]["kind"] == "cold_record"
    assert out[0]["domain"] == "infra"
    assert "reason" in out[0]


def test_cold_records_empty_stats_guard_returns_nothing():
    # No evidence collected yet (stats disabled/empty) must never be treated as "all cold".
    records = [_Rec("r1", {"domain": "pagan"}), _Rec("r2", {"domain": "infra"})]
    out = health.cold_records(records, {})
    assert out == []


def test_cold_records_respects_limit():
    records = [_Rec(f"r{i}", {"domain": "pagan"}) for i in range(100)]
    out = health.cold_records(records, {"unrelated": {"count": 1}}, limit=5)
    assert len(out) == 5


def test_cold_records_handles_dict_records():
    records = [
        {"id": "d1", "metadata": {"domain": "infra"}},
        {"id": "d2", "metadata": {"domain": "pagan"}},
    ]
    stats_by_id = {"d1": {"count": 1}}
    out = health.cold_records(records, stats_by_id)
    assert [item["id"] for item in out] == ["d2"]
    assert out[0]["domain"] == "pagan"


def test_cold_records_skips_records_without_id():
    records = [_Rec(None, {"domain": "pagan"}), {"metadata": {"domain": "infra"}}]
    out = health.cold_records(records, {"whatever": {"count": 1}})
    assert out == []


# --- hot_topics_without_synthesis --------------------------------------------


def test_hot_topics_flags_uncovered_hot_topic():
    stats_by_topic = {"pagan\tsamhain": 10}
    out = health.hot_topics_without_synthesis([], stats_by_topic, min_retrievals=5)
    assert len(out) == 1
    assert out[0]["kind"] == "hot_topic"
    assert out[0]["domain"] == "pagan"
    assert out[0]["topic"] == "samhain"
    assert out[0]["count"] == 10
    assert "reason" in out[0]


def test_hot_topics_skips_topic_with_current_page():
    pages = [_page("p", domain="pagan", topic="samhain", status="current")]
    stats_by_topic = {"pagan\tsamhain": 10}
    out = health.hot_topics_without_synthesis(pages, stats_by_topic, min_retrievals=5)
    assert out == []


def test_hot_topics_below_min_retrievals_not_flagged():
    stats_by_topic = {"pagan\tsamhain": 4}
    out = health.hot_topics_without_synthesis([], stats_by_topic, min_retrievals=5)
    assert out == []


def test_hot_topics_stale_or_draft_page_does_not_count_as_covered():
    pages = [
        _page("p1", domain="pagan", topic="samhain", status="stale"),
        _page("p2", domain="infra", topic="k8s", status="draft"),
    ]
    stats_by_topic = {"pagan\tsamhain": 10, "infra\tk8s": 10}
    out = health.hot_topics_without_synthesis(pages, stats_by_topic, min_retrievals=5)
    assert {(item["domain"], item["topic"]) for item in out} == {("pagan", "samhain"), ("infra", "k8s")}


def test_hot_topics_skips_malformed_keys():
    stats_by_topic = {"no-tab-here": 10, "\ttopic": 10, "domain\t": 10, "": 10}
    out = health.hot_topics_without_synthesis([], stats_by_topic, min_retrievals=1)
    assert out == []


def test_hot_topics_sort_order():
    stats_by_topic = {
        "a\ttopic1": 5,
        "b\ttopic2": 10,
        "a\ttopic2": 10,
    }
    out = health.hot_topics_without_synthesis([], stats_by_topic, min_retrievals=1)
    ordered = [(item["domain"], item["topic"], item["count"]) for item in out]
    assert ordered == [("a", "topic2", 10), ("b", "topic2", 10), ("a", "topic1", 5)]


# --- run_all ---------------------------------------------------------------


def test_run_all_returns_all_four_keys():
    pages = [
        _page("stale-one", status="stale"),
        _page("super", status="current", supersedes=["covered"]),
        _page("covered", domain="pagan", status="current"),
    ]
    records = [_Rec("orphan-1", {})]
    out = health.run_all(pages, raw_counts={"infra": 20}, min_count=8, records=records)
    assert set(out.keys()) == {"stale_pages", "coverage_gaps", "supersession", "orphans",
                                "cold_records", "hot_topics"}
    assert [item["slug"] for item in out["stale_pages"]] == ["stale-one"]
    assert {item["domain"] for item in out["coverage_gaps"]} == {"infra"}
    assert [item["slug"] for item in out["supersession"]] == ["super"]
    assert [item["id"] for item in out["orphans"]] == ["orphan-1"]


def test_run_all_records_default_empty():
    out = health.run_all([_page("a")], raw_counts={}, min_count=8)
    assert out["orphans"] == []


def test_run_all_backward_compat_no_stats_new_keys_empty():
    pages = [_page("a")]
    records = [_Rec("r1", {})]
    out = health.run_all(pages, raw_counts={}, min_count=8, records=records)
    assert out["cold_records"] == []  # no stats passed -> empty-stats guard, not false positives
    assert out["hot_topics"] == []


def test_run_all_with_stats_surfaces_new_proposals():
    pages = [_page("a", domain="pagan", topic="samhain", status="current")]
    records = [_Rec("r1", {"domain": "infra"}), _Rec("r2", {"domain": "pagan"})]
    stats = {
        "by_id": {"r1": {"count": 2, "last_ts": 1.0, "domain": "infra", "topic": "x"}},
        "by_topic": {"infra\tx": 10},
        "by_domain": {},
        "events": 500,  # above the cold-record floor so archive proposals are trusted
    }
    out = health.run_all(pages, raw_counts={}, min_count=8, records=records, stats=stats)
    assert [item["id"] for item in out["cold_records"]] == ["r2"]
    assert [(item["domain"], item["topic"]) for item in out["hot_topics"]] == [("infra", "x")]


def test_run_all_cold_records_suppressed_below_event_floor():
    # Partial stats (some evidence, but not enough) must NOT flag records as cold —
    # a not-yet-retrieved record looks identical to a genuinely cold one at this scale.
    records = [_Rec("r1", {"domain": "infra"}), _Rec("r2", {"domain": "pagan"})]
    stats = {
        "by_id": {"r1": {"count": 1, "last_ts": 1.0, "domain": "infra", "topic": "x"}},
        "by_topic": {},
        "by_domain": {},
        "events": 5,  # below COLD_RECORD_MIN_EVENTS
    }
    out = health.run_all([], raw_counts={}, min_count=8, records=records, stats=stats)
    assert out["cold_records"] == []


def test_run_all_cold_records_appear_at_event_floor():
    records = [_Rec("r1", {"domain": "infra"}), _Rec("r2", {"domain": "pagan"})]
    stats = {
        "by_id": {"r1": {"count": 1, "last_ts": 1.0, "domain": "infra", "topic": "x"}},
        "by_topic": {},
        "by_domain": {},
        "events": health.COLD_RECORD_MIN_EVENTS,  # exactly at the floor is enough (>=)
    }
    out = health.run_all([], raw_counts={}, min_count=8, records=records, stats=stats)
    assert [item["id"] for item in out["cold_records"]] == ["r2"]


def test_run_all_cold_min_events_override_lets_sparse_through():
    records = [_Rec("r1", {"domain": "infra"}), _Rec("r2", {"domain": "pagan"})]
    stats = {
        "by_id": {"r1": {"count": 1, "last_ts": 1.0, "domain": "infra", "topic": "x"}},
        "by_topic": {},
        "by_domain": {},
        "events": 5,  # below the default floor, but override drops the threshold to 0
    }
    out = health.run_all([], raw_counts={}, min_count=8, records=records, stats=stats,
                         cold_min_events=0)
    assert [item["id"] for item in out["cold_records"]] == ["r2"]


def test_run_all_hot_topic_min_defaults_to_min_count():
    pages = []
    stats = {"by_id": {}, "by_topic": {"pagan\tsamhain": 8}, "by_domain": {}, "events": 8}
    out_below = health.run_all(pages, raw_counts={}, min_count=10, records=(), stats=stats)
    assert out_below["hot_topics"] == []  # 8 < default min_count=10

    out_explicit = health.run_all(pages, raw_counts={}, min_count=10, records=(), stats=stats,
                                   hot_topic_min=5)
    assert [item["count"] for item in out_explicit["hot_topics"]] == [8]  # explicit override wins
