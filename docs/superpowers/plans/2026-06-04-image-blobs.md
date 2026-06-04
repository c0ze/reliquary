# Reliquary Image Blobs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let agents store binary files (mostly images) in Reliquary and fetch them back, discoverable both by semantic caption search and by a stable blob id.

**Architecture:** A new dependency-light `app/blobs.py` owns a content-addressed filesystem blob store (sha256 ids, host bind-mounted dir, ref-counted dedup, HMAC-signed URLs). `app/server.py` gains three MCP tools (`add_image`, `fetch_image`, `delete_image`), an authed `GET /blobs/{id}` route, and threads a signed blob URL through existing search/fetch. Each image is paired with a normal Mem0 text memory (its caption) so existing semantic search finds it; Mem0/Qdrant stays text-only.

**Tech Stack:** Python 3.12+, ASGI (uvicorn), Mem0, stdlib `hashlib`/`hmac`/`secrets`, pytest.

All paths are relative to the `reliquary/` repo root. Run all commands from there.

---

## File Structure

- **Create `app/blobs.py`** — `BlobStore`, `BlobInfo`, `BlobTooLarge`, `sniff_mimetype`. Filesystem + signing only; no Mem0/Qdrant/server imports. Single responsibility: persist and address blobs.
- **Create `tests/test_blobs.py`** — unit tests for `blobs.py`, dependency-light like `tests/test_helpers.py`.
- **Modify `app/server.py`** — settings fields, arg parsing, `BlobStore` construction, `mcp_tool_result` image support, tool schemas + dispatch + handlers, the `GET /blobs/{id}` route, and blob-URL surfacing in `_enrich_hit`/`_document_url`.
- **Modify `docker-compose.yml`** — host bind-mount for the blob dir.
- **Modify `.env.example`** — document the new env vars.
- **Modify `README.md`** — list the new tools and the blob dir.

---

## Task 1: BlobStore module

**Files:**
- Create: `app/blobs.py`
- Test: `tests/test_blobs.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_blobs.py`:

```python
"""Unit tests for the filesystem blob store."""

from __future__ import annotations

import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import pytest  # noqa: E402

from blobs import BlobStore, BlobTooLarge, sniff_mimetype  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 16


def make_store(tmp_path, **kw) -> BlobStore:
    return BlobStore(blob_dir=str(tmp_path), signing_key=b"test-key", **kw)


def test_sniff_known_and_unknown():
    assert sniff_mimetype(PNG) == ("image/png", "png")
    assert sniff_mimetype(JPG) == ("image/jpeg", "jpg")
    assert sniff_mimetype(WEBP) == ("image/webp", "webp")
    assert sniff_mimetype(b"not an image") == ("application/octet-stream", "bin")


def test_put_get_roundtrip(tmp_path):
    store = make_store(tmp_path)
    info = store.put(PNG)
    assert info.id == hashlib.sha256(PNG).hexdigest()
    assert info.mimetype == "image/png"
    assert info.ext == "png"
    assert info.size == len(PNG)
    assert info.ref_count == 1
    got = store.get(info.id)
    assert got == (PNG, "image/png")


def test_put_uses_caller_mimetype_only_as_fallback(tmp_path):
    store = make_store(tmp_path)
    # Recognisable bytes: caller hint is ignored in favour of the sniff.
    assert store.put(PNG, mimetype="application/x-lie").mimetype == "image/png"
    # Unrecognisable bytes: caller hint is used.
    info = store.put(b"opaque-bytes", mimetype="image/svg+xml")
    assert info.mimetype == "image/svg+xml"


def test_dedup_increments_ref_count(tmp_path):
    store = make_store(tmp_path)
    first = store.put(PNG)
    second = store.put(PNG)
    assert first.id == second.id
    assert second.ref_count == 2
    # Only one set of files on disk.
    assert len(list(Path(tmp_path).rglob("*.png"))) == 1


def test_delete_decrements_then_unlinks(tmp_path):
    store = make_store(tmp_path)
    store.put(PNG)
    store.put(PNG)  # ref_count == 2
    assert store.delete("missing") is None
    blob_id = store.put(PNG).id  # ref_count == 3
    assert store.delete(blob_id) is False  # -> 2, still present
    assert store.delete(blob_id) is False  # -> 1, still present
    assert store.get(blob_id) is not None
    assert store.delete(blob_id) is True   # -> 0, unlinked
    assert store.get(blob_id) is None
    assert list(Path(tmp_path).rglob("*.png")) == []


def test_sign_verify_roundtrip(tmp_path):
    store = make_store(tmp_path)
    exp, sig = store.sign("abc123", ttl_seconds=60)
    assert store.verify("abc123", exp, sig) is True
    assert store.verify("abc123", exp, "tampered") is False
    assert store.verify("other-id", exp, sig) is False


def test_verify_rejects_expired(tmp_path):
    store = make_store(tmp_path)
    past = int(time.time()) - 10
    sig = store._sig("abc123", past)
    assert store.verify("abc123", past, sig) is False


def test_size_cap(tmp_path):
    store = make_store(tmp_path, max_bytes=4)
    with pytest.raises(BlobTooLarge):
        store.put(b"too many bytes")
    # 0 disables the cap.
    nolimit = make_store(tmp_path, max_bytes=0)
    assert nolimit.put(b"too many bytes").size == 14
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_blobs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'blobs'`.

