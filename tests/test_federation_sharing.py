"""The owner side of a shared API session (#2054): the verbs a peer calls, the per-call check, the
byte budget and the clipping.

Two real services joined by the in-memory transport of `test_federation_service`. Most tests read
a scripted session (`FakeRuntime`, which stands in for `structured_runtime` only); the tests at
the end drive the real native runtime with its scripted agents (`test_native_runtime`'s fixtures)
and compare against the real public-link routes.

`wire` records every answer body exactly as a channel wrote it to its stream, so the size and
"nothing was sent" assertions are about the bytes the production serializer produced.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import copy
import json
import random
import time
import uuid
from types import SimpleNamespace

import pytest

from agent_sessions import metadata, share_api, shares, template_secrets
from agent_sessions import structured_runtime as runtime
from agent_sessions.federation import channel as ch
from agent_sessions.federation import grants, remote, service, sharing, store, terminal
from agent_sessions.homefree import mux as hf_mux
from federation_helpers import until
from test_federation_grants import api_key, live, trusted  # noqa: F401 — `live` is a fixture
from test_federation_routes import Instance, _wait, two_instances  # noqa: F401 — a fixture
from test_federation_service import Net, _same_channel, _stop, _trusted_pair
from test_native_runtime import ENGINES, host, ident, project, settle  # noqa: F401 — fixtures
from test_share_api import _operator_snapshot, _session, _visitor, env  # noqa: F401 — fixtures

NO_GRANT = b'{"ok":false,"code":"no_grant"}'
UNAVAILABLE = b'{"ok":false,"code":"unavailable"}'
SECRET = "s3cret-value-123"
EMOJI = "\U0001f600"  # one character, twelve bytes on the wire


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def wire(monkeypatch):
    """Every answer body a channel wrote, as bytes."""
    bodies: list[bytes] = []
    real = hf_mux.Stream.write

    async def write(self, data):
        bodies.append(bytes(data))
        return await real(self, data)

    monkeypatch.setattr(hf_mux.Stream, "write", write)
    return bodies


def data_bodies(wire: list[bytes]) -> list[bytes]:
    """The recorded answers that carry session content or a refusal (not pings and hints)."""
    return [
        b
        for b in wire
        if b != b'{"ok":true,"result":{}}' and b'"protocol"' not in b and b'"generation"' not in b
    ]


class Pair:
    def __init__(self, net, a, b) -> None:
        self.net, self.a, self.b = net, a, b

    async def call(self, verb: str, args: dict | None = None, **kwargs) -> dict:
        """Beta (the receiver) calls alpha (the owner)."""
        return await self.b.channel_for(self.a.peer_id).call(verb, args or {}, **kwargs)

    async def refused(self, verb: str, args: dict | None = None, **kwargs) -> str:
        with pytest.raises(ch.CallError) as exc:
            await self.call(verb, args, **kwargs)
        return exc.value.code

    async def grant(self, key: str | None = None, **kwargs) -> store.Grant:
        return await asyncio.to_thread(
            grants.create,
            self.a.store,
            session_key=key or api_key(),
            peer_id=self.b.peer_id,
            level="view",
            expires_in=kwargs.get("expires_in"),
        )

    def pause_owner_rounds(self, monkeypatch) -> None:
        """Stop alpha's dialer rounds: a revoke written by another process then leaves the
        channel open, which is exactly the window the per-call check exists for."""

        async def nothing() -> None:
            return None

        monkeypatch.setattr(self.a, "_dial_round", nothing)

    async def quiet_receiver(self, monkeypatch) -> None:
        """Stop beta's rounds and wait out what one already started: its own refresh of the
        mirror is a `share.list` too, and one answered while the test's call is in flight puts
        a second, earlier page on the wire (#2059: beta's round does more per tick)."""

        async def nothing() -> None:
            return None

        monkeypatch.setattr(self.b, "_dial_round", nothing)
        idle = 0
        for _ in range(1000):
            busy = self.b._refreshing or self.b._checking
            idle = 0 if busy else idle + 1
            if idle >= 3 * 5:  # three round ticks (DIAL_TICK_S = 0.05) with nothing in flight
                return
            await asyncio.sleep(0.01)
        raise AssertionError("beta never went quiet")


@contextlib.asynccontextmanager
async def paired(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DIAL_TICK_S", 0.05)
    net = Net()
    a, b = net.add("alpha", tmp_path), net.add("beta", tmp_path)
    a.start()
    b.start()
    await _trusted_pair(net, a, b)
    await _same_channel(a, b)
    try:
        yield Pair(net, a, b)
    finally:
        await _stop(a, b)


def ref(grant: store.Grant, **extra) -> dict:
    return {"grant_id": grant.grant_id, "revision": grant.revision, **extra}


# ── a scripted session ────────────────────────────────────────────────


def op_turn(text: str = "hi", reply: str | None = "ok", **extra) -> dict:
    turn_id = str(uuid.uuid4())
    return {
        "kind": "operator",
        "turn_id": turn_id,
        "operation_id": turn_id,
        "state": "completed",
        "text": text,
        "ts": 1.0,
        "reply": reply,
        "reply_ts": 2.0,
        "text_truncated": False,
        "reply_truncated": False,
        "attachments": [],
        "tools": [],
        "tools_truncated": False,
        "reason": "an operator-only reason",
        "context": {"mission_id": "m1"},
        **extra,
    }


class FakeRuntime:
    """What `sharing` reads of `structured_runtime`, for one scripted session."""

    WINDOW = 50

    def __init__(self) -> None:
        self.turns: list[dict] = []
        self.requests: list[dict] = []
        self.log: list[dict] = []
        self.cursor = 10
        self.waiting = 0
        self.override: dict | None = None
        self._moved = asyncio.Event()

    def install(self, monkeypatch) -> FakeRuntime:
        for name in ("snapshot", "events", "wait_events", "history"):
            monkeypatch.setattr(runtime, name, getattr(self, name))
        return self

    def emit(self, kind: str = "text") -> None:
        self.cursor += 1
        self.log.append({"cursor": self.cursor, "kind": kind, "data": {"text": "PRIVATE-DATA"}})
        moved, self._moved = self._moved, asyncio.Event()
        moved.set()

    async def snapshot(self, key: str, **_kw) -> dict:
        if self.override is not None:
            return copy.deepcopy(self.override)
        shown = self.turns[-self.WINDOW :]
        return {
            "session_key": key,
            "revision": self.cursor,
            "event_cursor": self.cursor,
            "cwd": "/home/op/secret-project",
            "state": "idle",
            "active_turn": None,
            "model_effective": "model-x",
            "turns": copy.deepcopy(shown),
            "omitted_turns": len(self.turns) - len(shown),
            "pending_requests": copy.deepcopy(self.requests),
            "native": {"native_id": "abc", "worker": "w-uuid"},
            "read_only": None,
            "images": True,
        }

    async def events(self, key: str, *, after: int, limit: int = 100) -> dict:
        events = [e for e in self.log if e["cursor"] > after][:limit]
        return {
            "session_key": key,
            "revision": self.cursor,
            "events": copy.deepcopy(events),
            "next_cursor": events[-1]["cursor"] if events else after,
        }

    async def wait_events(self, key: str, *, after: int, limit: int = 100, wait: float = 0, **_kw):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait
        while True:
            page = await self.events(key, after=after, limit=limit)
            left = deadline - loop.time()
            if page["events"] or left <= 0:
                return page
            self.waiting += 1
            try:
                with contextlib.suppress(TimeoutError):
                    async with asyncio.timeout(left):
                        await self._moved.wait()
            finally:
                self.waiting -= 1

    async def history(self, key: str, *, before, limit: int = 50, **_kw) -> dict:
        ids = [t["turn_id"] for t in self.turns]
        if before not in ids:
            raise runtime.StructuredError(422, "before must be a turn id")
        older = self.turns[: ids.index(before)]
        page = older[-limit:]
        more = len(older) > len(page)
        return {
            "session_key": key,
            "turns": copy.deepcopy(page),
            "before": page[0]["turn_id"] if more else None,
            "has_more": more,
            "worker_ends": [{"operator": "only"}],
        }


@pytest.fixture
def fake(monkeypatch):
    return FakeRuntime().install(monkeypatch)


def sent(wire: list[bytes], result: dict) -> bytes:
    """The recorded body of ``result``: proof that what was measured is what was written."""
    body = ch.encode_answer({"ok": True, "result": result})
    assert body in wire
    return body


# ── clipping one item ─────────────────────────────────────────────────


def test_an_item_within_budget_is_returned_unchanged():
    turn = share_api.project({"turns": [op_turn("a" * 5000)]}, {}, share_api._Redactor([]), "g")[
        "turns"
    ][0]
    assert sharing.clip(turn, sharing.TURN_BUDGET) is turn
    assert "truncated" not in turn


def test_a_long_string_keeps_its_first_32768_characters_and_says_how_many_were_cut():
    turn = {"turn_id": "t", "text": "a" * 300_000, "reply": "b" * 100, "tools": []}
    out = sharing.clip(turn, sharing.TURN_BUDGET)
    assert out["text"] == "a" * 32768 + "… [cut: 267232 more characters]"
    assert out["reply"] == "b" * 100 and out["truncated"] is True
    assert sharing._size(out) <= sharing.TURN_BUDGET
    assert turn["text"] == "a" * 300_000 and "truncated" not in turn  # the input is not changed


def test_the_limit_is_halved_while_the_item_is_over_budget_down_to_256():
    # 20 000 non-BMP characters are 240 KiB on the wire: under the 32 768 limit, over the budget.
    turn = {"turn_id": "t", "text": EMOJI * 20_000, "tools": []}
    out = sharing.clip(turn, sharing.TURN_BUDGET)
    assert out["text"] == EMOJI * 8192 + "… [cut: 11808 more characters]"
    assert out["truncated"] is True and sharing._size(out) <= sharing.TURN_BUDGET
    # Deterministic: the same input gives the same bytes.
    assert sharing.clip(turn, sharing.TURN_BUDGET) == out
    # Down to the floor and no further.
    tiny = sharing.clip({"turn_id": "t", "text": EMOJI * 20_000}, 6000)
    assert tiny["text"].startswith(EMOJI * 256 + "… [cut: ")
    assert tiny["text"].count(EMOJI) == 256


def test_tool_entries_and_then_attachments_are_dropped_from_the_end_and_counted():
    tools = [{"name": f"tool-{i}", "outcome": "ok", "summary": "s" * 400} for i in range(5000)]
    files = [{"name": f"file-{i}.txt", "mime": "text/plain"} for i in range(40)]
    turn = {"turn_id": "t", "text": "x", "tools": tools, "attachments": files}
    out = sharing.clip(turn, sharing.TURN_BUDGET)
    kept = len(out["tools"])
    assert 0 < kept < 5000 and out["tools_omitted"] == 5000 - kept
    assert [t["name"] for t in out["tools"]] == [f"tool-{i}" for i in range(kept)]  # from the end
    assert out["attachments"] == files and "attachments_omitted" not in out  # tools sufficed
    assert out["truncated"] is True and sharing._size(out) <= sharing.TURN_BUDGET

    # Attachments go only when dropping every tool entry was not enough.
    many = [{"name": f"file-{i}.txt", "mime": "text/plain"} for i in range(20_000)]
    out = sharing.clip({"turn_id": "t", "tools": tools[:10], "attachments": many}, 64 * 1024)
    assert out["tools"] == [] and out["tools_omitted"] == 10
    assert 0 < len(out["attachments"]) < 20_000
    assert out["attachments_omitted"] == 20_000 - len(out["attachments"])
    assert sharing._size(out) <= 64 * 1024


def test_an_item_no_step_can_fit_becomes_its_own_scalar_fields():
    risk = {f"k{i}": "v" * 50 for i in range(20_000)}  # not a string, a tool or an attachment
    request = {"request_id": "r", "turn_id": "t", "kind": "shell", "summary": "rm", "risk": risk}
    out = sharing.clip(request, sharing.REQUEST_BUDGET)
    assert out == {
        "request_id": "r",
        "turn_id": "t",
        "kind": "shell",
        "summary": "rm",
        "truncated": True,
    }


# ── the aggregate budget ──────────────────────────────────────────────


def projected(turns: list[dict], requests: list[dict] | None = None, **top) -> dict:
    snap = {
        "revision": 7,
        "event_cursor": 7,
        "state": "idle",
        "turns": turns,
        "pending_requests": requests or [],
        "omitted_turns": 0,
        **top,
    }
    view = share_api.project(snap, {}, share_api._Redactor([]), "g" * 22)
    view["link"] = "l" * 24
    view["can_send"] = False
    return view


def test_turns_each_within_their_cap_are_still_budgeted_together():
    # 30 turns of about 120 KiB: none is clipped, and they cannot all be sent.
    turns = [op_turn(EMOJI * 10_000) for _ in range(30)]
    view = projected(turns)
    out = sharing.fit_snapshot(view)
    assert sharing.wire_size(out) <= sharing.BUDGET
    assert len(out["turns"]) == 1 and out["omitted_turns"] == 29
    assert out["turns"][0]["turn_id"] == turns[-1]["turn_id"]  # newest first
    assert "truncated" not in out["turns"][0]


def test_pending_requests_count_toward_the_same_budget_and_never_crowd_out_the_turn():
    requests = [
        {"request_id": f"r{i}", "turn_id": "t", "kind": "shell", "summary": EMOJI * 300, "risk": {}}
        for i in range(500)
    ]
    newest = op_turn(EMOJI * 20_000)  # clipped to its cap
    out = sharing.fit_snapshot(projected([op_turn("old"), newest], requests))
    assert sharing.wire_size(out) <= sharing.BUDGET
    assert out["turns"] and out["turns"][-1]["turn_id"] == newest["turn_id"]
    assert out["turns"][-1]["truncated"] is True
    kept = len(out["pending_requests"])
    assert 0 < kept < 500 and out["pending_requests_omitted"] == 500 - kept
    head = {**out, "turns": []}
    assert sharing.wire_size(head) <= sharing.HEAD_BUDGET + 64
    assert out["omitted_turns"] == 2 - len(out["turns"])


def test_a_request_over_its_cap_is_clipped_by_the_same_rule():
    request = {"request_id": "r", "turn_id": "t", "kind": "k" * 100_000, "summary": "x", "risk": {}}
    out = sharing.fit_snapshot(projected([op_turn()], [request]))
    (got,) = out["pending_requests"]
    # 32 768, then halved until the request fits its 8 KiB: 4 096 characters are kept
    assert got["truncated"] is True and got["kind"] == "k" * 4096 + "… [cut: 95904 more characters]"
    assert sharing._size(got) <= sharing.REQUEST_BUDGET


def test_no_shape_of_snapshot_passes_the_budget():
    rng = random.Random(2054)
    for _ in range(25):
        turns = []
        for _ in range(rng.randrange(0, 60)):
            char = rng.choice(["a", EMOJI, "é"])
            turn = op_turn(char * rng.randrange(0, 30_000), char * rng.randrange(0, 30_000))
            turn["tools"] = [
                {"name": "t", "outcome": "ok", "summary": char * rng.randrange(0, 500)}
                for _ in range(rng.choice([0, 0, 3, 400, 4000]))
            ]
            turn["attachments"] = [
                {"name": char * rng.randrange(1, 200), "mime": "image/png", "stored": "s.png"}
                for _ in range(rng.choice([0, 0, 2, 3000]))
            ]
            turns.append(turn)
        requests = [
            {
                "request_id": f"r{i}",
                "turn_id": "t",
                "kind": "shell",
                "summary": rng.choice(["a", EMOJI]) * rng.randrange(0, 400),
                "risk": {"level": rng.choice(["a", EMOJI]) * rng.randrange(0, 5000)},
            }
            for i in range(rng.choice([0, 1, 50, 700]))
        ]
        view = projected(turns, requests, model_effective=EMOJI * rng.choice([0, 10, 5000]))
        sharing._strip_images(view)
        out = sharing.fit_snapshot(view)
        assert sharing.wire_size(out) <= sharing.BUDGET
        assert bool(out["turns"]) == bool(turns)  # at least one turn whenever there is one
        assert out["omitted_turns"] == len(turns) - len(out["turns"])
        delivered = [t["turn_id"] for t in out["turns"]]
        assert delivered == [t["turn_id"] for t in turns][len(turns) - len(delivered) :]


# ── every refusal is the same answer ──────────────────────────────────


async def _all_refused(p: Pair, wire: list[bytes], made: store.Grant, revision=None) -> None:
    """Every session verb answers `no_grant` for this grant, and writes exactly those bytes."""
    args = {"grant_id": made.grant_id, "revision": revision or made.revision}
    calls = (
        ("session.snapshot", args),
        ("session.events", {**args, "after": 0}),
        ("session.events", {**args, "after": 0, "wait": 0.2}),
        ("session.history", {**args, "before": str(uuid.uuid4())}),
    )
    for verb, call_args in calls:
        wire.clear()
        assert await p.refused(verb, call_args) == "no_grant", verb
        assert data_bodies(wire) == [NO_GRANT], verb  # byte for byte, and nothing else


@pytest.mark.anyio
async def test_every_refusal_is_byte_for_byte_the_answer_to_an_unknown_id(
    tmp_path,
    monkeypatch,
    wire,
    fake,
    live,  # noqa: F811
):
    fake.turns = [op_turn("the content a refusal must never carry")]
    fake.emit()
    async with paired(tmp_path, monkeypatch) as p:
        # What an unknown id gets: the reference every other case is compared with.
        unknown = SimpleNamespace(grant_id="u" * 22, revision=1)
        await _all_refused(p, wire, unknown)

        # A live grant is served, so the cases below are refusals and not a broken harness.
        served = await p.grant()
        assert (await p.call("session.snapshot", ref(served)))["turns"]

        # Another peer's grant id.
        stranger = await asyncio.to_thread(trusted, p.a.store, "stranger")
        theirs = await asyncio.to_thread(
            grants.create,
            p.a.store,
            session_key=api_key(),
            peer_id=stranger.peer_id,
            level="view",
            expires_in=None,
        )
        await _all_refused(p, wire, theirs)

        # A stale revision (and one from the future).
        changed = await asyncio.to_thread(
            grants.change, p.a.store, served.grant_id, expires_in=3600, revision=1
        )
        await _all_refused(p, wire, served)  # still carries revision 1
        await _all_refused(p, wire, served, revision=3)
        assert (await p.call("session.snapshot", ref(changed)))["turns"]

        # An expired grant, on the owner's clock (its sweep may or may not have run yet).
        short = await p.grant(expires_in=60)
        assert (await p.call("session.snapshot", ref(short)))["turns"]
        with monkeypatch.context() as m:
            m.setattr(grants, "_clock", lambda: time.time() + 61)
            await _all_refused(p, wire, short)
        assert (await p.call("session.snapshot", ref(changed)))["turns"]

        # An archived session: the grant is still live in the store, the session is not.
        live[changed.session_key] = "archived"
        await _all_refused(p, wire, changed)
        live.clear()
        # ...and an archive through the wrapper every archive path uses.
        monkeypatch.setenv("AGENT_SESSIONS_FEDERATION_DIR", str(p.a.dir))
        async with shares.archiving(changed.session_key):
            pass
        await _all_refused(p, wire, changed)

        # A revoked grant.
        revoked = await p.grant()
        await asyncio.to_thread(grants.revoke, p.a.store, revoked.grant_id)
        await _all_refused(p, wire, revoked)

        # A revoked peer: written by ANOTHER process, while the channel is still open.
        last = await p.grant()
        p.pause_owner_rounds(monkeypatch)
        await asyncio.sleep(0.15)
        await asyncio.to_thread(store.Store(p.a.dir).revoke, p.b.peer_id)
        channel = p.a.channel_for(p.b.peer_id)
        assert channel is not None and not channel.closed
        await _all_refused(p, wire, last)
        wire.clear()
        assert await p.refused("share.list") == "no_grant"
        assert data_bodies(wire) == [NO_GRANT]
        assert not channel.closed  # every one of those was refused by the check, not the socket


@pytest.mark.anyio
async def test_malformed_arguments_reset_the_stream_and_reach_no_handler(
    tmp_path,
    monkeypatch,
    wire,
    fake,
    live,  # noqa: F811
):
    async with paired(tmp_path, monkeypatch) as p:
        made = await p.grant()
        good = ref(made)
        bad_calls = [
            ("session.snapshot", {"grant_id": made.grant_id}),
            ("session.snapshot", {**good, "session_key": made.session_key}),  # never a key
            ("session.snapshot", {**good, "revision": True}),
            ("session.events", {**good, "wait": 20.5}),
            ("session.events", {**good, "limit": 101}),
            ("session.events", {**good, "after": -1}),
            ("session.history", good),  # no `before`
        ]
        wire.clear()
        for verb, args in bad_calls:
            assert await p.refused(verb, args) == "refused", (verb, args)
        assert data_bodies(wire) == []  # the stream was reset: no handler ran, no answer
        assert p.a.channel_for(p.b.peer_id).stats.refused == len(bad_calls)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "verb,args",
    [("share.list", {"limit": 51}), ("share.list", {"after": "../x"}), ("share.changed", {"x": 1})],
)
async def test_the_list_verbs_refuse_malformed_arguments(tmp_path, monkeypatch, verb, args):
    async with paired(tmp_path, monkeypatch) as p:
        assert await p.refused(verb, args) == "refused"


# ── a parked call is authorized twice ─────────────────────────────────

ACTIONS = ("revoke", "expire", "revision", "archive", "peer_revoked_by_another_process")


async def _end_grant(action: str, p: Pair, made: store.Grant, monkeypatch) -> None:
    if action == "revoke":
        await asyncio.to_thread(grants.revoke, p.a.store, made.grant_id)
    elif action == "expire":
        late = time.time() + 8 * 86400
        monkeypatch.setattr(grants, "_clock", lambda: late)
    elif action == "revision":
        await asyncio.to_thread(
            grants.change, p.a.store, made.grant_id, expires_in=3600, revision=made.revision
        )
    elif action == "archive":
        monkeypatch.setenv("AGENT_SESSIONS_FEDERATION_DIR", str(p.a.dir))
        async with shares.archiving(made.session_key):
            pass
    else:
        # A second store object over the same directory: another process's write.
        await asyncio.to_thread(store.Store(p.a.dir).revoke, p.b.peer_id)


@pytest.mark.anyio
@pytest.mark.parametrize("action", ACTIONS)
@pytest.mark.parametrize("woken", [False, True], ids=["ends_the_wait", "woken_by_an_event"])
async def test_a_parked_wait_ends_as_no_grant_and_sends_no_event(
    tmp_path,
    monkeypatch,
    wire,
    fake,
    live,  # noqa: F811
    action,
    woken,
):
    # `woken`: the wait is never re-checked while parked, an event wakes it AFTER the grant
    # ended, and only the check directly before the answer stands between that event and the
    # peer. Otherwise: the parked wait itself notices, well before its 20 s are over.
    monkeypatch.setattr(sharing, "WAIT_SLICE_S", 60.0 if woken else 0.05)
    async with paired(tmp_path, monkeypatch) as p:
        made = await p.grant()
        p.pause_owner_rounds(monkeypatch)
        await asyncio.sleep(0.15)
        channel = p.a.channel_for(p.b.peer_id)
        wire.clear()
        started = time.monotonic()
        parked = asyncio.create_task(
            p.call("session.events", ref(made, after=fake.cursor, wait=20), timeout=30)
        )
        await until(lambda: p.a.waits.get(p.b.peer_id) == 1 and fake.waiting == 1)
        assert not parked.done()

        await _end_grant(action, p, made, monkeypatch)
        if woken:
            await asyncio.sleep(0.05)
            assert not parked.done()  # still parked: nothing re-checked it
            fake.emit("text")  # the event that wakes it
        with pytest.raises(ch.CallError) as exc:
            async with asyncio.timeout(10):
                await parked
        assert exc.value.code == "no_grant"
        assert time.monotonic() - started < 10  # ended by the check, not by its 20 s
        assert data_bodies(wire) == [NO_GRANT]  # no cursor, no kind, no data
        assert b"PRIVATE-DATA" not in b"".join(wire)
        assert p.a.waits == {}  # the slot is free again

        # The channel is still open, and the next call on it is refused too.
        assert p.a.channel_for(p.b.peer_id) is channel and not channel.closed
        wire.clear()
        assert await p.refused("session.snapshot", ref(made)) == "no_grant"
        assert await p.refused("session.events", ref(made, after=0)) == "no_grant"
        assert data_bodies(wire) == [NO_GRANT, NO_GRANT]


@pytest.mark.anyio
async def test_a_snapshot_built_before_a_revoke_is_not_sent(
    tmp_path,
    monkeypatch,
    wire,
    fake,
    live,  # noqa: F811
):
    """The second check sits after the projection was built: a revoke that lands while the
    answer is being made turns it into `no_grant`."""
    fake.turns = [op_turn("built before the revoke")]
    async with paired(tmp_path, monkeypatch) as p:
        made = await p.grant()
        building = asyncio.Event()
        release = asyncio.Event()
        real = sharing.fit_snapshot
        loop = asyncio.get_running_loop()

        def slow(view):
            out = real(view)
            loop.call_soon_threadsafe(building.set)
            asyncio.run_coroutine_threadsafe(release.wait(), loop).result(10)
            return out

        monkeypatch.setattr(sharing, "fit_snapshot", slow)
        wire.clear()
        call = asyncio.create_task(p.call("session.snapshot", ref(made)))
        await building.wait()  # the answer exists now
        await asyncio.to_thread(grants.revoke, p.a.store, made.grant_id)
        release.set()
        with pytest.raises(ch.CallError) as exc:
            await call
        assert exc.value.code == "no_grant"
        assert data_bodies(wire) == [NO_GRANT]
        assert b"built before the revoke" not in b"".join(wire)


def test_the_second_check_is_the_last_await_before_every_answer():
    """Pinned in the source: in each session verb the last `await` is the per-call check, and
    the statement after it is the `return` of the answer."""
    import ast
    import inspect
    import textwrap

    for handler in (
        sharing.session_snapshot,
        sharing.session_events,
        sharing.session_history,
        terminal.terminal_frame,  # the terminal view (#2056)
    ):
        tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
        body = tree.body[0].body
        last, check = body[-1], body[-2]
        assert isinstance(last, ast.Return) and isinstance(last.value, ast.Name), handler.__name__
        assert isinstance(check, ast.Expr) and isinstance(check.value, ast.Await)
        assert check.value.value.func.id == "_authorize", handler.__name__
        first = next(n for n in ast.walk(tree) if isinstance(n, ast.Await))
        assert first.value.func.id == "_authorize"  # and the call starts with the same check
        if handler is not terminal.terminal_frame:  # its own `_authorize` asks for terminal
            for call in (check.value.value, first.value):
                kinds = [ast.unparse(k.value) for k in call.keywords if k.arg == "kind"]
                assert kinds == ["grants.KIND_API"], handler.__name__  # stated, never defaulted


# ── the long-poll ─────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_an_event_wakes_a_parked_wait_with_cursors_and_kinds_only(
    tmp_path,
    monkeypatch,
    wire,
    fake,
    live,  # noqa: F811
):
    async with paired(tmp_path, monkeypatch) as p:
        made = await p.grant()
        after = fake.cursor
        parked = asyncio.create_task(
            p.call("session.events", ref(made, after=after, wait=20), timeout=30)
        )
        await until(lambda: fake.waiting == 1)
        fake.emit("turn_started")
        page = await parked
        assert page == {
            "link": share_api.link_id(made.grant_id),
            "revision": after + 1,
            "next_cursor": after + 1,
            "events": [{"cursor": after + 1, "kind": "turn_started"}],
        }
        assert b"PRIVATE-DATA" not in b"".join(wire)
        sent(wire, page)
        assert remote.check_events(page) is page  # and the receiving side accepts exactly this
        # without `wait`, a quiet session answers at once
        quiet = await p.call("session.events", ref(made, after=after + 1))
        assert quiet["events"] == [] and quiet["next_cursor"] == after + 1


