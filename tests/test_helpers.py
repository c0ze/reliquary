"""Unit tests for the extracted stateless helpers."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from helpers import (  # noqa: E402
    added_memory_ids,
    coerce_threshold,
    decode_headers,
    extract_assistant_text_from_response,
    extract_text_content,
    extract_text_from_stream_event,
    format_fetched_document,
    json_dumps,
    latest_user_text,
    lean_add_image_args,
    lean_add_memory_args,
    lean_update_args,
    normalize_base_url,
    normalize_token,
    parse_form,
    preview_body,
    trim_text,
)


def test_json_dumps_keeps_unicode():
    out = json_dumps({"t": "日本語 Türkçe"})
    assert "日本語".encode() in out
    assert b"\\u" not in out


def test_decode_headers_lowercases():
    scope = {"headers": [(b"Authorization", b"Bearer x"), (b"Host", b"h")]}
    assert decode_headers(scope) == {"authorization": "Bearer x", "host": "h"}


def test_parse_form():
    assert parse_form(b"a=1&b=2&b=3") == {"a": "1", "b": "2"}


def test_normalize_base_url():
    assert normalize_base_url("http://x/") == "http://x"
    assert normalize_base_url("") is None
    assert normalize_base_url(None) is None


def test_normalize_token():
    assert normalize_token("  tok  ") == "tok"
    assert normalize_token("   ") is None
    assert normalize_token(None) is None


def test_extract_text_content_variants():
    assert extract_text_content("hello ") == "hello"
    assert extract_text_content([{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert extract_text_content(123) == ""


def test_latest_user_text_takes_last_two():
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "x"},
        {"role": "user", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    assert latest_user_text(messages) == "second\n\nthird"


def test_extract_assistant_text_from_response():
    data = {"choices": [{"message": {"content": "hi"}}]}
    assert extract_assistant_text_from_response(data) == "hi"
    assert extract_assistant_text_from_response({"choices": []}) == ""


def test_extract_handles_malformed_payloads():
    # non-dict entries must not raise AttributeError
    assert extract_assistant_text_from_response({"choices": ["oops"]}) == ""
    assert extract_assistant_text_from_response({"choices": [{"message": "nope"}]}) == ""
    assert extract_text_from_stream_event({"choices": ["oops", {"delta": "x"}]}) == ""
    assert extract_text_from_stream_event({"choices": [{"delta": {"content": "y"}}]}) == "y"


def test_coerce_threshold():
    assert coerce_threshold("0.5") == 0.5
    assert coerce_threshold("") is None
    assert coerce_threshold(None) is None
    assert coerce_threshold("nope") is None


def test_coerce_threshold_rejects_non_finite():
    assert coerce_threshold("nan") is None
    assert coerce_threshold("inf") is None
    assert coerce_threshold("-inf") is None
    assert coerce_threshold(float("nan")) is None


def test_trim_text():
    assert trim_text("a   b\n c", 100) == "a b c"
    assert trim_text("abcdefgh", 5) == "ab..."


def test_trim_text_respects_small_limits():
    # must never return more than `limit` characters
    for limit in range(0, 6):
        assert len(trim_text("abcdefgh", limit)) <= limit


def test_lean_add_memory_args_strips_smuggled_fields():
    raw = {
        "text": "hi", "title": "t", "topic": "x", "source": "s", "infer": True,
        # these must NOT pass through to the write handler on the OpenAI path:
        "user_id": "someone_else", "metadata": {"evil": 1},
        "domain": "d", "hall": "h", "room": "r", "source_ref": "ref", "kind": "k",
    }
    cleaned = lean_add_memory_args(raw)
    assert cleaned == {"text": "hi", "title": "t", "topic": "x", "source": "s", "infer": True}
    assert "user_id" not in cleaned and "metadata" not in cleaned and "domain" not in cleaned


def test_lean_add_image_args_strips_smuggled_fields():
    raw = {
        "caption": "a cat", "image_base64": "AAAA", "mimetype": "image/png", "title": "t",
        # these must NOT pass through on the OpenAI path:
        "user_id": "someone_else", "metadata": {"evil": 1},
        "domain": "d", "hall": "h", "room": "r",
    }
    cleaned = lean_add_image_args(raw)
    assert cleaned == {"caption": "a cat", "image_base64": "AAAA", "mimetype": "image/png", "title": "t"}
    assert "user_id" not in cleaned and "metadata" not in cleaned


def test_lean_update_args_strips_smuggled_fields():
    raw = {"id": "x", "text": "new", "user_id": "someone", "metadata": {"evil": 1}, "domain": "d"}
    cleaned = lean_update_args(raw)
    assert cleaned == {"id": "x", "text": "new"}
    assert "user_id" not in cleaned and "metadata" not in cleaned


def test_preview_body_caps_and_flags():
    body, truncated = preview_body("x" * 50, limit=10)
    assert truncated is True
    assert len(body) <= 10
    body2, truncated2 = preview_body("short", limit=10)
    assert (body2, truncated2) == ("short", False)


def test_format_fetched_document_includes_body():
    doc = {"id": "abc", "title": "NAS", "url": "mem0://record/abc", "text": "Synology DS220+ at 192.168.11.3."}
    out = format_fetched_document(doc)
    # The body must be in the text content, not just structuredContent.
    assert "Synology DS220+" in out
    assert "# NAS" in out
    assert "mem0://record/abc" in out


def test_format_fetched_document_handles_missing_fields():
    assert format_fetched_document({"title": "Only title"}) == "# Only title"
    assert format_fetched_document({"id": "x"}) == "# x"
    assert format_fetched_document({}) == "# Document"


def test_added_memory_ids():
    assert added_memory_ids({"results": [{"id": "abc", "event": "ADD"}]}) == ["abc"]
    # all ids returned when infer splits into several atomic facts
    assert added_memory_ids({"results": [{"id": "1"}, {"id": "2"}]}) == ["1", "2"]
    # unexpected / empty shapes degrade to an empty list (never raise)
    assert added_memory_ids({"results": []}) == []
    assert added_memory_ids({"results": [{"memory": "no id"}]}) == []
    assert added_memory_ids({"results": [{"id": ""}]}) == []  # falsy id skipped
    assert added_memory_ids({"results": 123}) == []  # non-list truthy: must not raise
    assert added_memory_ids({"results": True}) == []
    assert added_memory_ids({"results": [123, {"id": "ok"}]}) == ["ok"]  # non-dict item skipped
    assert added_memory_ids("nope") == []
    assert added_memory_ids({}) == []


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