- [ ] **Step 3: Implement `app/blobs.py`**

Create `app/blobs.py`:

```python
"""Content-addressed filesystem blob store for Reliquary.

Self-contained: depends only on the stdlib and a configured directory + signing
key. No Mem0/Qdrant/server imports, so it is unit-testable in isolation.

Blobs are addressed by the sha256 of their content (the id), giving natural
dedup and a stable handle. Layout under ``blob_dir`` (sharded by the first two
hex chars of the id to avoid one huge flat directory):

    <blob_dir>/<ab>/<sha256>.<ext>     raw bytes, ext for human browsability
    <blob_dir>/<ab>/<sha256>.json      sidecar: mimetype, size, ext, ref_count, created
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import asdict, dataclass


class BlobTooLarge(Exception):
    """Raised by ``BlobStore.put`` when data exceeds the configured cap."""

    def __init__(self, size: int, max_bytes: int) -> None:
        super().__init__(f"blob is {size} bytes, exceeds max {max_bytes}")
        self.size = size
        self.max_bytes = max_bytes


@dataclass
class BlobInfo:
    id: str
    mimetype: str
    size: int
    ext: str
    ref_count: int
    created: float


# (signature, mimetype, extension). RIFF/WEBP is special-cased in sniff_mimetype.
_MAGIC: list[tuple[bytes, str, str]] = [
    (b"\x89PNG\r\n\x1a\n", "image/png", "png"),
    (b"\xff\xd8\xff", "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
    (b"%PDF-", "application/pdf", "pdf"),
]

# Used to pick an extension when the caller supplies a mimetype we couldn't sniff.
_MIME_EXT: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "application/pdf": "pdf",
    "image/svg+xml": "svg",
}


def sniff_mimetype(data: bytes) -> tuple[str, str]:
    """Return ``(mimetype, ext)`` from magic bytes.

    Falls back to ``("application/octet-stream", "bin")`` for unrecognised data.
    """
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", "webp"
    for sig, mime, ext in _MAGIC:
        if data.startswith(sig):
            return mime, ext
    return "application/octet-stream", "bin"


class BlobStore:
    def __init__(self, blob_dir: str, signing_key: bytes, max_bytes: int = 0) -> None:
        self.blob_dir = blob_dir
        self.signing_key = signing_key
        self.max_bytes = max_bytes
        os.makedirs(self.blob_dir, exist_ok=True)

    # --- paths ---------------------------------------------------------------
    def _shard_dir(self, blob_id: str) -> str:
        return os.path.join(self.blob_dir, blob_id[:2])

    def _blob_path(self, blob_id: str, ext: str) -> str:
        return os.path.join(self._shard_dir(blob_id), f"{blob_id}.{ext}")

    def _sidecar_path(self, blob_id: str) -> str:
        return os.path.join(self._shard_dir(blob_id), f"{blob_id}.json")

    # --- sidecar -------------------------------------------------------------
    def info(self, blob_id: str) -> BlobInfo | None:
        try:
            with open(self._sidecar_path(blob_id), "r", encoding="utf-8") as fh:
                return BlobInfo(**json.load(fh))
        except (FileNotFoundError, ValueError, TypeError):
            return None

    def _write_sidecar(self, info: BlobInfo) -> None:
        path = self._sidecar_path(info.id)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(asdict(info), fh)
        os.replace(tmp, path)

    # --- store / fetch -------------------------------------------------------
    def put(self, data: bytes, *, mimetype: str | None = None) -> BlobInfo:
        if self.max_bytes and len(data) > self.max_bytes:
            raise BlobTooLarge(len(data), self.max_bytes)

        blob_id = hashlib.sha256(data).hexdigest()

        existing = self.info(blob_id)
        if existing is not None:
            existing.ref_count += 1
            self._write_sidecar(existing)
            return existing

        sniffed_mime, ext = sniff_mimetype(data)
        if sniffed_mime == "application/octet-stream" and mimetype:
            final_mime = mimetype
            ext = _MIME_EXT.get(mimetype, "bin")
        else:
            final_mime = sniffed_mime

        os.makedirs(self._shard_dir(blob_id), exist_ok=True)
        blob_path = self._blob_path(blob_id, ext)
        tmp = f"{blob_path}.tmp"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, blob_path)

        info = BlobInfo(
            id=blob_id,
            mimetype=final_mime,
            size=len(data),
            ext=ext,
            ref_count=1,
            created=time.time(),
        )
        self._write_sidecar(info)
        return info

    def get(self, blob_id: str) -> tuple[bytes, str] | None:
        info = self.info(blob_id)
        if info is None:
            return None
        try:
            with open(self._blob_path(blob_id, info.ext), "rb") as fh:
                return fh.read(), info.mimetype
        except FileNotFoundError:
            return None

    def delete(self, blob_id: str) -> bool | None:
        """Decrement the ref count; unlink bytes + sidecar when it reaches zero.

        Returns ``None`` if unknown, ``True`` if the blob was unlinked, ``False``
        if it was only decremented (still referenced).
        """
        info = self.info(blob_id)
        if info is None:
            return None
        info.ref_count -= 1
        if info.ref_count > 0:
            self._write_sidecar(info)
            return False
        for path in (self._blob_path(blob_id, info.ext), self._sidecar_path(blob_id)):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        return True

    # --- signed URLs ---------------------------------------------------------
    def _sig(self, blob_id: str, exp: int) -> str:
        msg = f"{blob_id}|{exp}".encode("utf-8")
        return hmac.new(self.signing_key, msg, hashlib.sha256).hexdigest()

    def sign(self, blob_id: str, ttl_seconds: int) -> tuple[int, str]:
        exp = int(time.time()) + ttl_seconds
        return exp, self._sig(blob_id, exp)

    def verify(self, blob_id: str, exp: int, sig: str) -> bool:
        if exp < int(time.time()):
            return False
        return hmac.compare_digest(self._sig(blob_id, exp), sig)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_blobs.py -v`