@pytest.mark.anyio
async def test_at_most_eight_waits_per_peer_are_parked_and_the_ninth_is_busy(
    tmp_path,
    monkeypatch,
    fake,
    live,  # noqa: F811
):
    async with paired(tmp_path, monkeypatch) as p:
        made = await p.grant()
        held = ref(made, after=fake.cursor, wait=20)
        parked = [
            asyncio.create_task(p.call("session.events", held, timeout=30))
            for _ in range(sharing.WAITS_PER_PEER)
        ]
        await until(lambda: fake.waiting == 8)
        assert p.a.waits == {p.b.peer_id: 8}
        assert await p.refused("session.events", held, timeout=5) == "busy"
        # a read that does not wait is not a parked wait
        assert (await p.call("session.events", ref(made, after=fake.cursor)))["events"] == []
        assert (await p.call("session.snapshot", ref(made)))["can_send"] is False
        fake.emit()
        pages = await asyncio.gather(*parked)
        assert all(len(page["events"]) == 1 for page in pages)
        assert p.a.waits == {}

        # The runtime's own waiter slots are few and the operator's pane needs them: once the
        # peers' share is taken, a held read polls once per slice and takes no further slot.
        assert sharing.PEER_WAITERS_PER_SESSION == 2 < runtime.EVENTS_WAITERS_PER_SESSION
        with monkeypatch.context() as m:
            m.setattr(runtime, "events_waiters", lambda key=None: 2)
            m.setattr(sharing, "WAIT_SLICE_S", 0.05)
            polled = asyncio.create_task(
                p.call("session.events", ref(made, after=fake.cursor, wait=20), timeout=30)
            )
            await until(lambda: p.a.waits.get(p.b.peer_id) == 1)
            await asyncio.sleep(0.2)
            assert fake.waiting == 0 and not polled.done()  # parked here, not in the runtime
            fake.emit()
            assert len((await polled)["events"]) == 1
        # a wait whose channel goes away frees its slot too
        held = ref(made, after=fake.cursor, wait=20)
        gone = asyncio.create_task(p.call("session.events", held, timeout=30))
        await until(lambda: p.a.waits.get(p.b.peer_id) == 1)
        p.a.channel_for(p.b.peer_id).close("test")
        with pytest.raises(ch.CallError):
            await gone
        await until(lambda: p.a.waits == {})


