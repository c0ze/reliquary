"""Query-time retrieval quality helpers.

Mem0's vector rank is the primary signal. These helpers add small, deterministic
adjustments for lexical fit and recency, then remove near-duplicate result text
so one repeated fact does not consume the whole result budget.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from typing import Any


_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DATE_KEYS = (
    "updated_at",
    "updatedAt",
    "created_at",
    "createdAt",
    "timestamp",
    "date",
)
_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "you",
    "your",
    "about",
    "into",
}


def retrieval_candidate_limit(limit: int) -> int:
    """Return how many raw vector candidates to request before quality filtering."""
    if limit <= 0:
        return 0
    overfetch = min(50, max(limit * 4, limit + 8))
    return max(limit, overfetch)


def apply_retrieval_quality(
    query: str,
    hits: list[dict[str, Any]],
    *,
    limit: int,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """Rerank and deduplicate raw Mem0 hits, returning at most ``limit`` items."""
    if limit <= 0:
        return []

    current_time = time.time() if now is None else now
    query_terms = _content_terms(query)
    ranked = sorted(
        enumerate(hits),
        key=lambda item: (
            -_quality_score(item[1], query_terms=query_terms, now=current_time),
            item[0],
        ),
    )

    selected: list[dict[str, Any]] = []
    selected_tokens: list[set[str]] = []
    selected_norms: list[str] = []
    for _, hit in ranked:
        text = _hit_text(hit)
        normalized = _normalize_text(text)
        tokens = set(_tokens(text))
        if _is_duplicate(normalized, tokens, selected_norms, selected_tokens):
            continue
        selected.append(hit)
        selected_norms.append(normalized)
        selected_tokens.append(tokens)
        if len(selected) >= limit:
            break
    return selected


def _quality_score(hit: dict[str, Any], *, query_terms: set[str], now: float) -> float:
    score = _numeric_score(hit.get("score"))
    score += _lexical_bonus(query_terms, _hit_text(hit))
    score += _recency_bonus(hit, now=now)
    return score


def _numeric_score(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def _lexical_bonus(query_terms: set[str], text: str) -> float:
    if not query_terms:
        return 0.0
    text_terms = _content_terms(text)
    if not text_terms:
        return 0.0
    coverage = len(query_terms & text_terms) / len(query_terms)
    return 0.08 * coverage


def _recency_bonus(hit: dict[str, Any], *, now: float) -> float:
    timestamp = _hit_timestamp(hit)
    if timestamp is None:
        return 0.0
    age_days = max(0.0, (now - timestamp) / 86_400)
    return 0.05 / (1.0 + (age_days / 30.0))


def _hit_timestamp(hit: dict[str, Any]) -> float | None:
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    for source in (hit, metadata):
        for key in _DATE_KEYS:
            parsed = _parse_timestamp(source.get(key))
            if parsed is not None:
                return parsed
    return None


def _parse_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        parsed = float(value)
        if parsed > 10_000_000_000:
            parsed /= 1000.0
        return parsed
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return _parse_timestamp(float(text))
    except ValueError:
        pass
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def _is_duplicate(
    normalized: str,
    tokens: set[str],
    selected_norms: list[str],
    selected_tokens: list[set[str]],
) -> bool:
    if not normalized:
        return False
    for existing_norm, existing_tokens in zip(selected_norms, selected_tokens, strict=True):
        if normalized == existing_norm:
            return True
        if _token_similarity(tokens, existing_tokens) >= 0.86:
            return True
    return False


def _token_similarity(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    union = left | right
    if len(union) < 5:
        return 0.0
    return len(left & right) / len(union)


def _hit_text(hit: dict[str, Any]) -> str:
    value = hit.get("memory", hit.get("text", ""))
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str):
                    parts.append(content)
            elif isinstance(item, str):
                parts.append(item)
        return " ".join(parts)
    return str(value or "")


def _normalize_text(text: str) -> str:
    return " ".join(_tokens(text))


def _content_terms(text: str) -> set[str]:
    return {token for token in _tokens(text) if token not in _STOPWORDS and len(token) > 2}


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())