Expected: PASS (all 8 tests).

- [ ] **Step 5: Commit**

```bash
git add app/blobs.py tests/test_blobs.py
git commit -m "feat: content-addressed blob store with signed URLs"
```

---

## Task 2: Settings, arg parsing, and startup wiring

**Files:**
- Modify: `app/server.py` (ProxySettings ~124-149, build_settings ~1796-1821, parse_args ~1884-1895, __init__ ~196, imports ~22-25)

- [ ] **Step 1: Add the import**

In `app/server.py`, after the existing local imports (the `from runtime import …` line at ~25), add:

```python
from blobs import BlobStore
```

- [ ] **Step 2: Add fields to `ProxySettings`**

In the `ProxySettings` dataclass (~124), after `oauth_verbatim_token: bool`, add:

```python
    blob_dir: str
    blob_signing_key: str | None
    blob_max_bytes: int
    blob_url_ttl: int
```

- [ ] **Step 3: Populate them in `build_settings`**

In `build_settings` (~1796), inside the `return ProxySettings(` call, after `oauth_verbatim_token=args.oauth_verbatim_token,`, add:

```python
        blob_dir=args.blob_dir,
        blob_signing_key=normalize_token(args.blob_signing_key),
        blob_max_bytes=args.blob_max_bytes,
        blob_url_ttl=args.blob_url_ttl,
```

- [ ] **Step 4: Add the CLI args**

In `parse_args` (~1884), just before `parser.add_argument("--log-level", …)`, add:

```python
    parser.add_argument(
        "--blob-dir",
        default=os.getenv("MEM0_BLOB_DIR", "/data/blobs"),
        help="Directory for stored binary blobs (images, etc.). Bind-mount this for host access.",
    )
    parser.add_argument(
        "--blob-signing-key",
        default=os.getenv("MEM0_BLOB_SIGNING_KEY"),
        help="HMAC key for signed blob URLs. Unset = random per-process key (URLs break on restart).",
    )
    parser.add_argument(
        "--blob-max-bytes",
        type=int,
        default=int(os.getenv("MEM0_BLOB_MAX_BYTES", str(30 * 1024 * 1024))),
        help="Max blob size in bytes (0 disables the cap). Default 30 MB.",
    )
    parser.add_argument(
        "--blob-url-ttl",
        type=int,
        default=int(os.getenv("MEM0_BLOB_URL_TTL", "3600")),
        help="Lifetime in seconds of signed blob URLs. Default 3600.",
    )
```

- [ ] **Step 5: Construct the `BlobStore` in `__init__`**

In `Mem0ChatProxy.__init__`, after the `self.oauth = OAuthProvider(…)` block (~210), add:

```python
        self.blobs = BlobStore(
            blob_dir=settings.blob_dir,
            signing_key=(settings.blob_signing_key or secrets.token_hex(32)).encode("utf-8"),
            max_bytes=settings.blob_max_bytes,
        )
        if not settings.blob_signing_key:
            LOG.warning(
                "MEM0_BLOB_SIGNING_KEY is unset; using a random per-process key. "
                "Signed blob URLs will invalidate on restart."
            )
```

