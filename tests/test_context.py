from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from context import CallerContext, resolve_context  # noqa: E402


def test_args_context_yields_repo_slug():
    ctx = resolve_context({"context": {"repo": "c0ze/reliquary", "client": "codex"}}, {})
    assert ctx is not None
    assert ctx.repo == "c0ze/reliquary"
    assert ctx.repo_slug == "reliquary"
    assert ctx.client == "codex"


def test_git_root_basename_slug():
    ctx = resolve_context({"context": {"git_root": "/home/arda/projects/utils/"}}, {})
    assert ctx is not None and ctx.repo_slug == "utils"


def test_header_fallback_when_no_args_context():
    ctx = resolve_context({}, {"x-reliquary-repo": "c0ze/reliquary"})
    assert ctx is not None and ctx.repo_slug == "reliquary"


def test_args_take_precedence_over_headers():
    ctx = resolve_context({"context": {"repo": "owner/from-args"}}, {"x-reliquary-repo": "owner/from-header"})
    assert ctx.repo == "owner/from-args" and ctx.repo_slug == "from-args"


def test_no_signal_returns_none():
    assert resolve_context({}, {}) is None
    assert resolve_context({"context": {"client": "codex"}}, {}) is None  # client only, no repo


def test_header_key_case_insensitive():
    ctx = resolve_context({}, {"X-Reliquary-Repo": "owner/MyRepo"})
    assert ctx is not None and ctx.repo_slug == "myrepo"


def test_trailing_slash_repo_slug():
    ctx = resolve_context({"context": {"repo": "owner/name/"}}, {})
    assert ctx is not None and ctx.repo_slug == "name"


def test_malformed_context_is_ignored():
    assert resolve_context({"context": "not-a-dict"}, {}) is None
    assert resolve_context(None, None) is None
