"""Unit tests for the corpus routing taxonomy (pure, no mem0/qdrant)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from catalog import CorpusCatalog, CorpusRecord  # noqa: E402


def _record(rid, *, domain=None, hall=None, room=None, topic=None, title="t", text="body"):
    metadata = {"title": title}
    for key, value in (("domain", domain), ("hall", hall), ("room", room), ("topic", topic)):
        if value:
            metadata[key] = value
    return CorpusRecord(import_record_id=rid, title=title, text=text, metadata=metadata, source_ref="")


def _catalog():
    return CorpusCatalog(
        [
            _record("r1", domain="alpha", room="specs", topic="roadmap"),
            _record("r2", domain="alpha", room="specs"),
            _record("r3", domain="beta", room="tickets"),
        ]
    )


def test_routeable_domains_listed():
    cat = _catalog()
    assert set(cat.routeable_domains) == {"alpha", "beta"}


def test_domain_match():
    cat = _catalog()
    assert cat.match_query("what about alpha stuff") == {"domain": "alpha"}


def test_domain_plus_room():
    cat = _catalog()
    matched = cat.match_query("alpha specs please")
    assert matched.get("domain") == "alpha"
    assert matched.get("room") == "specs"


def test_generic_word_does_not_route():
    cat = _catalog()
    # "notes" is a generic value and must not produce a route
    assert cat.match_query("show me my notes") == {}


def test_domain_inferred_from_unique_room():
    cat = _catalog()
    # "tickets" only ever appears under the beta domain
    matched = cat.match_query("check the tickets")
    assert matched.get("domain") == "beta"
    assert matched.get("room") == "tickets"


def test_build_routes_orders_specific_then_global():
    cat = _catalog()
    routes = cat.build_routes("alpha specs")
    descriptions = [r.description for r in routes]
    assert descriptions[0].startswith("domain=alpha")
    assert descriptions[-1] == "global"
    assert routes[-1].filters is None


def test_build_routes_global_only_when_no_match():
    cat = _catalog()
    routes = cat.build_routes("hello there")
    assert len(routes) == 1
    assert routes[0].filters is None


def test_fetch_document_and_dead_method_gone():
    cat = _catalog()
    doc = cat.fetch_document("r1")
    assert doc["id"] == "r1" and doc["title"] == "t"
    assert cat.fetch_document("missing") is None
    assert not hasattr(cat, "enrich_hit")  # removed dead method


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    sys.exit(1 if failures else 0)