- [ ] **Step 6: Verify it compiles and the server constructs**

Run: `python -m py_compile app/*.py`
Expected: no output (success).

Run: `python -m pytest tests/test_blobs.py tests/test_helpers.py -v`
Expected: PASS (no regressions).

- [ ] **Step 7: Commit**

```bash
git add app/server.py
git commit -m "feat: wire BlobStore settings and construction into the server"
```

---

## Task 3: Image content in `mcp_tool_result`

**Files:**
- Modify: `app/server.py` (`mcp_tool_result` ~1408-1414)

- [ ] **Step 1: Extend `mcp_tool_result`**

Replace the `mcp_tool_result` static method (~1408) with:

```python
    @staticmethod
    def mcp_tool_result(
        *,
        text: str,
        structured: dict[str, Any],
        is_error: bool = False,
        image: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if image is not None:
            data, mimetype = image
            content.append({"type": "image", "data": data, "mimeType": mimetype})
        return {
            "content": content,
            "structuredContent": structured,
            "isError": is_error,
        }
```

- [ ] **Step 2: Verify existing callers still work**

Run: `python -m py_compile app/server.py`
Expected: no output.

Run: `python -m pytest tests/ -v`
Expected: PASS (the new `image` param defaults to `None`, so all current calls are unaffected).

- [ ] **Step 3: Commit**

```bash
git add app/server.py
git commit -m "feat: allow an image content block in mcp_tool_result"
```

---

## Task 4: Signed blob URL helper + `GET /blobs/{id}` route

**Files:**
- Modify: `app/server.py` (module regex near ~67, router in `__call__` ~331, a new `handle_blob_get` method, a `_signed_blob_url` helper)

- [ ] **Step 1: Add a path regex near the other module-level regex**

After the `ICON_PATH_RE = …` line (~67), add:

```python
BLOB_PATH_RE = re.compile(r"/blobs/([0-9a-f]{64})\Z")
```

- [ ] **Step 2: Add a signed-URL helper method**

Add this method to `Mem0ChatProxy` (place it just above `_require_claude_auth` ~1353):

```python
    def _signed_blob_url(self, blob_id: str) -> str:
        exp, sig = self.blobs.sign(blob_id, self.settings.blob_url_ttl)
        return f"/blobs/{blob_id}?exp={exp}&sig={sig}"
```

- [ ] **Step 3: Add the route handler method**

Add this method to `Mem0ChatProxy` (place it just below `send_asset` ~1741):

```python
    async def handle_blob_get(self, blob_id: str, scope: dict[str, Any], send) -> None:
        # Authorize via a valid signed query OR the Claude bearer.
        query = parse_qs(scope.get("query_string", b"").decode("utf-8", "replace"))
        exp_raw = query.get("exp", [""])[0]
        sig = query.get("sig", [""])[0]
        authorized = False
        try:
            authorized = bool(sig) and self.blobs.verify(blob_id, int(exp_raw), sig)
        except (TypeError, ValueError):
            authorized = False
        if not authorized and not self._require_claude_auth(decode_headers(scope)):
            await self.send_json(send, 403, {"error": "Invalid or expired blob signature"})
            return
        result = self.blobs.get(blob_id)
        if result is None:
            await self.send_json(send, 404, {"error": f"No blob for id={blob_id}"})
            return
        data, mimetype = result
        headers = [
            (b"content-type", mimetype.encode("latin-1", "replace")),
            (b"cache-control", b"private, max-age=86400"),
            (b"content-length", str(len(data)).encode("latin-1")),
        ]
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        await send({"type": "http.response.body", "body": data, "more_body": False})
```

- [ ] **Step 4: Register the route in `__call__`**

In `__call__`, just before the `profile = self.endpoint_profiles.get(path)` line (~331), add:

```python
            blob_match = BLOB_PATH_RE.fullmatch(path)
            if method == "GET" and blob_match:
                await self.handle_blob_get(blob_match.group(1), scope, send)
                return
```

- [ ] **Step 5: Verify it compiles**