@pytest.mark.anyio
async def test_the_callers_timeout_for_a_held_wait_does_not_fire_before_the_answer(
    tmp_path,
    monkeypatch,
    fake,
    live,  # noqa: F811
):
    # The arithmetic, at full size: a 20 s wait gets 30 s, which the 20 s default would cut off,
    # and the handler's own deadline is beyond the longest wait.
    assert sharing.WAIT_MAX_S == 20.0 == ch.CALL_TIMEOUT_S
    assert remote.events_timeout(sharing.WAIT_MAX_S) == 30.0 > ch.CALL_TIMEOUT_S
    assert remote.events_timeout(0) == sharing.WAIT_CALL_MARGIN_S == 10.0
    assert ch.HANDLER_TIMEOUT_S > sharing.WAIT_MAX_S

    # The same arithmetic, shortened: a quiet 1 s wait is answered after 1 s, inside wait + margin.
    monkeypatch.setattr(sharing, "WAIT_CALL_MARGIN_S", 1.0)
    async with paired(tmp_path, monkeypatch) as p:
        key = api_key()
        _rows_for(monkeypatch, {key: "held"})
        made = await p.grant(key)
        # the receiver learned of it the ordinary way: the owner's hint, then `share.list`
        await until(lambda: p.b.store.get_remote(p.a.peer_id, made.grant_id) is not None)
        assert remote.events_timeout(1.0) == 2.0
        started = time.monotonic()
        page = await remote.read(
            p.b,
            p.a.peer_id,
            made.grant_id,
            "session.events",
            {"after": fake.cursor, "limit": 100, "wait": 1.0},
            check=remote.check_events,
            timeout=remote.events_timeout(1.0),
        )
        assert page["events"] == [] and time.monotonic() - started >= 1.0
        # A timeout no longer than the wait is what would have fired first.
        assert (
            await p.refused("session.events", ref(made, after=fake.cursor, wait=1.0), timeout=0.5)
            == "timeout"
        )


