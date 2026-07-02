"""Runtime helpers for the Mem0 chat/MCP proxy.

These are deliberately dependency-free (stdlib + asyncio only) so they can be
unit-tested without the heavy mem0/qdrant/httpx stack.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator


def reads_can_be_concurrent(vector_store: dict[str, Any] | None) -> bool:
    """Decide whether memory *reads* are safe to run concurrently.

    Only a server-backed **Qdrant** is treated as concurrency-safe: provider is
    ``qdrant`` and the config points at a remote (``url`` or ``host``/``port``)
    rather than an on-disk ``path`` (embedded ``QdrantLocal`` is not
    read-thread-safe). Everything else — embedded Qdrant, non-Qdrant providers,
    or anything unrecognized — is treated conservatively (serialized) unless the
    operator explicitly forces concurrency.
    """
    vector_store = vector_store if isinstance(vector_store, dict) else {}
    provider = str(vector_store.get("provider") or "").strip().lower()
    raw_config = vector_store.get("config")
    config = raw_config if isinstance(raw_config, dict) else {}
    if provider != "qdrant":
        return False
    if config.get("path"):
        return False
    return bool(config.get("url") or config.get("host") or config.get("port"))


def native_hybrid_active(mode: str | None, *, has_bm25_slot: bool, bm25_usable: bool) -> bool:
    """Decide whether mem0's server-side BM25 hybrid is active, so reliquary can skip
    its redundant get_all lexical fallback. 'off' forces the fallback (never hybrid);
    'on' asserts hybrid (operator guarantees a bm25 collection + fastembed); 'auto'
    (default) detects from the live collection slot + bm25_usable, where bm25_usable
    means the BM25 encoder actually resolves (not mere importability of fastembed)."""
    m = (mode or "auto").strip().lower()
    if m == "off":
        return False
    if m == "on":
        return True
    return bool(has_bm25_slot and bm25_usable)  # auto


class AsyncRWLock:
    """A writer-preferring readers-writer lock for asyncio.

    Multiple readers may hold the lock at once; a writer holds it exclusively.
    New readers wait while a writer is active or waiting, which prevents writer
    starvation under a steady stream of reads.

    Intended use: parallelize many concurrent memory *reads* (search/count) while
    keeping *writes* (add/writeback) exclusive. This is only safe when the
    underlying store tolerates concurrent reads (e.g. a Qdrant server). For an
    embedded single-process store that is not read-thread-safe, use it like a
    plain mutex by taking ``write()`` everywhere.
    """

    def __init__(self) -> None:
        self._cond = asyncio.Condition()
        self._readers = 0
        self._writer_active = False
        self._writers_waiting = 0

    @asynccontextmanager
    async def read(self) -> AsyncIterator[None]:
        async with self._cond:
            while self._writer_active or self._writers_waiting > 0:
                await self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            async with self._cond:
                self._readers -= 1
                if self._readers == 0:
                    self._cond.notify_all()

    @asynccontextmanager
    async def write(self) -> AsyncIterator[None]:
        async with self._cond:
            self._writers_waiting += 1
            try:
                while self._writer_active or self._readers > 0:
                    await self._cond.wait()
            finally:
                self._writers_waiting -= 1
            self._writer_active = True
        try:
            yield
        finally:
            async with self._cond:
                self._writer_active = False
                self._cond.notify_all()


class MCPSessionStore:
    """Bounded MCP session table with idle-TTL and LRU eviction.

    Replaces a plain FIFO cap, where an old-but-still-active session could be
    evicted out from under a long-running client. Here, accessing a session
    refreshes its recency, so only genuinely idle sessions are dropped once the
    table is full.

    Single-event-loop access only; no internal locking needed.
    """

    def __init__(self, *, max_size: int, ttl: float, clock=time.monotonic, session_store=None) -> None:
        if max_size < 1:
            raise ValueError("max_size must be >= 1")
        self.max_size = max_size
        self.ttl = ttl
        self._clock = clock
        # session_id -> [profile_name, last_seen]; insertion/access order == LRU order
        self._sessions: "dict[str, list]" = {}
        self._session_store = session_store
        if self._session_store is not None:
            for sid, profile_name in self._session_store.load().items():
                if isinstance(sid, str) and isinstance(profile_name, str):
                    self._sessions[sid] = [profile_name, self._clock()]

    def add(self, session_id: str, profile_name: str) -> None:
        self._evict_expired()
        self._sessions.pop(session_id, None)
        self._sessions[session_id] = [profile_name, self._clock()]
        while len(self._sessions) > self.max_size:
            # drop least-recently-used (first inserted/touched)
            oldest = next(iter(self._sessions))
            self._sessions.pop(oldest, None)
        self._persist()

    def touch(self, session_id: str) -> str | None:
        """Return the session's profile if live, refreshing recency; else None."""
        entry = self._sessions.get(session_id)
        if entry is None:
            return None
        if self._is_expired(entry):
            self._sessions.pop(session_id, None)
            # Structural deletion: persist so the stale id isn't resurrected on restart.
            self._persist()
            return None
        entry[1] = self._clock()
        # move to most-recent position
        self._sessions.pop(session_id, None)
        self._sessions[session_id] = entry
        return entry[0]

    def remove(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._persist()

    def __len__(self) -> int:
        self._evict_expired()
        return len(self._sessions)

    def __contains__(self, session_id: str) -> bool:
        return self.touch(session_id) is not None

    def _is_expired(self, entry: list) -> bool:
        return (self._clock() - entry[1]) > self.ttl

    def _evict_expired(self) -> None:
        expired = [sid for sid, entry in self._sessions.items() if self._is_expired(entry)]
        for sid in expired:
            self._sessions.pop(sid, None)
        if expired:
            self._persist()

    def _persist(self) -> None:
        if self._session_store is not None:
            self._session_store.save({sid: entry[0] for sid, entry in self._sessions.items()})