Run: `python -m py_compile app/server.py`
Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add app/server.py
git commit -m "feat: add authed GET /blobs/{id} route with signed-URL access"
```

---

## Task 5: `add_image` tool

**Files:**
- Modify: `app/server.py` (`mcp_tools_for` openai branch ~677-723 and claude list ~726-804, `call_mcp_tool` openai ~808-841 and claude ~843-877, new `handle_add_image_tool` method near `handle_add_memory_tool` ~1031)

- [ ] **Step 1: Add the `add_image` schema to the Claude tool list**

In `mcp_tools_for`, in the Claude (`return [ … ]`) list, after the `mem0_delete` tool dict (~803), add this dict as a new list element:

```python
            {
                "name": "add_image",
                "title": "Add Image",
                "description": "Store a binary file (usually an image) and a searchable caption. "
                "Returns blob_id, memory_id and a signed url. Find it later via mem0_search on the "
                "caption, or fetch_image with the blob_id.",
                "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "caption": {"type": "string", "description": "Searchable text describing the image."},
                        "image_base64": {"type": "string", "description": "Base64-encoded file bytes."},
                        "mimetype": {"type": "string", "description": "Optional fallback mimetype if bytes can't be sniffed."},
                        "user_id": {"type": "string"},
                        "title": {"type": "string"},
                        "domain": {"type": "string"},
                        "hall": {"type": "string"},
                        "room": {"type": "string"},
                        "topic": {"type": "string"},
                        "metadata": {"type": "object"},
                    },
                    "required": ["caption", "image_base64"],
                    "additionalProperties": False,
                },
            },
```

- [ ] **Step 2: Add the `add_image` schema to the OpenAI write branch**

In `mcp_tools_for`, inside `if profile.allow_write:` for the openai branch (after the `delete` append ~723), add:

```python
                tools.append(
                    {
                        "name": "add_image",
                        "title": "Add Image",
                        "description": "Store a binary file (usually an image) plus a searchable caption.",
                        "annotations": {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "caption": {"type": "string", "description": "Searchable text describing the image."},
                                "image_base64": {"type": "string", "description": "Base64-encoded file bytes."},
                                "mimetype": {"type": "string"},
                                "title": {"type": "string"},
                            },
                            "required": ["caption", "image_base64"],
                            "additionalProperties": False,
                        },
                    }
                )
```

- [ ] **Step 3: Add the `handle_add_image_tool` method**

Add this method to `Mem0ChatProxy`, placed right after `handle_add_memory_tool` (~1068):

```python
    async def handle_add_image_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        caption = str(arguments.get("caption") or "").strip()
        if not caption:
            return self.mcp_tool_result(
                text="A non-empty `caption` is required.",
                structured={"error": "missing_caption"},
                is_error=True,
            )
        image_b64 = arguments.get("image_base64")
        if not isinstance(image_b64, str) or not image_b64.strip():
            return self.mcp_tool_result(
                text="A non-empty `image_base64` is required.",
                structured={"error": "missing_image"},
                is_error=True,
            )
        try:
            data = base64.b64decode(image_b64, validate=True)
        except (ValueError, base64.binascii.Error):
            return self.mcp_tool_result(
                text="`image_base64` is not valid base64.",
                structured={"error": "invalid_image"},
                is_error=True,
            )
        if not data:
            return self.mcp_tool_result(
                text="Decoded image is empty.",
                structured={"error": "invalid_image"},
                is_error=True,
            )
        try:
            info = self.blobs.put(data, mimetype=arguments.get("mimetype"))
        except BlobTooLarge as exc:
            return self.mcp_tool_result(
                text=f"Image is {exc.size} bytes, exceeds the {exc.max_bytes}-byte limit.",
                structured={"error": "too_large", "size": exc.size, "max_bytes": exc.max_bytes},
                is_error=True,
            )

        user_id = str(arguments.get("user_id") or self.settings.user_id)
        metadata = arguments.get("metadata") or {}
        if not isinstance(metadata, dict):
            return self.mcp_tool_result(
                text="`metadata` must be an object.",
                structured={"error": "invalid_metadata"},
                is_error=True,
            )
        metadata.setdefault("source", "mcp")
        metadata["kind"] = "image"
        metadata["source_group"] = "user-write"
        metadata["blob_ref"] = info.id
        metadata["blob_mime"] = info.mimetype
        metadata["blob_size"] = info.size
        for key in ("title", "domain", "hall", "room", "topic"):
            value = arguments.get(key)
            if value is not None and str(value).strip():
                metadata[key] = str(value).strip()

        result = await self.add_memory(caption, user_id=user_id, metadata=metadata, infer=False)
        new_ids = added_memory_ids(result)
        memory_id = new_ids[0] if new_ids else None
        url = self._signed_blob_url(info.id)
        return self.mcp_tool_result(
            text=f"Stored image (blob_id={info.id}, memory_id={memory_id}): {trim_text(caption, 160)}",
            structured={
                "blob_id": info.id,
                "memory_id": memory_id,
                "url": url,
                "mimetype": info.mimetype,
                "size": info.size,
                "user_id": user_id,
            },
        )
```

- [ ] **Step 4: Dispatch `add_image` on the Claude endpoint**

In `call_mcp_tool`, in the Claude branch, after the `mem0_delete` handling block (~877, just before the closing `except Exception as exc:`), add:

```python
            if tool_name == "add_image":
                if not profile.allow_write:
                    return self.mcp_tool_result(
                        text=f"Tool {tool_name} is not available on the {profile.name} endpoint.",
                        structured={"error": "read_only_endpoint"},
                        is_error=True,
                    )
                return await self.handle_add_image_tool(arguments)