# ── oversized answers, through the channel ────────────────────────────


@pytest.mark.anyio
async def test_a_turn_of_300000_characters_is_delivered_clipped_and_older_turns_stay_pageable(
    tmp_path,
    monkeypatch,
    wire,
    fake,
    live,  # noqa: F811
):
    # 70 turns: ordinary ones, big non-BMP ones, one with thousands of tool entries, and the
    # newest alone is far over the whole budget.
    fake.turns = [
        op_turn(f"turn {i} " + EMOJI * (3000 if i % 3 == 0 else 5), f"reply {i}") for i in range(69)
    ]
    fake.turns[40]["tools"] = [
        {"name": f"tool-{i}", "outcome": "ok", "summary": "s" * 450} for i in range(6000)
    ]
    fake.turns.append(op_turn("N" * 300_000, "the newest reply"))
    ids = [t["turn_id"] for t in fake.turns]
    async with paired(tmp_path, monkeypatch) as p:
        made = await p.grant()
        wire.clear()
        snap = await p.call("session.snapshot", ref(made))
        assert len(sent(wire, snap)) <= sharing.BUDGET < ch.ANSWER_MAX
        newest = snap["turns"][-1]
        assert newest["turn_id"] == ids[-1] and newest["truncated"] is True
        assert newest["text"] == "N" * 32768 + "… [cut: 267232 more characters]"
        assert newest["reply"] == "the newest reply"
        assert len(snap["turns"]) > 1  # older turns ride along while they fit
        assert snap["omitted_turns"] == (70 - 50) + (50 - len(snap["turns"]))

        # Page back by the oldest turn id delivered, until the first turn.
        delivered = [t["turn_id"] for t in snap["turns"]]
        before, pages = delivered[0], 0
        while True:
            wire.clear()
            page = await p.call("session.history", ref(made, before=before))
            pages += 1
            assert len(sent(wire, page)) <= sharing.BUDGET
            assert page["turns"], "every page holds at least one turn"
            got = [t["turn_id"] for t in page["turns"]]
            delivered = got + delivered
            assert set(page) == {"link", "turns", "before", "has_more"}
            assert remote.check_history(page) is page
            if not page["has_more"]:
                assert page["before"] is None
                break
            assert page["before"] == got[0] != before  # the cursor moved, to what was delivered
            before = page["before"]
            assert pages < 70, "paging must terminate"
        assert delivered == ids  # every turn exactly once, in order, none dropped silently
        assert pages > 1

        # The turn with thousands of tool entries came through clipped and marked.
        wire.clear()
        page = await p.call("session.history", ref(made, before=ids[41]))
        heavy = page["turns"][-1]
        assert heavy["turn_id"] == ids[40] and heavy["truncated"] is True
        assert heavy["tools_omitted"] == 6000 - len(heavy["tools"]) > 0
        assert len(sent(wire, page)) <= sharing.BUDGET


