"""Unit tests for the dependency-free runtime helpers.

Run with: python -m pytest mem0_import/tests/test_runtime.py
(or: python mem0_import/tests/test_runtime.py)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from runtime import AsyncRWLock, MCPSessionStore, reads_can_be_concurrent  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# --------------------------- reads_can_be_concurrent ---------------------------


def test_embedded_local_qdrant_is_serialized():
    # the repo's default: on-disk path -> not read-thread-safe
    assert reads_can_be_concurrent({"provider": "qdrant", "config": {"path": "~/.mem0/qdrant_db"}}) is False


def test_server_qdrant_allows_concurrency():
    assert reads_can_be_concurrent({"provider": "qdrant", "config": {"url": "http://localhost:6333"}}) is True
    assert reads_can_be_concurrent({"provider": "Qdrant", "config": {"host": "qdrant", "port": 6333}}) is True


def test_non_qdrant_remote_is_serialized():
    # a non-Qdrant backend with a remote host must NOT be assumed read-thread-safe
    assert reads_can_be_concurrent({"provider": "chroma", "config": {"host": "chroma", "port": 8000}}) is False
    assert reads_can_be_concurrent({"provider": "pgvector", "config": {"url": "postgres://db"}}) is False
    # server-like config with no provider is also conservative
    assert reads_can_be_concurrent({"config": {"url": "http://localhost:6333"}}) is False


def test_unknown_backend_is_conservative():
    assert reads_can_be_concurrent(None) is False
    assert reads_can_be_concurrent({}) is False
    assert reads_can_be_concurrent({"provider": "qdrant", "config": {}}) is False


# --------------------------- AsyncRWLock ---------------------------


def test_readers_run_concurrently():
    async def scenario():
        lock = AsyncRWLock()
        concurrent = 0
        peak = 0
        started = asyncio.Event()

        async def reader():
            nonlocal concurrent, peak
            async with lock.read():
                concurrent += 1
                peak = max(peak, concurrent)
                started.set()
                await asyncio.sleep(0.02)
                concurrent -= 1

        await asyncio.gather(*(reader() for _ in range(5)))
        return peak

    assert run(scenario()) == 5


def test_writer_is_exclusive_of_readers():
    async def scenario():
        lock = AsyncRWLock()
        events: list[str] = []

        async def reader():
            async with lock.read():
                events.append("r-start")
                await asyncio.sleep(0.05)
                events.append("r-end")

        async def writer():
            # ensure the reader grabs the lock first
            await asyncio.sleep(0.01)
            async with lock.write():
                events.append("w-start")
                events.append("w-end")

        await asyncio.gather(reader(), writer())
        return events

    events = run(scenario())
    # writer must not start until the reader has fully released
    assert events.index("w-start") > events.index("r-end")


def test_writer_preference_blocks_new_readers():
    async def scenario():
        lock = AsyncRWLock()
        order: list[str] = []

        async def long_reader():
            async with lock.read():
                order.append("r1-start")
                await asyncio.sleep(0.05)
                order.append("r1-end")

        async def writer():
            await asyncio.sleep(0.01)
            async with lock.write():
                order.append("w-start")
                await asyncio.sleep(0.02)
                order.append("w-end")

        async def late_reader():
            await asyncio.sleep(0.02)  # arrives while writer is waiting
            async with lock.read():
                order.append("r2-start")

        await asyncio.gather(long_reader(), writer(), late_reader())
        return order

    order = run(scenario())
    # the late reader must wait for the queued writer to finish
    assert order.index("r2-start") > order.index("w-end")


# --------------------------- MCPSessionStore ---------------------------


def test_add_and_touch():
    store = MCPSessionStore(max_size=4, ttl=100, clock=lambda: 0.0)
    store.add("s1", "claude")
    assert store.touch("s1") == "claude"
    assert "s1" in store
    assert store.touch("missing") is None


def test_idle_ttl_eviction():
    now = [0.0]
    store = MCPSessionStore(max_size=4, ttl=10, clock=lambda: now[0])
    store.add("s1", "openai")
    now[0] = 11.0  # past ttl
    assert store.touch("s1") is None
    assert "s1" not in store


def test_touch_refreshes_recency():
    now = [0.0]
    store = MCPSessionStore(max_size=4, ttl=10, clock=lambda: now[0])
    store.add("s1", "claude")
    now[0] = 8.0
    assert store.touch("s1") == "claude"  # refreshes last_seen to 8.0
    now[0] = 16.0  # 8s after refresh, still within ttl
    assert store.touch("s1") == "claude"


def test_lru_eviction_keeps_active_session():
    now = [0.0]
    store = MCPSessionStore(max_size=2, ttl=1000, clock=lambda: now[0])
    store.add("a", "claude")
    now[0] += 1
    store.add("b", "claude")
    now[0] += 1
    store.touch("a")  # 'a' becomes most-recent; 'b' is now LRU
    now[0] += 1
    store.add("c", "claude")  # over capacity -> evict LRU ('b'), not active 'a'
    assert store.touch("a") == "claude"
    assert store.touch("c") == "claude"
    assert store.touch("b") is None


def test_remove():
    store = MCPSessionStore(max_size=4, ttl=100, clock=lambda: 0.0)
    store.add("s1", "claude")
    store.remove("s1")
    assert store.touch("s1") is None


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