```

- [ ] **Step 5: Dispatch `add_image` on the OpenAI endpoint**

In `call_mcp_tool`, in the `if profile.name == "openai":` branch, after the `if tool_name == "delete":` block (~837, before the `Unknown tool` return at ~838), add:

```python
                if tool_name == "add_image":
                    if not profile.allow_write:
                        return self.mcp_tool_result(
                            text="add_image is not available on this endpoint.",
                            structured={"error": "read_only_endpoint"},
                            is_error=True,
                        )
                    return await self.handle_add_image_tool(arguments)
```

- [ ] **Step 6: Add a module-level import note**

`BlobTooLarge` must be importable. Update the `from blobs import BlobStore` line (added in Task 2 Step 1) to:

```python
from blobs import BlobStore, BlobTooLarge
```

- [ ] **Step 7: Verify it compiles**

Run: `python -m py_compile app/server.py`
Expected: no output.

- [ ] **Step 8: Commit**

```bash
git add app/server.py
git commit -m "feat: add_image MCP tool (base64 in, blob + caption memory out)"
```

---

## Task 6: `fetch_image` tool

**Files:**
- Modify: `app/server.py` (`mcp_tools_for` both branches, `call_mcp_tool` both branches, new `handle_fetch_image_tool` method)

- [ ] **Step 1: Add the `fetch_image` schema to the Claude tool list**

In `mcp_tools_for`, in the Claude list, after the `add_image` dict added in Task 5 Step 1, add:

```python
            {
                "name": "fetch_image",
                "title": "Fetch Image",
                "description": "Fetch a stored binary file by blob_id. Returns the image inline plus a "
                "signed url for direct download of large files.",
                "annotations": {"readOnlyHint": True, "openWorldHint": False},
                "inputSchema": {
                    "type": "object",
                    "properties": {"id": {"type": "string", "description": "blob_id from add_image or mem0_search."}},
                    "required": ["id"],
                    "additionalProperties": False,
                },
            },
```

- [ ] **Step 2: Add the `fetch_image` schema to the OpenAI tool list**

In `mcp_tools_for`, in the openai `tools = [ … ]` list (the read-only tools, after the `fetch` dict ~675), add a new element:

```python
                {
                    "name": "fetch_image",
                    "title": "Fetch Image",
                    "description": "Fetch a stored binary file by blob_id; returns it inline plus a signed url.",
                    "annotations": read_only,
                    "inputSchema": {
                        "type": "object",
                        "properties": {"id": {"type": "string", "description": "blob_id from search results."}},
                        "required": ["id"],
                        "additionalProperties": False,
                    },
                },
```

- [ ] **Step 3: Add the `handle_fetch_image_tool` method**

Add this method right after `handle_add_image_tool`:

```python
    async def handle_fetch_image_tool(self, arguments: dict[str, Any]) -> dict[str, Any]:
        blob_id = str(arguments.get("id") or "").strip()
        if not blob_id:
            return self.mcp_tool_result(
                text="A non-empty `id` is required.",
                structured={"error": "missing_id"},
                is_error=True,
            )
        result = self.blobs.get(blob_id)
        if result is None:
            return self.mcp_tool_result(
                text=f"No blob found for id={blob_id}.",
                structured={"error": "not_found", "id": blob_id},
                is_error=True,
            )
        data, mimetype = result
        encoded = base64.b64encode(data).decode("ascii")
        url = self._signed_blob_url(blob_id)
        return self.mcp_tool_result(
            text=f"Image {blob_id} ({mimetype}, {len(data)} bytes). Download: {url}",
            structured={"blob_id": blob_id, "url": url, "mimetype": mimetype, "size": len(data)},
            image=(encoded, mimetype),
        )
```

- [ ] **Step 4: Dispatch `fetch_image` on the Claude endpoint**

In `call_mcp_tool`, Claude branch, after the `if tool_name == "mem0_fetch":` block (~861), add:

```python
            if tool_name == "fetch_image":
                return await self.handle_fetch_image_tool(arguments)
```

- [ ] **Step 5: Dispatch `fetch_image` on the OpenAI endpoint**

In `call_mcp_tool`, openai branch, after the `if tool_name == "fetch":` block (~819), add:

```python
                if tool_name == "fetch_image":
                    return await self.handle_fetch_image_tool(arguments)