@pytest.mark.anyio
async def test_a_turn_with_thousands_of_tool_entries_fits_the_envelope(
    tmp_path,
    monkeypatch,
    wire,
    fake,
    live,  # noqa: F811
):
    tools = [{"name": "shell", "outcome": "ok", "summary": EMOJI * 499} for _ in range(4000)]
    fake.turns = [op_turn("older"), op_turn("run them all", "done", tools=tools)]
    async with paired(tmp_path, monkeypatch) as p:
        made = await p.grant()
        wire.clear()
        snap = await p.call("session.snapshot", ref(made))
        assert len(sent(wire, snap)) <= sharing.BUDGET
        newest = snap["turns"][-1]
        assert newest["truncated"] is True and newest["text"] == "run them all"
        assert newest["tools_omitted"] == 4000 - len(newest["tools"]) > 0
        assert [t["turn_id"] for t in snap["turns"]] == [t["turn_id"] for t in fake.turns]
        assert remote.check_snapshot(snap) is snap


@pytest.mark.anyio
@pytest.mark.parametrize(
    "filler,boundary",
    [("a", 32768), (EMOJI, 8192)],
    ids=["at_the_first_limit", "at_a_halved_limit"],
)
async def test_a_secret_across_a_clip_boundary_never_appears_in_part(
    tmp_path,
    monkeypatch,
    wire,
    fake,
    live,  # noqa: F811
    filler,
    boundary,
):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [SECRET])
    # The secret starts 4 characters before the cut: clipped BEFORE redaction, "s3cr" would
    # survive. The projection is redacted first, so the cut lands inside the placeholder.
    size = 300_000 if filler == "a" else 20_000
    text = filler * (boundary - 4) + SECRET + filler * (size - boundary)
    fake.turns = [op_turn(text, f"echo {SECRET}")]
    async with paired(tmp_path, monkeypatch) as p:
        made = await p.grant()
        wire.clear()
        snap = await p.call("session.snapshot", ref(made))
        body = sent(wire, snap)
        turn = snap["turns"][0]
        assert turn["truncated"] is True
        assert turn["text"].startswith(filler * (boundary - 4) + "[sec… [cut: ")
        assert turn["reply"] == "echo [secret]"
        everything = b"".join(wire).decode()
        for part in (SECRET, SECRET[:4], SECRET[:6], SECRET[4:], "s3c"):
            assert part not in everything, part
        assert len(body) <= sharing.BUDGET


# ── share.list ────────────────────────────────────────────────────────


def _rows_for(monkeypatch, titles: dict[str, str]) -> None:
    """Make every granted session one its engine's scan lists, titled by the operator."""
    monkeypatch.setattr(
        sharing,
        "_session_rows",
        lambda engine: {
            key.partition(":")[2]: SimpleNamespace(
                uuid=key.partition(":")[2], archived=False, first_user_message="first message"
            )
            for key in titles
        },
    )
    index = {key: metadata.SessionMeta(title=title) for key, title in titles.items() if title}
    monkeypatch.setattr(metadata, "load", lambda path=None: index)


async def _all_pages(p: Pair, wire: list[bytes], limit: int = 50) -> tuple[list[dict], list[int]]:
    entries, sizes, after = [], [], None
    while True:
        wire.clear()
        args = {"limit": limit, **({"after": after} if after else {})}
        page = await p.call("share.list", args)
        sizes.append(len(sent(wire, page)))
        assert set(page) == {"generation", "grants", "next"}
        entries += page["grants"]
        if page["next"] is None:
            return entries, sizes
        assert page["grants"] and page["next"] == page["grants"][-1]["grant_id"]
        after = page["next"]
        assert len(sizes) <= 201, "paging must terminate"


