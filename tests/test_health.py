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


# --- run_all ---------------------------------------------------------------


def test_run_all_returns_all_four_keys():
    pages = [
        _page("stale-one", status="stale"),
        _page("super", status="current", supersedes=["covered"]),
        _page("covered", domain="pagan", status="current"),
    ]
    records = [_Rec("orphan-1", {})]
    out = health.run_all(pages, raw_counts={"infra": 20}, min_count=8, records=records)
    assert set(out.keys()) == {"stale_pages", "coverage_gaps", "supersession", "orphans"}
    assert [item["slug"] for item in out["stale_pages"]] == ["stale-one"]
    assert {item["domain"] for item in out["coverage_gaps"]} == {"infra"}
    assert [item["slug"] for item in out["supersession"]] == ["super"]
    assert [item["id"] for item in out["orphans"]] == ["orphan-1"]


def test_run_all_records_default_empty():
    out = health.run_all([_page("a")], raw_counts={}, min_count=8)
    assert out["orphans"] == []