```

- [ ] **Step 6: Verify it compiles**

Run: `python -m py_compile app/server.py`
Expected: no output.

- [ ] **Step 7: Commit**

```bash
git add app/server.py
git commit -m "feat: fetch_image MCP tool (inline image + signed download url)"
```

---

## Task 7: `delete_image` tool

**Files:**
- Modify: `app/server.py` (`mcp_tools_for` Claude list, `call_mcp_tool` Claude branch, new `handle_delete_image_tool` method)

- [ ] **Step 1: Add the `delete_image` schema to the Claude tool list**

In `mcp_tools_for`, in the Claude list, after the `fetch_image` dict, add:

```python
            {
                "name": "delete_image",
                "title": "Delete Image",
                "description": "Delete an image you stored via add_image, by its memory_id. Removes the "
                "caption memory and unlinks the blob when no other memory references it.",
                "annotations": {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "memory_id": {"type": "string", "description": "memory_id returned by add_image."},
                        "user_id": {"type": "string", "description": "Optional Mem0 user_id override (must own the record)."},
                    },
                    "required": ["memory_id"],
                    "additionalProperties": False,
                },
            },
```

- [ ] **Step 2: Add the `handle_delete_image_tool` method**

Add this method right after `handle_fetch_image_tool`. It reuses the same ownership guards as `handle_delete_tool`:

```python
    async def handle_delete_image_tool(self, arguments: dict[str, Any], *, allow_user_id: bool = False) -> dict[str, Any]:
        memory_id = str(arguments.get("memory_id") or "").strip()
        if not memory_id:
            return self.mcp_tool_result(
                text="A non-empty `memory_id` is required.",
                structured={"error": "missing_id"},
                is_error=True,
            )
        effective_user_id = (
            str(arguments.get("user_id") or self.settings.user_id) if allow_user_id else self.settings.user_id
        )
        existing = await self.fetch_live_memory(memory_id)
        if existing is None:
            return self.mcp_tool_result(
                text=f"No deletable image found for memory_id={memory_id}.",
                structured={"error": "not_found", "id": memory_id},
                is_error=True,
            )
        if existing.get("user_id") != effective_user_id:
            return self.mcp_tool_result(
                text=f"No deletable image found for memory_id={memory_id} under user_id={effective_user_id}.",
                structured={"error": "not_found", "id": memory_id},
                is_error=True,
            )
        metadata = existing.get("metadata") or {}
        if metadata.get("source_group") != "user-write":
            return self.mcp_tool_result(
                text=f"Refusing to delete memory_id={memory_id}: not a user-written memory.",
                structured={"error": "protected_record", "id": memory_id},
                is_error=True,
            )
        blob_ref = metadata.get("blob_ref")
        if not blob_ref:
            return self.mcp_tool_result(
                text=f"memory_id={memory_id} is not an image (no blob_ref).",
                structured={"error": "not_an_image", "id": memory_id},
                is_error=True,
            )
        await self.delete_memory(memory_id)
        unlinked = self.blobs.delete(str(blob_ref))
        return self.mcp_tool_result(
            text=f"Deleted image memory_id={memory_id} (blob_id={blob_ref}, blob_unlinked={bool(unlinked)}).",
            structured={
                "deleted": True,
                "memory_id": memory_id,
                "blob_id": blob_ref,
                "blob_unlinked": bool(unlinked),
            },
        )
```

Note: `fetch_live_memory` (~1141) returns a dict whose `user_id` comes from Mem0's `get`; it already includes `metadata`. The `existing.get("user_id")` check mirrors `handle_delete_tool`.

- [ ] **Step 3: Confirm `fetch_live_memory` exposes `user_id`**

`fetch_live_memory` (server.py:1161) already returns `"user_id": str(result.get("user_id") or "") or None`, which is what `handle_delete_tool` relies on — so the ownership guard in `handle_delete_image_tool` works as written. No change needed; this step is just a confirmation.

Run: `grep -n '"user_id": str(result' app/server.py`
Expected: one match inside `fetch_live_memory`.

- [ ] **Step 4: Dispatch `delete_image` on the Claude endpoint**

In `call_mcp_tool`, Claude branch, after the `add_image` dispatch block from Task 5 Step 4, add:

```python
            if tool_name == "delete_image":
                if not profile.allow_write:
                    return self.mcp_tool_result(
                        text=f"Tool {tool_name} is not available on the {profile.name} endpoint.",
                        structured={"error": "read_only_endpoint"},
                        is_error=True,
                    )
                return await self.handle_delete_image_tool(arguments, allow_user_id=True)