@pytest.mark.anyio
async def test_200_grants_with_200_non_bmp_character_titles_all_arrive_within_the_cap(
    tmp_path,
    monkeypatch,
    wire,
    live,  # noqa: F811
):
    async with paired(tmp_path, monkeypatch) as p:
        p.pause_owner_rounds(monkeypatch)
        titles = {api_key(): EMOJI * 200 for _ in range(store.GRANTS_PER_PEER_MAX)}
        _rows_for(monkeypatch, titles)
        made = [await p.grant(key) for key in titles]
        with pytest.raises(grants.GrantError):
            await p.grant()  # the 201st

        entries, sizes = await _all_pages(p, wire)
        assert len(entries) == 200 and len(sizes) == 4
        assert max(sizes) <= sharing.BUDGET < ch.ANSWER_MAX  # as the channel serialized them
        assert min(sizes[:-1]) > 100 * 1024  # a count alone would have called these pages small
        assert [e["grant_id"] for e in entries] == sorted(g.grant_id for g in made)
        assert all(e["title"] == EMOJI * 200 for e in entries)
        by_id = {g.grant_id: g for g in made}
        for entry in entries:
            g = by_id[entry["grant_id"]]
            engine, _, native = g.session_key.partition(":")
            assert entry == {
                "grant_id": g.grant_id,
                "session": {"engine": engine, "id": native},
                "title": EMOJI * 200,
                "kind": "api",
                "level": "view",
                "expires_at": g.expires_at,
                "revision": 1,
            }  # no cwd, no project, no model

        # Under a budget a full page cannot meet, a page holds fewer than `limit` entries, at
        # least one, and `next` continues from there: still all 200, every page within budget.
        monkeypatch.setattr(sharing, "BUDGET", 20 * 1024)
        entries, sizes = await _all_pages(p, wire)
        assert len(entries) == 200 and len({e["grant_id"] for e in entries}) == 200
        assert max(sizes) <= 20 * 1024 and len(sizes) > 20
        monkeypatch.setattr(sharing, "BUDGET", 200 * 1024)

        # And the receiving side stores all 200 from those pages.
        assert await remote.refresh(p.b, p.a.peer_id) is True
        assert len(p.b.store.list_remote(p.a.peer_id)) == 200


@pytest.mark.anyio
async def test_the_list_holds_the_callers_own_live_grants_and_a_redacted_title(
    tmp_path,
    monkeypatch,
    wire,
    live,  # noqa: F811
):
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [SECRET])
    async with paired(tmp_path, monkeypatch) as p:
        keys = [api_key() for _ in range(5)]
        titles = {
            keys[0]: f"deploy with {SECRET} now",
            keys[1]: "two\nlines\tand " + "x" * 400,
            keys[2]: "",  # no operator title: the first message stands in
            keys[3]: "ended",
            keys[4]: "another peer's",
        }
        _rows_for(monkeypatch, titles)
        mine = [await p.grant(key) for key in keys[:4]]
        stranger = await asyncio.to_thread(trusted, p.a.store, "stranger")
        await asyncio.to_thread(
            grants.create,
            p.a.store,
            session_key=keys[4],
            peer_id=stranger.peer_id,
            level="view",
            expires_in=None,
        )
        await asyncio.to_thread(grants.revoke, p.a.store, mine[3].grant_id)
        generation = p.a.store.generation()

        entries, _ = await _all_pages(p, wire)
        by_key = {f"{e['session']['engine']}:{e['session']['id']}": e for e in entries}
        assert set(by_key) == set(keys[:3])  # not the ended one, not the other peer's
        assert by_key[keys[0]]["title"] == "deploy with [secret] now"
        assert by_key[keys[1]]["title"] == ("two lines and " + "x" * 400)[:200]
        assert by_key[keys[2]]["title"] == "first message"
        assert SECRET not in b"".join(wire).decode()
        assert (await p.call("share.list"))["generation"] == generation

        # A session that is no longer there to read is not listed (its grant would be refused).
        monkeypatch.setattr(sharing, "_session_rows", lambda engine: {})
        assert (await p.call("share.list"))["grants"] == []

        # Nothing is listed while redaction is unavailable.
        _rows_for(monkeypatch, titles)

        def unavailable():
            raise template_secrets.RedactionUnavailable("store unreadable")

        monkeypatch.setattr(template_secrets, "redaction_values", unavailable)
        wire.clear()
        assert await p.refused("share.list") == "unavailable"
        # (beta's own background refresh may have asked too: it got the same answer)
        bodies = data_bodies(wire)
        assert bodies and set(bodies) == {UNAVAILABLE}


# ── parity with the public view, and redaction ────────────────────────


@pytest.mark.anyio
async def test_the_peers_view_equals_the_public_view_projection_field_for_field(
    tmp_path,
    monkeypatch,
    fake,
    live,  # noqa: F811
):
    """The scripted snapshot of `test_share_api` (attachments, tools, a pending request, operator
    only fields): the peer's answer is `share_api.project` of it, except the four stated things
    and the one bit #2059 adds under a `view` grant (`owner_present`)."""
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [SECRET])
    fake.override = _operator_snapshot()
    for i, turn in enumerate(fake.override["turns"]):
        turn["turn_id"] = turn["operation_id"] = str(uuid.UUID(int=i + 1))
    async with paired(tmp_path, monkeypatch) as p:
        made = await p.grant()
        peer_view = await p.call("session.snapshot", ref(made))
        authors = shares.store().authors(made.session_key)
        public = share_api.project(fake.override, authors, share_api.redactor(), "a-public-share")

        assert set(peer_view) == set(public) | {"link", "can_send", "owner_present"}
        # 5. (#2059) one bit about the operator's own pane; a `view` grant is offered no choices
        assert peer_view["owner_present"] is False
        assert all("choices" not in q for q in peer_view["pending_requests"])
        # 1. the link id is the grant's
        assert peer_view["link"] == share_api.link_id(made.grant_id)
        assert peer_view["link"] != share_api.link_id("a-public-share")
        # 2. `can_send` is always false
        assert peer_view["can_send"] is False
        # 3. image handles are omitted: name and type only
        images = 0
        for turn in public["turns"]:
            for attachment in turn["attachments"]:
                images += "image" in attachment
                attachment.pop("image", None)
        assert images == 1
        assert all(set(a) == {"name", "mime"} for t in peer_view["turns"] for a in t["attachments"])
        # 4. nothing here is over the byte budget, so nothing is clipped
        assert '"truncated"' not in json.dumps(peer_view)
        for field in public:
            assert peer_view[field] == public[field], field
        assert SECRET not in json.dumps(peer_view) and "[secret]" in json.dumps(peer_view)
        for leak in ("secret-project/x.py", "w-uuid", "native_id", "mission_id", "saved_in"):
            assert leak not in json.dumps(peer_view), leak


