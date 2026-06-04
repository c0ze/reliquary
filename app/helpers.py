"""Stateless helpers for the Mem0 chat/MCP proxy.

Pure request/response/text utilities split out of ``mem0_chat_proxy`` so the
ASGI app module stays focused on routing and orchestration. ``httpx`` is the
only third-party dependency here; everything else is stdlib, so these are
straightforward to unit-test in isolation.
"""

from __future__ import annotations

import json
import math
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

if TYPE_CHECKING:
    import httpx


SEARCH_PREVIEW_CHAR_CAP = 10000
# ChatGPT/OpenAI deep-research connector expects lean search hits (short snippet
# + id/title/url); the full document is pulled via the `fetch` tool.
OPENAI_SNIPPET_CHAR_CAP = 500

# Fields the OpenAI add_memory tool publishes. The server enforces this allowlist
# so a caller can't smuggle user_id / metadata / routing fields past the schema
# (additionalProperties:false is advisory; MCP servers must enforce it).
OPENAI_ADD_MEMORY_FIELDS = ("text", "title", "topic", "source", "infer")
OPENAI_ADD_IMAGE_FIELDS = ("caption", "image_base64", "source_url", "mimetype", "title")
OPENAI_UPDATE_FIELDS = ("id", "text")


def lean_add_memory_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Restrict OpenAI add_memory arguments to the published lean schema."""
    return {key: value for key, value in arguments.items() if key in OPENAI_ADD_MEMORY_FIELDS}


def lean_add_image_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Restrict OpenAI add_image arguments to the published lean schema."""
    return {key: value for key, value in arguments.items() if key in OPENAI_ADD_IMAGE_FIELDS}


def lean_update_args(arguments: dict[str, Any]) -> dict[str, Any]:
    """Restrict OpenAI update arguments to the published lean schema."""
    return {key: value for key, value in arguments.items() if key in OPENAI_UPDATE_FIELDS}


def json_dumps(data: Any) -> bytes:
    # ensure_ascii=False keeps Japanese/Turkish corpus text compact and readable
    # in both the JSON body and the structuredContent the model consumes.
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def decode_headers(scope: dict[str, Any]) -> dict[str, str]:
    return {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}


def parse_form(body: bytes) -> dict[str, str]:
    parsed = parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)
    return {key: values[0] for key, values in parsed.items() if values}


def normalize_base_url(url: str | None) -> str | None:
    if not url:
        return None
    return url.rstrip("/")


def normalize_token(value: Any) -> str | None:
    if value is None:
        return None
    token = str(value).strip()
    return token or None


def extract_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()

    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in (None, "text", "input_text"):
            for key in ("text", "input_text", "content"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    parts.append(value.strip())
                    break
    return "\n".join(parts).strip()


def latest_user_text(messages: list[dict[str, Any]]) -> str:
    collected: list[str] = []
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = extract_text_content(message.get("content"))
        if text:
            collected.append(text)
        if len(collected) >= 2:
            break
    collected.reverse()
    return "\n\n".join(collected).strip()


def extract_assistant_text_from_response(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""

    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    return extract_text_content(content)


def extract_text_from_stream_event(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list):
        return ""

    parts: list[str] = []
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta") or choice.get("message") or {}
        if not isinstance(delta, dict):
            continue
        content = delta.get("content")
        if isinstance(content, str) and content:
            parts.append(content)
        else:
            text = extract_text_content(content)
            if text:
                parts.append(text)
    return "".join(parts)


def coerce_threshold(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None  # reject NaN / Inf


def trim_text(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    if limit <= 3:
        # no room for an ellipsis without exceeding the cap
        return text[: max(0, limit)]
    return text[: limit - 3].rstrip() + "..."


def added_memory_ids(add_result: Any) -> list[str]:
    """Pull every new memory id out of a mem0 ``add()`` result, in order.

    mem0 returns ``{"results": [{"id": ..., "event": "ADD"}, ...]}`` — and with
    infer=True a single write can split into several atomic facts, each with its
    own id. Returning all of them keeps the "the returned id(s) undo the write"
    contract honest (a single-id response would orphan the rest on delete).
    """
    ids: list[str] = []
    if isinstance(add_result, dict):
        results = add_result.get("results")
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict) and item.get("id"):
                    ids.append(str(item["id"]))
    return ids


def format_fetched_document(document: dict[str, Any]) -> str:
    """Render a fetched memory as readable text for the MCP ``content`` block.

    The full document is returned in ``structuredContent``, but clients that
    don't consume it (e.g. the Claude.ai connector surface) only show
    ``content[].text`` to the model — so the body must live there too, not just
    a confirmation line. The MCP tools spec recommends mirroring structured
    content into a text block for exactly this back-compat reason.
    """
    title = str(document.get("title") or document.get("id") or "Document").strip()
    body = str(document.get("text") or "").strip()
    url = str(document.get("url") or "").strip()
    header = f"# {title}"
    if url:
        header += f"\n{url}"
    return f"{header}\n\n{body}".strip() if body else header


def safe_mcp_headers(headers: dict[str, str]) -> dict[str, str]:
    safe_names = {
        "host",
        "user-agent",
        "content-type",
        "origin",
        "x-forwarded-for",
        "x-forwarded-proto",
        "cf-connecting-ip",
        "cf-ray",
        "mcp-session-id",
    }
    return {name: value for name, value in headers.items() if name in safe_names}


def preview_bytes(body: bytes, limit: int = 4000) -> str:
    text = body.decode("utf-8", errors="replace")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "...<truncated>"


def preview_body(text: str, limit: int = SEARCH_PREVIEW_CHAR_CAP) -> tuple[str, bool]:
    text = text.strip()
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - 1)].rstrip() + "…", True


def response_headers_from_httpx(
    upstream_headers: "httpx.Headers",
    extra: dict[str, str] | None = None,
) -> list[tuple[bytes, bytes]]:
    excluded = {"content-length", "transfer-encoding", "connection", "keep-alive"}
    headers: list[tuple[bytes, bytes]] = []
    for key, value in upstream_headers.multi_items():
        if key.lower() in excluded:
            continue
        headers.append((key.encode("latin-1"), value.encode("latin-1")))
    headers.extend(
        [
            (b"access-control-allow-origin", b"*"),
            (
                b"access-control-allow-headers",
                b"authorization, content-type, x-mem0-user-id, mcp-session-id, mcp-protocol-version, origin",
            ),
            (b"access-control-allow-methods", b"GET, POST, DELETE, OPTIONS"),
        ]
    )
    if extra:
        headers.extend((key.encode("latin-1"), value.encode("latin-1")) for key, value in extra.items())
    return headers