```

- [ ] **Step 5: Verify it compiles**

Run: `python -m py_compile app/server.py`
Expected: no output.

- [ ] **Step 6: Commit**

```bash
git add app/server.py
git commit -m "feat: delete_image MCP tool (caption memory + ref-counted blob)"
```

---

## Task 8: Surface the signed blob URL in search and fetch

**Files:**
- Modify: `app/server.py` (`_enrich_hit` ~1115-1130, `_document_url` ~1132-1139)

- [ ] **Step 1: Emit a signed blob URL from `_document_url`**

Replace the `_document_url` method (~1132) with a version that prefers a signed blob URL when the metadata carries a `blob_ref`:

```python
    def _document_url(self, record_id: str, metadata: dict[str, Any]) -> str:
        blob_ref = metadata.get("blob_ref")
        if isinstance(blob_ref, str) and blob_ref:
            return self._signed_blob_url(blob_ref)
        source_url = metadata.get("source_url")
        if isinstance(source_url, str) and source_url.startswith(("http://", "https://")):
            return source_url
        source_ref = metadata.get("source_ref")
        if isinstance(source_ref, str) and source_ref.startswith(("http://", "https://")):
            return source_ref
        return f"mem0://record/{record_id}"
```

`_enrich_hit` and `fetch_live_memory` already call `_document_url`, so search hits and `mem0_fetch` results for image memories will now carry a working signed URL with no further change. The `blob_ref`/`blob_mime` keys already live in the emitted `metadata` because they were stored on the memory.

- [ ] **Step 2: Verify it compiles**

Run: `python -m py_compile app/server.py`
Expected: no output.

- [ ] **Step 3: Run the full suite**

Run: `python -m pytest tests/ -v`
Expected: PASS. (No existing test stores a `blob_ref`, so existing URL behaviour is unchanged.)

- [ ] **Step 4: Commit**

```bash
git add app/server.py
git commit -m "feat: surface signed blob URLs in search and fetch results"
```

---

## Task 9: Deployment config and docs

**Files:**
- Modify: `docker-compose.yml` (app service volumes ~56-57)
- Modify: `.env.example`
- Modify: `README.md` (MCP endpoints/tools table)

- [ ] **Step 1: Add the host bind-mount to the app service**

In `docker-compose.yml`, under the `app:` service `volumes:` (~56), add the blob mount so the existing block reads:

```yaml
    volumes:
      - ./config.yaml:/config/config.yaml:ro
      - ${BLOB_HOST_DIR:-./data/blobs}:/data/blobs
```

- [ ] **Step 2: Document the new env vars**

Append to `.env.example`:

```
# --- Binary blobs (images, etc.) ---
# Host directory where saved blobs live. Browse/back up this path directly.
BLOB_HOST_DIR=./data/blobs
# Max upload size in bytes (0 disables the cap). Default 30 MB.
MEM0_BLOB_MAX_BYTES=31457280
# HMAC key for signed blob URLs. Blank = random per-process key (URLs break on
# restart); set a fixed value for stable, shareable URLs.
MEM0_BLOB_SIGNING_KEY=
# Signed blob URL lifetime in seconds.
MEM0_BLOB_URL_TTL=3600
```

- [ ] **Step 3: Update the README tools table**

In `README.md`, in the `## MCP endpoints & tools` table, update the tools cell for the Claude row to append `, add_image, fetch_image, delete_image`, and for the OpenAI row append `; fetch_image` (and `add_image` under the write-gate note). Then add a short paragraph after the table:

```markdown
**Binary blobs.** `add_image` stores a file (base64) plus a searchable caption;
it returns a `blob_id`, a `memory_id`, and a signed `url`. Find images later with
`mem0_search` on the caption or `fetch_image` by `blob_id`; `delete_image` removes
the caption memory and ref-counted blob. Files live under `BLOB_HOST_DIR` on the
host (default `./data/blobs`) so you can browse and back them up. `GET /blobs/{id}`
serves bytes to anyone holding a valid signed URL or the Claude bearer.
```

- [ ] **Step 4: Verify compose is valid**

Run: `docker compose config -q`
Expected: no output (valid). If Docker is unavailable, skip and note it.

- [ ] **Step 5: Commit**

```bash
git add docker-compose.yml .env.example README.md
git commit -m "docs: blob storage compose mount, env vars, and README tools"
```

---

## Self-review notes (addressed)

- **Spec coverage:** BlobStore + dedup/sign (Task 1); 30 MB cap + host mount + settings (Tasks 2, 9); `add_image`/`fetch_image`/`delete_image` (Tasks 5–7); signed URL escape hatch + `GET /blobs/{id}` (Task 4); search/fetch discovery (Task 8); image content block (Task 3). All spec sections map to a task.
- **Type consistency:** `BlobStore.put/get/delete/sign/verify/info`, `BlobInfo(id, mimetype, size, ext, ref_count, created)`, and `_signed_blob_url` are used with identical signatures across tasks. `delete` returns `bool | None`; callers coerce with `bool(...)`.
- **Endpoint gating:** `add_image`/`delete_image` follow `add_memory`/`delete` write-gating; `fetch_image` is read-available on both endpoints, matching the spec.
- **Verification dependency:** Task 7 Step 3 explicitly checks that `fetch_live_memory` returns `user_id` before the delete guard relies on it.