@pytest.mark.anyio
async def test_a_real_sessions_peer_view_equals_its_public_view_link(
    tmp_path,
    monkeypatch,
    wire,
    env,  # noqa: F811
    host,  # noqa: F811
    project,  # noqa: F811
    live,  # noqa: F811
):
    """End to end on the native runtime: the same session read through a public `view` link and
    through a peer's grant. A planted secret reaches neither."""
    monkeypatch.setattr(template_secrets, "redaction_values", lambda: [SECRET])
    key = await _session("codex", project)
    first = ident()
    await runtime.submit_turn(key, operation_id=first, text=f"deploy with {SECRET}")
    await settle(key, first)
    sender, _ = await _visitor(env, key)  # a `send` link, so one turn has a visitor author
    async with contextlib.aclosing(sender):
        r = await sender.post(
            "/api/public/turns", json={"client_key": str(uuid.uuid4()), "text": "from a visitor"}
        )
        assert r.status_code in (200, 202), r.text
    (visitor_op,) = shares.store().authors(key)
    await settle(key, visitor_op)
    viewer, _ = await _visitor(env, key, mode="view")

    async with paired(tmp_path, monkeypatch) as p:
        monkeypatch.setattr(
            sharing,
            "_session_rows",
            lambda engine: {
                key.partition(":")[2]: SimpleNamespace(
                    uuid=key.partition(":")[2],
                    archived=False,
                    first_user_message=f"deploy with {SECRET}",
                )
            },
        )
        made = await p.grant(key)
        wire.clear()
        async with contextlib.aclosing(viewer):
            public = (await viewer.get("/api/public/session")).json()
        peer_view = await p.call("session.snapshot", ref(made))
        listed = await p.call("share.list")
        history = await p.call("session.history", ref(made, before=visitor_op))
        events = await p.call("session.events", ref(made, after=0))

        projection = set(share_api.project({}, {}, share_api._Redactor([]), "x"))
        assert set(peer_view) == projection | {"link", "can_send", "owner_present"}
        assert projection <= set(public)
        for field in projection:
            assert peer_view[field] == public[field], field
        assert peer_view["can_send"] is False and public["can_send"] is False
        assert peer_view["link"] != public["link"]
        authors = [t["author"] for t in peer_view["turns"]]
        assert authors == [None, {"name": "Alex", "via": "public_link"}]

        # The secret is in the operator's own snapshot, and in nothing a peer was sent.
        raw = await runtime.snapshot(key)
        assert any(SECRET in (t.get("text") or "") for t in raw["turns"])
        assert SECRET not in b"".join(wire).decode()
        assert "[secret]" in peer_view["turns"][0]["text"]
        assert listed["grants"][0]["title"] == "deploy with [secret]"
        assert [t["turn_id"] for t in history["turns"]] == [first]
        assert history["turns"][0] == peer_view["turns"][0]
        assert history["has_more"] is False and history["before"] is None
        assert events["events"] and all(set(e) == {"cursor", "kind"} for e in events["events"])
        assert remote.check_events(events) is events
        for leak in ("cwd", "native", "operation_id", str(project)):
            assert leak not in json.dumps([peer_view, history, events, listed]), leak

        # A turn on the real runtime wakes a parked wait.
        cursor = peer_view["event_cursor"]
        parked = asyncio.create_task(
            p.call("session.events", ref(made, after=cursor, wait=20), timeout=30)
        )
        await until(lambda: p.a.waits.get(p.b.peer_id) == 1)
        started = time.monotonic()
        third = ident()
        await runtime.submit_turn(key, operation_id=third, text="wake up")
        page = await parked
        assert page["events"] and page["next_cursor"] > cursor
        assert time.monotonic() - started < 10
        await settle(key, third)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["redaction", "secret_key"])
async def test_nothing_is_sent_while_redaction_is_unavailable(
    tmp_path,
    monkeypatch,
    wire,
    fake,
    live,  # noqa: F811
    failure,
):
    fake.turns = [op_turn("older"), op_turn("content that must not leave")]
    fake.emit()
    async with paired(tmp_path, monkeypatch) as p:
        made = await p.grant()
        assert (await p.call("session.snapshot", ref(made)))["turns"]

        def unavailable(*_a, **_k):
            raise template_secrets.RedactionUnavailable("store unreadable")

        def no_key(*_a, **_k):
            raise template_secrets.SecretKeyUnavailable("key unreadable")

        if failure == "redaction":
            monkeypatch.setattr(template_secrets, "redaction_values", unavailable)
            verbs = ("session.snapshot", "session.history")
        else:
            monkeypatch.setattr(template_secrets, "keyed_digest", no_key)
            verbs = ("session.snapshot", "session.history", "session.events")
        wire.clear()
        for verb in verbs:
            extra = {"before": fake.turns[1]["turn_id"]} if verb == "session.history" else {}
            assert await p.refused(verb, ref(made, **extra)) == "unavailable", verb
        # (beta's own background `share.list` may be among them: it is refused the same way)
        bodies = data_bodies(wire)
        assert len(bodies) >= len(verbs) and set(bodies) == {UNAVAILABLE}
        assert b"must not leave" not in b"".join(wire)


# ── two real instances share an API session (#2054) ───────────────────


def _pair_instances(a: Instance, b: Instance) -> tuple[str, str]:
    """Pair two running instances through their operator APIs. Returns their peer ids."""
    invite = a.post("/invites").json()
    theirs = b.post("/pairings", {"invite": invite["invite"]}).json()["id"]

    def confirming(inst, pairing_id):
        return any(
            p["id"] == pairing_id and p["state"] == "confirm" for p in inst.state()["pairings"]
        )

    _wait(lambda: confirming(b, theirs))
    _wait(lambda: confirming(a, invite["id"]))
    assert a.post(f"/pairings/{invite['id']}/confirm").status_code == 200
    assert b.post(f"/pairings/{theirs}/confirm").status_code == 200

    def connected(inst):
        peers = inst.state()["peers"]
        return peers and peers[0]["connected"] and peers[0]["last_seen_at"]

    _wait(lambda: connected(a))
    _wait(lambda: connected(b))
    return a.state()["self"]["peer_id"], b.state()["self"]["peer_id"]


def test_two_real_instances_grant_list_read_wake_a_long_poll_and_revoke(
    two_instances,  # noqa: F811
    host,  # noqa: F811
    project,  # noqa: F811
    monkeypatch,
):
    a, b = two_instances
    a_id, b_id = _pair_instances(a, b)
    monkeypatch.setattr(shares, "session_state", lambda prov, native: "live")
    loop = a.server.servers[0].get_loop()

    def on_a(coro):
        """The session lives in alpha: everything of the runtime runs on alpha's event loop."""
        return asyncio.run_coroutine_threadsafe(coro, loop).result(60)

    key = on_a(runtime.create_session(ENGINES["codex"], str(project), operation_id=ident()))[
        "session_key"
    ]
    first = ident()
    on_a(runtime.submit_turn(key, operation_id=first, text="hello from the owner"))
    on_a(settle(key, first))
    engine, _, native = key.partition(":")
    monkeypatch.setattr(
        sharing,
        "_session_rows",
        lambda _engine: {
            native: SimpleNamespace(
                uuid=native, archived=False, first_user_message="hello from the owner"
            )
        },
    )

    def remote_rows() -> list[dict]:
        r = b.http.get("/api/federation/remote", headers=b.read)
        assert r.status_code == 200, r.text
        return r.json()["remote"]

    assert remote_rows() == []  # paired, and nothing is shared

    # grant: the owner's operator shares the session with beta
    made = a.post("/grants", {"session_key": key, "peer_id": b_id, "level": "view"})
    assert made.status_code == 201, made.text
    grant = made.json()["grant"]

    # list: beta learns of it over the channel and shows it under a remote key
    (row,) = _wait(remote_rows)
    assert row["key"] == f"peer:{a_id}/{engine}:{native}" and row["grant_id"] == grant["grant_id"]
    assert row["title"] == "hello from the owner" and row["level"] == "view"
    assert row["connected"] is True and row["revision"] == 1
    # alpha's own session list is alpha's: beta has no such session, only the remote row
    assert not any(
        s["id"].endswith(native)
        for s in b.http.get("/api/sessions", headers=b.read).json()["sessions"]
    )

    # snapshot: beta's operator reads it through beta's own authenticated API
    base = f"/api/federation/remote/{a_id}/{grant['grant_id']}"
    assert b.http.get(f"{base}/snapshot").status_code == 401  # beta's session is still required
    got = b.http.get(f"{base}/snapshot", headers=b.read)
    assert got.status_code == 200, got.text
    snap = got.json()
    assert [t["text"] for t in snap["turns"]] == ["hello from the owner"]
    assert snap["turns"][0]["reply"] and snap["can_send"] is False
    assert "cwd" not in snap and str(project) not in got.text

    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

    def held(after: int) -> concurrent.futures.Future:
        return pool.submit(
            b.http.get, f"{base}/events?after={after}&wait=20", headers=b.read, timeout=40
        )

    try:
        # an event wakes the long-poll: a turn on alpha answers beta's parked read
        parked = held(snap["event_cursor"])
        _wait(lambda: a.fed.waits.get(b_id) == 1)
        started = time.monotonic()
        second = ident()
        on_a(runtime.submit_turn(key, operation_id=second, text="a second turn"))
        woken = parked.result(30)
        assert woken.status_code == 200, woken.text
        page = woken.json()
        assert page["events"] and all(set(e) == {"cursor", "kind"} for e in page["events"])
        assert page["next_cursor"] > snap["event_cursor"]
        assert time.monotonic() - started < 15  # the event woke it, not the end of its 20 s
        on_a(settle(key, second))
        again = b.http.get(f"{base}/snapshot", headers=b.read).json()
        assert [t["text"] for t in again["turns"]] == ["hello from the owner", "a second turn"]
        older = b.http.get(f"{base}/history?before={second}", headers=b.read)
        assert older.status_code == 200 and older.json()["turns"] == again["turns"][:1]

        # revoke ends it: a parked read answers 404, and so does everything after it
        parked = held(again["event_cursor"])
        _wait(lambda: a.fed.waits.get(b_id) == 1)
        started = time.monotonic()
        assert a.post(f"/grants/{grant['grant_id']}/revoke").status_code == 200
        ended = parked.result(30)
        assert ended.status_code == 404 and ended.json()["code"] == "no_grant", ended.text
        assert time.monotonic() - started < 15
        assert "events" not in ended.json()
    finally:
        pool.shutdown(wait=True)
    assert b.http.get(f"{base}/snapshot", headers=b.read).status_code == 404
    _wait(lambda: remote_rows() == [])  # and it leaves beta's list
    assert b.http.get(f"{base}/events", headers=b.read).status_code == 404
    # the channel itself is still up: revoking a grant is not revoking the peer
    assert a.state()["peers"][0]["connected"] and a.state()["peers"][0]["state"] == "trusted"


