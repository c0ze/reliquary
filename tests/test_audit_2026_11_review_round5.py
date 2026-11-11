"""Round 5 of the November 2026 audit: the upload-slot ownership invariant.

A model-based harness drives the real proxy (in-memory FakeMemory with
injectable failures, real blob store and state files) through randomized and
exhaustively enumerated event sequences on one or two upload slots that may
share bytes, and checks after every step:

(a) every live caption that references a blob can fetch its bytes;
(b) no sidecar's ref_count is below its number of owners, and no owner names a
    memory that was deleted;
(c) a slot never reports committed (or already committed) without a caption
    carrying its upload_id having been written, and a successful commit's
    caption owns the blob;
(d) nothing escapes but the injected crash, and upload POSTs answer 2xx/4xx.

A failure prints the event sequence that produced it.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import itertools
import json
import logging
import os
import random
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from conftest import FakeMemory  # noqa: E402
from runtime import drain_on_cancel  # noqa: E402

# Captured at import, before the fixtures swap in their inline stand-in.
REAL_TO_THREAD = asyncio.to_thread

PNG = b"\x89PNG\r\n\x1a\n" + b"round5-model"
USER = "my_lord"
OTHER_USER = "other_user"


class Crash(BaseException):
    """The process dies at this point."""


class ChaosMemory(FakeMemory):
    """FakeMemory with Mem0 2.0.4's failure shapes injectable, and a record of
    everything ever written and deleted (it survives 'restarts': it is the
    vector store)."""

    def __init__(self) -> None:
        super().__init__()
        self.down = False
        self.next_add: str | None = None  # "before" | "after"
        self.next_delete_fails = False
        self.ever: dict[str, dict] = {}
        self.deleted: set[str] = set()

    def _check(self):
        if self.down:
            raise ConnectionError("memory backend down")

    def search(self, *args, **kwargs):
        self._check()
        return super().search(*args, **kwargs)

    def get(self, memory_id):
        self._check()
        return super().get(memory_id)

    def get_all(self, user_id=None, **kwargs):
        self._check()
        return super().get_all(user_id=user_id, **kwargs)

    def add(self, text, **kwargs):
        self._check()
        mode, self.next_add = self.next_add, None
        if mode == "before":
            raise RuntimeError("embedder unavailable")
        result = super().add(text, **kwargs)
        memory_id = result["results"][0]["id"]
        self.ever[memory_id] = dict(self._store[memory_id]["metadata"])
        if mode == "after":
            raise RuntimeError("history database is locked")  # the vector already landed
        return result

    def delete(self, memory_id):
        self._check()
        if self.next_delete_fails:
            self.next_delete_fails = False
            raise ConnectionError("delete timed out")
        super().delete(memory_id)
        self.deleted.add(memory_id)

    def update(self, *args, **kwargs):
        self._check()
        return super().update(*args, **kwargs)


# "commit" or "settle": that kind of lookup, started from this context, waits
# for the model's gate after it has read the backend (a slow lookup whose result
# arrives late). Tasks inherit it, so the caller is checked too.
SLOW = contextvars.ContextVar("slow", default=None)
GATED_CALLERS = {"commit": {"_commit_upload", "handle_commit_image_upload_tool"},
                 "settle": {"_settle_expired_uploads"}}


class Violation(AssertionError):
    pass


class Model:
    """One sequence run: a fresh state/blob directory, a ChaosMemory that
    outlives restarts, and the current proxy."""

    def __init__(self, base: Path, server_module, settings_cls):
        self.base = base
        self.server = server_module
        self.settings_cls = settings_cls
        self.memory = ChaosMemory()
        self.clock = [time.time()]
        self.slots: dict[str, str] = {}      # "A"/"B" -> upload_id
        self.log: list[str] = []
        self.gate = asyncio.Event()
        self.gates = [self.gate]
        self.background: list[asyncio.Task] = []
        self.orphans: list[asyncio.Task] = []  # tasks of a dead process, blocked for good
        self.dead: list = []
        self.crash_next_transfer = False
        self.crash_next_drop = False
        self.fail_next_transfer = False
        self.fail_next_save = False
        cfg = base / "config.json"
        cfg.write_text(json.dumps({
            "vector_store": {"provider": "qdrant", "config": {"host": "x", "port": 6333}},
            "embedder": {"provider": "openai", "config": {"model": "m"}}}))
        self.cfg = str(cfg)
        self.proxy = self._new_proxy()

    # --- construction / restart -------------------------------------------------
    def _new_proxy(self):
        settings = self.settings_cls(
            config_path=self.cfg, user_id=USER, claude_token="claude-secret", openai_token="openai-secret",
            blob_dir=str(self.base / "blobs"), blob_signing_key="k", compiled_dir=str(self.base / "compiled"),
            state_dir=str(self.base / "state"))
        proxy = self.server.ReliquaryChatProxy(settings, memory=self.memory, compiled_memory=FakeMemory())
        original = proxy._look_up_slot
        model = self

        async def gated(upload, *args, **kwargs):
            caller = sys._getframe(1).f_code.co_name
            result = await original(upload, *args, **kwargs)
            if caller in GATED_CALLERS.get(SLOW.get(), ()):
                # Drained like the real worker: cancelling the caller does not
                # end the wait (a thread that cannot be stopped).
                await drain_on_cancel(model.gate.wait())
            return result

        proxy._look_up_slot = gated
        real_transfer = proxy.blobs.transfer_owner

        def transfer(*args, **kwargs):
            # Only a commit's own hand-over crashes (the injected process death).
            in_commit = sys._getframe(2).f_code.co_name == "_commit_upload"
            if model.crash_next_transfer and not SLOW.get() and in_commit:
                model.crash_next_transfer = False
                raise Crash()
            if model.fail_next_transfer:
                model.fail_next_transfer = False
                raise OSError(28, "No space left on device")
            return real_transfer(*args, **kwargs)

        proxy.blobs.transfer_owner = transfer
        real_drop = proxy._drop_upload

        def drop(*args, **kwargs):
            if model.crash_next_drop and not SLOW.get():
                model.crash_next_drop = False
                raise Crash()
            return real_drop(*args, **kwargs)

        proxy._drop_upload = drop
        store = proxy.pending_upload_store
        real_save = store.save

        def save(data):
            if model.fail_next_save:
                model.fail_next_save = False
                raise OSError(28, "No space left on device")  # then storage recovers
            return real_save(data)

        store.save = save
        return proxy

    async def restart(self, rewrite=None):
        """The process dies (in-flight work never finishes) and starts again."""
        old = self.proxy
        old.pending_upload_store = None  # a dead process writes nothing more
        # Its in-flight work never resumes: tasks blocked on the gate stay blocked
        # (a drained commit cannot be cancelled) and are only reaped at the end.
        tasks = [t for t in self.background] + ([old._reap_task] if old._reap_task else [])
        for task in tasks:
            task.cancel()
        await asyncio.sleep(0)
        self.orphans.extend(t for t in tasks if not t.done())
        self.background = []
        self.gate = asyncio.Event()
        self.gates.append(self.gate)
        self.dead.append(old)
        if rewrite:
            rewrite()
        self.proxy = self._new_proxy()

    # --- helpers ------------------------------------------------------------------
    def profile(self):
        return self.proxy.endpoint_profiles[self.proxy.settings.claude_mcp_path]

    async def tool(self, name, args):
        return await self.proxy.call_mcp_tool(self.profile(), name, args, can_write=True)

    async def post(self, upload_id, data=PNG):
        messages = []

        async def receive():
            return {"type": "http.request", "body": data, "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {"type": "http", "method": "POST", "path": f"/uploads/{upload_id}",
                 "headers": [(b"authorization", b"Bearer claude-secret"), (b"content-type", b"image/png")]}
        await self.proxy.handle_upload_post(upload_id, scope, receive, send)
        status = next(m["status"] for m in messages if m["type"] == "http.response.start")
        if not (200 <= status < 500 or status == 503):
            raise Violation(f"upload POST answered {status}")
        return status

    def caption_of(self, slot):
        upload_id = self.slots.get(slot)
        ids = [mid for mid, md in self.memory.ever.items()
               if md.get("upload_id") == upload_id and mid in self.memory._store]
        return ids[-1] if ids else None

    def check_commit_result(self, slot, upload_id, result):
        sc = result.get("structuredContent") or {}
        if not result.get("isError"):
            memory_id = sc.get("memory_id")
            rec = self.memory._store.get(memory_id)
            if rec is None or rec["metadata"].get("upload_id") != upload_id:
                raise Violation(f"commit of {slot} reported success without its caption")
            info = self.proxy.blobs.info(sc["blob_id"])
            if info is None or memory_id not in info.owners:
                raise Violation(f"commit of {slot} succeeded but its caption does not own the blob")
        elif sc.get("error") == "already_committed":
            if not any(md.get("upload_id") == upload_id for md in self.memory.ever.values()):
                raise Violation(f"slot {slot} reported already_committed but no caption was ever written")

    # --- events -------------------------------------------------------------------
    async def ev_upload(self, slot):
        created = self.proxy.handle_create_image_upload_tool({}, profile=self.profile())
        if created.get("isError"):
            return
        upload_id = created["structuredContent"]["upload_id"]
        self.slots[slot] = upload_id
        await self.post(upload_id)

    async def _commit(self, slot, user):
        upload_id = self.slots.get(slot)
        if upload_id is None:
            return
        args = {"upload_id": upload_id, "caption": f"caption {slot}"}
        if user != USER:
            args["user_id"] = user
        try:
            result = await self.tool("reliquary_commit_image_upload", args)
        except Crash:
            await self.restart()
            return
        self.check_commit_result(slot, upload_id, result)

    async def ev_commit(self, slot):
        await self._commit(slot, USER)

    async def ev_commit_other(self, slot):
        await self._commit(slot, OTHER_USER)

    async def ev_commit_slow(self, slot):
        async def slow():
            SLOW.set("commit")
            await self._commit(slot, USER)
        self.background.append(asyncio.create_task(slow()))

    async def ev_settle(self):
        if self.background:
            self.proxy._schedule_reap()  # a gated pass may be running: do not wait for it
        else:
            await self.proxy._reap_expired_uploads()

    async def ev_settle_slow(self):
        async def slow():
            SLOW.set("settle")
            await self.proxy._reap_expired_uploads()
        self.background.append(asyncio.create_task(slow()))

    async def ev_release(self):
        self.gate.set()
        tasks, self.background = self.background, []
        if self.proxy._reap_task is not None:
            tasks.append(self.proxy._reap_task)
        for outcome in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(outcome, BaseException) and not isinstance(outcome, asyncio.CancelledError):
                raise outcome
        self.gate = asyncio.Event()
        self.gates.append(self.gate)

    async def ev_expire(self):
        self.clock[0] += 3600

    async def ev_delete_image(self, slot):
        caption = self.caption_of(slot)
        if caption:
            user = self.memory._store[caption]["user_id"]
            await self.tool("reliquary_delete_image", {"memory_id": caption, "user_id": user})

    async def ev_delete_generic(self, slot):
        caption = self.caption_of(slot)
        if caption:
            user = self.memory._store[caption]["user_id"]
            await self.tool("reliquary_delete", {"id": caption, "user_id": user})

    async def ev_add_image(self):
        await self.tool("reliquary_add_image", {"caption": "same bytes",
                                                "image_base64": base64.b64encode(PNG).decode()})

    async def ev_restart(self):
        await self.restart()

    async def ev_legacy_restart(self):
        """Restart onto state in the deployed build's format: no upload markers
        in sidecars, no commit record on slots."""
        def rewrite():
            for sidecar in (self.base / "blobs").rglob("*.json"):
                data = json.loads(sidecar.read_text())
                data["owners"] = [o for o in data.get("owners", []) if not str(o).startswith("upload:")]
                sidecar.write_text(json.dumps(data))
            path = self.base / "state" / "pending_uploads.json"
            if path.exists():
                slots = json.loads(path.read_text())
                for rec in slots.values():
                    rec.pop("commit_user_id", None)
                    rec.pop("commit_users", None)
                path.write_text(json.dumps(slots))
        await self.restart(rewrite)

    async def ev_oldfmt_restart(self):
        """Restart onto slots written by a build that marked slots but did not
        record the committing user."""
        def rewrite():
            path = self.base / "state" / "pending_uploads.json"
            if path.exists():
                slots = json.loads(path.read_text())
                for rec in slots.values():
                    rec.pop("commit_user_id", None)
                    rec.pop("commit_users", None)
                path.write_text(json.dumps(slots))
        await self.restart(rewrite)

    async def ev_down(self):
        self.memory.down = True

    async def ev_up(self):
        self.memory.down = False

    async def ev_add_before(self):
        self.memory.next_add = "before"

    async def ev_add_after(self):
        self.memory.next_add = "after"

    async def ev_crash_transfer(self):
        self.crash_next_transfer = True

    async def ev_crash_drop(self):
        self.crash_next_drop = True

    async def ev_save_fails(self):
        self.fail_next_save = True

    def _disk(self):
        state = self.base / "state" / "pending_uploads.json"
        sidecars = {p.name: p.read_text() for p in sorted((self.base / "blobs").rglob("*.json"))}
        return sidecars, state.read_text() if state.exists() else None

    async def ev_shutdown_hung(self):
        """Lifespan shutdown while a settling lookup stays blocked past the
        grace period. Right after shutdown returns, the pass must be abandoned;
        when the lookup returns later it must change nothing on disk. Then the
        process starts again."""
        proxy = self.proxy
        await self.ev_settle_slow()
        for _ in range(3):
            await asyncio.sleep(0)
        task = proxy._reap_task
        messages = iter([{"type": "lifespan.shutdown"}])

        async def receive():
            return next(messages)

        async def send(message):
            pass

        await proxy.handle_lifespan(receive, send)
        if task is not None and not task.done() and not getattr(proxy, "_settling_abandoned", False):
            raise Violation("(e) shutdown returned while its settling pass was still live, not abandoned")
        snapshot = self._disk()
        self.gate.set()  # the blocked lookup returns after shutdown
        await asyncio.gather(*self.background, *([task] if task else []), return_exceptions=True)
        self.background = []
        if self._disk() != snapshot:
            raise Violation("(e) the abandoned settling pass changed state after shutdown completed")
        await self.restart()

    async def ev_transfer_fails(self):
        self.fail_next_transfer = True

    async def ev_delete_fails(self):
        self.memory.next_delete_fails = True

    # --- invariants ---------------------------------------------------------------
    def check(self):
        blobs = self.proxy.blobs
        for memory_id, rec in self.memory._store.items():
            blob = (rec.get("metadata") or {}).get("blob_ref")
            if blob and blobs.get(blob) is None:
                raise Violation(f"(a) live caption {memory_id} cannot fetch its bytes")
        for info in blobs.iter_infos():
            if info.ref_count < len(info.owners):
                raise Violation(f"(b) blob ref_count {info.ref_count} < owners {info.owners}")
            for owner in info.owners:
                if owner in self.memory.deleted:
                    raise Violation(f"(b) blob owner {owner} is a deleted memory")


EVENTS = {
    "up_A": ("ev_upload", "A"), "up_B": ("ev_upload", "B"),
    "commit_A": ("ev_commit", "A"), "commit_B": ("ev_commit", "B"),
    "commit_other_A": ("ev_commit_other", "A"), "commit_slow_A": ("ev_commit_slow", "A"),
    "settle": ("ev_settle",), "settle_slow": ("ev_settle_slow",), "release": ("ev_release",),
    "expire": ("ev_expire",),
    "del_img_A": ("ev_delete_image", "A"), "del_gen_A": ("ev_delete_generic", "A"),
    "del_img_B": ("ev_delete_image", "B"),
    "add_image": ("ev_add_image",), "restart": ("ev_restart",),
    "legacy_restart": ("ev_legacy_restart",), "oldfmt_restart": ("ev_oldfmt_restart",),
    "down": ("ev_down",), "up": ("ev_up",),
    "add_before": ("ev_add_before",), "add_after": ("ev_add_after",),
    "crash_transfer": ("ev_crash_transfer",), "crash_drop": ("ev_crash_drop",),
    "transfer_fails": ("ev_transfer_fails",), "delete_fails": ("ev_delete_fails",),
    "save_fails": ("ev_save_fails",), "shutdown_hung": ("ev_shutdown_hung",),
}


@pytest.fixture
def run_sequence(tmp_path, monkeypatch):
    import persistence
    import server
    from server import ProxySettings

    monkeypatch.setattr(persistence.os, "fsync", lambda fd: None)  # speed only
    monkeypatch.setattr(logging.getLogger("reliquary"), "level", logging.CRITICAL + 1)  # speed only
    monkeypatch.setattr(server, "UPLOAD_RECONCILE_RETRY", 0.0)    # an explicit settle may always retry
    monkeypatch.setattr(server, "UPLOAD_SHUTDOWN_GRACE", 0.01)

    async def inline_to_thread(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", inline_to_thread)
    counter = itertools.count()
    real_time = time.time

    def _run(sequence):
        base = tmp_path / f"run{next(counter)}"
        base.mkdir()
        holder = {}
        monkeypatch.setattr(time, "time", lambda: holder["model"].clock[0] if "model" in holder else real_time())

        async def scenario():
            model = Model(base, server, ProxySettings)
            holder["model"] = model
            try:
                for step, name in enumerate(sequence):
                    method, *args = EVENTS[name]
                    try:
                        await asyncio.wait_for(getattr(model, method)(*args), 5)
                        for _ in range(3):
                            await asyncio.sleep(0)  # let scheduled background passes run
                        model.check()
                    except Violation as exc:
                        raise Violation(f"{exc}\n  after step {step + 1} of {sequence}") from None
                    except asyncio.TimeoutError:
                        raise Violation(f"(d) step hung\n  at step {step + 1} of {sequence}") from None
                    except Exception as exc:
                        raise Violation(f"(d) {type(exc).__name__}: {exc}\n  at step {step + 1} of {sequence}") from exc
                try:
                    await asyncio.wait_for(model.ev_release(), 5)
                except asyncio.TimeoutError:
                    raise Violation(f"(d) final release hung\n  in {sequence}") from None
                except Exception as exc:
                    raise Violation(f"(d) {type(exc).__name__}: {exc}\n  at the end of {sequence}") from exc
                model.check()
            finally:
                holder.pop("model", None)
                leftovers = [*model.background, *model.orphans,
                             *(p._reap_task for p in [model.proxy, *model.dead] if p._reap_task)]
                for proxy in [model.proxy, *model.dead]:
                    proxy.pending_upload_store = None
                for gate in model.gates:
                    gate.set()  # let blocked (drained) work finish; checks are done
                for task in leftovers:
                    task.cancel()
                await asyncio.wait_for(asyncio.gather(*leftovers, return_exceptions=True), 5)

        asyncio.run(scenario())

    return _run


# Codex's scenarios from rounds 4 and 5, and the shapes behind them.
KNOWN_SEQUENCES = [
    # round 5, P1 1 (first scenario): two deployed-format slots sharing bytes,
    # one of them the stale slot of a committed caption
    ["up_A", "up_B", "crash_drop", "commit_A", "legacy_restart", "del_img_A", "commit_A", "commit_B"],
    ["up_A", "up_B", "crash_drop", "commit_A", "legacy_restart", "del_img_A", "expire", "settle", "commit_B"],
    # round 5, P1 1 (second scenario): caption deleted while a settling lookup is in flight
    ["up_A", "crash_transfer", "commit_A", "expire", "settle_slow", "del_gen_A", "release"],
    # round 5, P1 2: a commit whose lookup returns after expiry, caption lands then add raises
    ["up_A", "add_after", "commit_slow_A", "expire", "release"],
    ["up_A", "commit_slow_A", "expire", "release"],
    # round 5 / round 4 item 1: retry under an unrecorded custom user
    ["up_A", "crash_transfer", "commit_other_A", "oldfmt_restart", "commit_other_A", "del_img_A"],
    # a hand-over that fails, then a rollback whose caption delete fails
    ["up_A", "transfer_fails", "delete_fails", "commit_A"],
    ["up_A", "transfer_fails", "delete_fails", "commit_A", "expire", "settle"],
    # round 6, P1: a commit's write-ahead save fails, the retry writes its caption
    # without saving, crash, restart, expiry
    ["up_A", "save_fails", "commit_A", "crash_transfer", "commit_A", "expire", "settle"],
    # round 6: shutdown while a settling lookup stays blocked past the grace period
    ["up_A", "crash_transfer", "commit_A", "expire", "shutdown_hung", "settle"],
    ["up_A", "expire", "shutdown_hung"],
]

# Exhaustive tails: the events behind every ownership change of one slot.
CORE = ["commit_A", "commit_slow_A", "settle_slow", "release", "expire", "del_gen_A", "add_after"]


@pytest.mark.parametrize("sequence", KNOWN_SEQUENCES, ids=lambda s: "-".join(s))
def test_known_upload_sequences_keep_the_invariant(run_sequence, sequence):
    run_sequence(sequence)


def _run_all(run_sequence, sequences):
    failures = []
    for sequence in sequences:
        try:
            run_sequence(sequence)
        except Violation as exc:
            failures.append(str(exc))
    assert not failures, f"{len(failures)} failing sequence(s), first ones:\n" + "\n".join(failures[:8])


def test_random_upload_sequences_keep_the_invariant(run_sequence):
    names = sorted(EVENTS)
    sequences = []
    for seed in range(80):
        rng = random.Random(seed)
        prefix = ["up_A"] + (["up_B"] if rng.random() < 0.5 else [])
        sequences.append(prefix + [rng.choice(names) for _ in range(rng.randint(4, 9))])
    _run_all(run_sequence, sequences)


def test_save_failures_anywhere_keep_the_invariant(run_sequence):
    """A pending-state save fails (then storage recovers) at every point of
    every short sequence over the events that read or write that state."""
    alphabet = ["commit_A", "crash_transfer", "restart"]
    sequences = []
    for tail in itertools.product(alphabet, repeat=3):
        for at in range(len(tail) + 1):
            sequences.append(["up_A", *tail[:at], "save_fails", *tail[at:], "expire", "settle"])
    _run_all(run_sequence, sequences)


def test_short_upload_sequences_exhaustively_keep_the_invariant(run_sequence):
    _run_all(run_sequence, [["up_A", *tail] for tail in itertools.product(CORE, repeat=3)])


# --- targeted cases ------------------------------------------------------------

def _claude_call(proxy, name, args):
    profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
    return asyncio.run(proxy.call_mcp_tool(profile, name, args, can_write=True))


def _upload(proxy):
    async def go():
        profile = proxy.endpoint_profiles[proxy.settings.claude_mcp_path]
        upload_id = proxy.handle_create_image_upload_tool({}, profile=profile)["structuredContent"]["upload_id"]
        sent = []

        async def receive():
            return {"type": "http.request", "body": PNG, "more_body": False}

        async def send(message):
            sent.append(message)

        await proxy.handle_upload_post(upload_id, {"type": "http", "headers": [
            (b"authorization", b"Bearer claude-secret"), (b"content-type", b"image/png")]}, receive, send)
        return upload_id
    return asyncio.run(go())


# 2 -- the one marker operation fails closed
def test_settle_owner_changes_nothing_without_the_marker(tmp_path):
    from blobs import BlobStore

    store = BlobStore(str(tmp_path / "blobs"), b"k")
    blob = store.put(PNG, owner="upload:upl_a").id
    store.put(PNG)  # an unmarked reference held by someone else
    assert store.settle_owner(blob, "upload:upl_b") is False
    assert store.settle_owner(blob, "upload:upl_b", to="m1") is False
    assert store.info(blob).ref_count == 2 and store.info(blob).owners == ["upload:upl_a"]
    assert store.settle_owner(blob, "upload:upl_a") is True
    assert store.settle_owner(blob, "upload:upl_a") is False  # a second release finds no marker
    assert store.info(blob).ref_count == 1 and store.info(blob).owners == []


# 1 -- deployed-format slots are forgotten; their references are not touched
def test_deployed_format_slot_is_forgotten_and_answers_unknown_upload(make_proxy, tmp_path):
    from blobs import BlobStore

    blob = BlobStore(str(tmp_path / "blobs"), b"test-signing-key").put(PNG).id
    state = tmp_path / "state"
    state.mkdir()
    (state / "pending_uploads.json").write_text(json.dumps({"upl_old": {
        "id": "upl_old", "created_at": 1.0, "expires_at": 1e12, "blob_id": blob, "mimetype": "image/png",
        "size": len(PNG), "profile": "claude"}}))
    proxy = make_proxy(state_dir=str(state))
    assert proxy.pending_uploads == {}
    result = _claude_call(proxy, "reliquary_commit_image_upload", {"upload_id": "upl_old", "caption": "cat"})
    assert result["structuredContent"]["error"] == "unknown_upload"
    assert proxy.blobs.info(blob).ref_count == 1  # kept: a leak lint reports


# 3 -- a caption delete retires its pending slot, or waits for its claim
def test_generic_delete_of_an_unowned_caption_retires_its_slot(make_proxy, tmp_path):
    memory = ChaosMemory()
    proxy = make_proxy(memory=memory, state_dir=str(tmp_path / "state"))
    slot = _upload(proxy)
    blob = proxy.pending_uploads[slot].blob_id
    proxy.pending_uploads[slot].commit_users = [USER]
    caption = memory.add("cat", user_id=USER, metadata={"kind": "image", "source_group": "user-write",
                                                        "blob_ref": blob, "upload_id": slot})["results"][0]["id"]
    result = _claude_call(proxy, "reliquary_delete", {"id": caption})
    assert result["isError"] is False
    assert slot not in proxy.pending_uploads
    assert proxy.blobs.info(blob) is None  # the slot's reference went with its caption


def test_caption_delete_waits_for_a_claimed_slot(make_proxy, tmp_path):
    memory = ChaosMemory()
    proxy = make_proxy(memory=memory, state_dir=str(tmp_path / "state"))
    slot = _upload(proxy)
    blob = proxy.pending_uploads[slot].blob_id
    caption = memory.add("cat", user_id=USER, metadata={"kind": "image", "source_group": "user-write",
                                                        "blob_ref": blob, "upload_id": slot})["results"][0]["id"]
    proxy._busy_uploads.add(slot)  # a commit or settling pass holds it
    for tool, args in (("reliquary_delete", {"id": caption}), ("reliquary_delete_image", {"memory_id": caption})):
        result = _claude_call(proxy, tool, args)
        assert result["structuredContent"]["error"] == "upload_in_progress"
    assert caption in memory._store and proxy.blobs.is_owner(blob, f"upload:{slot}")


# 6 -- shutdown cancels and drains the settling pass, bounded
def test_lifespan_shutdown_cancels_a_hung_settling_pass(make_proxy, tmp_path, monkeypatch):
    import threading

    import server

    monkeypatch.setattr(server, "UPLOAD_SHUTDOWN_GRACE", 0.2)
    memory = ChaosMemory()
    proxy = make_proxy(memory=memory, state_dir=str(tmp_path / "state"))
    slot = _upload(proxy)
    proxy.pending_uploads[slot].commit_users = [USER]
    proxy.pending_uploads[slot].expires_at = 1
    release = threading.Event()
    real_get_all = memory.get_all
    memory.get_all = lambda *a, **k: (release.wait(5), real_get_all(*a, **k))[1]
    monkeypatch.setattr(asyncio, "to_thread", REAL_TO_THREAD)

    async def scenario():
        messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])
        sent = []

        async def receive():
            if sent:
                await asyncio.sleep(0.05)  # the pass is now blocked in the backend
            return next(messages)

        async def send(message):
            sent.append(message["type"])

        started = time.monotonic()
        await proxy.handle_lifespan(receive, send)
        elapsed = time.monotonic() - started
        task = proxy._reap_task
        # Right after shutdown returns: the lookup is still blocked (a thread
        # cannot be killed), and the pass is abandoned.
        state_at_shutdown = (release.is_set(), task.done(), proxy._settling_abandoned)
        on_disk = (pending_file.read_text(), sidecar.read_text())
        release.set()  # the lookup returns after shutdown completed
        await asyncio.wait({task}, timeout=5)
        return sent, elapsed, task, state_at_shutdown, on_disk

    pending_file = tmp_path / "state" / "pending_uploads.json"
    blob = proxy.pending_uploads[slot].blob_id
    sidecar = Path(proxy.blobs._sidecar_path(blob))
    sent, elapsed, task, state_at_shutdown, on_disk = asyncio.run(scenario())
    assert sent == ["lifespan.startup.complete", "lifespan.shutdown.complete"]
    assert elapsed < 2.0
    assert state_at_shutdown == (False, False, True)
    assert task.done()
    # Nothing was applied or persisted after shutdown reported completion.
    assert (pending_file.read_text(), sidecar.read_text()) == on_disk
    assert proxy.blobs.is_owner(blob, f"upload:{slot}")
    assert slot in proxy.pending_uploads  # unresolved: settled after the next start
