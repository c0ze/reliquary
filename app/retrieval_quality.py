"""Query-time retrieval quality helpers.

Mem0's vector rank is the primary signal. These helpers add small, deterministic
adjustments for lexical fit and recency, then remove near-duplicate result text
so one repeated fact does not consume the whole result budget.
"""

from __future__ import annotations

import math
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any


_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Scripts written without spaces between words: there a "word" run is a whole
# phrase or sentence, so near-duplicate detection uses character bigrams.
_UNSPACED_RE = re.compile(
    "[\u0e00-\u0eff"  # Thai, Lao
    "\u1000-\u109f"  # Myanmar
    "\u1780-\u17ff"  # Khmer
    "\u3005-\u3007\u3040-\u30ff\u31f0-\u31ff"  # CJK marks, kana
    "\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"  # CJK ideographs
    "\uff66-\uff9f]"  # halfwidth katakana
)
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
        if _is_correction(hit):
            # A proposed correction is by design a small edit of its target;
            # dropping either one as a "duplicate" hides the disagreement the
            # correction exists to surface. Keep it, and never let it shadow
            # another record either.
            selected.append(hit)
            if len(selected) >= limit:
                break
            continue
        text = _hit_text(hit)
        normalized = _normalize_text(text)
        tokens = _dedup_tokens(text)
        if _is_duplicate(normalized, tokens, selected_norms, selected_tokens):
            continue
        selected.append(hit)
        selected_norms.append(normalized)
        selected_tokens.append(tokens)
        if len(selected) >= limit:
            break
    return selected


def _is_correction(hit: dict[str, Any]) -> bool:
    metadata = hit.get("metadata")
    return isinstance(metadata, dict) and metadata.get("kind") == "correction"


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
    # Exact-duplicate key: the full text, case- and whitespace-normalised.
    # (Reducing it to ASCII word tokens collapsed distinct Japanese or Turkish
    # notes that happened to share one Latin word or a number.)
    return " ".join(_fold(text).split())


def _fold(text: str) -> str:
    # NFKC folds full-width Latin and half-width kana. Turkish casing is not
    # locale-free ("I" lowers to "ı" in Turkish, "i" elsewhere; "İ" lowers to
    # "i" + U+0307), so all four i forms fold to "i": "ŞIK" and "şık" agree.
    # "ş", "ğ", "ç", "ö", "ü" stay distinct letters.
    return unicodedata.normalize("NFKC", text).lower().replace("i\u0307", "i").replace("ı", "i")


def _dedup_tokens(text: str) -> set[str]:
    """Unicode-aware token set for near-duplicate detection.

    Letters, digits and combining marks of any script form word runs. Runs in
    scripts written without spaces (Japanese, Chinese, Thai, ...) contribute
    character bigrams; everything else contributes whole words.
    """
    tokens: set[str] = set()
    for run in _word_runs(_fold(text)):
        position = 0
        for match in _UNSPACED_RE.finditer(run):
            start = match.start()
            if start > position:
                tokens.add(run[position:start])
            position = match.end()
        if position < len(run):
            tokens.add(run[position:])
        # Bigrams over each maximal unspaced segment.
        segment = ""
        for char in run + " ":
            if _UNSPACED_RE.match(char):
                segment += char
                continue
            if len(segment) == 1:
                tokens.add(segment)
            tokens.update(segment[i:i + 2] for i in range(len(segment) - 1))
            segment = ""
    return tokens


def _word_runs(text: str) -> list[str]:
    runs: list[str] = []
    current: list[str] = []
    for char in text:
        if unicodedata.category(char)[0] in "LNM":
            current.append(char)
        elif current:
            runs.append("".join(current))
            current = []
    if current:
        runs.append("".join(current))
    return runs


def _content_terms(text: str) -> set[str]:
    return {token for token in _tokens(text) if token not in _STOPWORDS and len(token) > 2}


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())