# ── a change DURING the final check or the list scan (#2054 review) ───

RACES = ("revoke", "revision", "expire", "peer_revoked_by_another_process")


def _act(action: str, p: Pair, made: store.Grant, monkeypatch) -> None:
    """`_end_grant`, synchronously: called from inside the worker thread that is scanning."""
    if action == "revoke":
        grants.revoke(store.Store(p.a.dir), made.grant_id)  # another store object: any process
    elif action == "revision":
        grants.change(p.a.store, made.grant_id, expires_in=3600, revision=made.revision)
    elif action == "expire":
        late = time.time() + 8 * 86400
        monkeypatch.setattr(grants, "_clock", lambda: late)
    else:
        store.Store(p.a.dir).revoke(p.b.peer_id)


@pytest.mark.anyio
@pytest.mark.parametrize("action", RACES)
@pytest.mark.parametrize("verb", ["session.snapshot", "session.history", "session.events"])
async def test_a_change_during_the_final_session_scan_is_not_missed(
    tmp_path,
    monkeypatch,
    wire,
    fake,
    live,  # noqa: F811
    verb,
    action,
):
    """The final check reads the store, scans the session, and reads the store AGAIN: a revoke,
    a revision change or an expiry that lands inside that last scan refuses the answer that was
    already built."""
    older, newer = op_turn("older turn"), op_turn("built before the change")
    fake.turns = [older, newer]
    async with paired(tmp_path, monkeypatch) as p:
        p.pause_owner_rounds(monkeypatch)
        made = await p.grant()
        args = ref(made)
        if verb == "session.history":
            args["before"] = newer["turn_id"]
        elif verb == "session.events":
            args["after"] = 0
        await p.call(verb, args)  # the control: with nothing changing, the call is answered

        scans = []

        def scanning(prov, native):
            scans.append(native)
            if len(scans) == 2:  # the scan of the SECOND, final check
                _act(action, p, made, monkeypatch)
            return "live"

        monkeypatch.setattr(shares, "session_state", scanning)
        wire.clear()
        assert await p.refused(verb, args) == "no_grant"
        assert len(scans) == 2
        assert data_bodies(wire) == [NO_GRANT]
        assert b"built before the change" not in b"".join(wire)


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["revoke", "expire", "archive"])
async def test_a_grant_that_ends_while_the_list_is_built_is_not_listed(
    tmp_path,
    monkeypatch,
    wire,
    live,  # noqa: F811
    action,
):
    async with paired(tmp_path, monkeypatch) as p:
        p.pause_owner_rounds(monkeypatch)
        kept, gone = await p.grant(), await p.grant()
        await p.quiet_receiver(monkeypatch)
        titles = {kept.session_key: "kept title", gone.session_key: "the title that must not leave"}
        _rows_for(monkeypatch, titles)
        rows = sharing._session_rows
        done = []
        loop = asyncio.get_running_loop()

        def scanning(engine):
            if not done:
                done.append(action)
                if action == "archive":
                    monkeypatch.setenv("AGENT_SESSIONS_FEDERATION_DIR", str(p.a.dir))

                    async def archive():
                        async with shares.archiving(gone.session_key):
                            pass

                    asyncio.run_coroutine_threadsafe(archive(), loop).result(10)
                else:
                    _act(action, p, gone, monkeypatch)
                    if action == "expire":  # only `gone` expires: `kept` gets a longer life
                        monkeypatch.setattr(grants, "_clock", time.time)
                        with p.a.store._write() as con:
                            con.execute(
                                "UPDATE grants SET expires_at = ? WHERE grant_id = ?",
                                (time.time() - 1, gone.grant_id),
                            )
            return rows(engine)

        monkeypatch.setattr(sharing, "_session_rows", scanning)
        wire.clear()
        page = await p.call("share.list", {"limit": 50})
        assert done == [action]
        assert [e["grant_id"] for e in page["grants"]] == [kept.grant_id]
        assert b"the title that must not leave" not in b"".join(wire)
        assert gone.session_key.partition(":")[2].encode() not in b"".join(wire)


@pytest.mark.anyio
async def test_a_revision_change_while_the_list_is_built_is_listed_at_the_new_revision(
    tmp_path,
    monkeypatch,
    live,  # noqa: F811
):
    async with paired(tmp_path, monkeypatch) as p:
        p.pause_owner_rounds(monkeypatch)
        made = await p.grant()
        _rows_for(monkeypatch, {made.session_key: "a title"})
        rows = sharing._session_rows
        done = []

        def scanning(engine):
            if not done:
                done.append(1)
                _act("revision", p, made, monkeypatch)
            return rows(engine)

        monkeypatch.setattr(sharing, "_session_rows", scanning)
        page = await p.call("share.list", {"limit": 50})
        current = p.a.store.get_grant(made.grant_id)
        assert [e["revision"] for e in page["grants"]] == [current.revision]
        assert current.revision != made.revision
        assert page["generation"] == p.a.store.live_grants_of(p.b.peer_id)[0]


@pytest.mark.anyio
async def test_a_list_whose_grants_never_hold_still_is_unavailable_not_stale(
    tmp_path,
    monkeypatch,
    live,  # noqa: F811
):
    async with paired(tmp_path, monkeypatch) as p:
        p.pause_owner_rounds(monkeypatch)
        made = await p.grant()
        _rows_for(monkeypatch, {made.session_key: "a title"})
        rows = sharing._session_rows

        import threading

        # Overlapping `share.list` scans each run this read-then-change; unserialised, one loses
        # the revision race and raises "changed since it was read" (an `internal` answer, not the
        # `unavailable` under test). The lock keeps every scan's change a clean revision bump.
        changing = threading.Lock()

        def scanning(engine):
            with changing:
                now = p.a.store.get_grant(made.grant_id)
                grants.change(p.a.store, made.grant_id, expires_in=3600, revision=now.revision)
            return rows(engine)

        monkeypatch.setattr(sharing, "_session_rows", scanning)
        assert await p.refused("share.list", {"limit": 50}) == "unavailable"
