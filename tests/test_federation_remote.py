"""The receiving side of a shared session (#2054): the remote key, the mirror and its refresh, the
reads, and the proof that no local consumer takes a remote session for one of its own.

The refresh tests script the owner's answers (`Scripted` stands where the live channel is), so a
torn, malformed or failing list can be produced on demand; the trigger tests use two real
services over the in-memory transport.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import math
import os
import time
import urllib.parse
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from fastapi.testclient import TestClient

from agent_sessions import (
    engines,
    needs_you_notify,
    notifications,
    runtime_cleanup,
    sessionlock,
    shares,
)
from agent_sessions.federation import channel as ch
from agent_sessions.federation import grants, noise, remote, store
from agent_sessions.main import create_app
from agent_sessions.routes import federation as routes_federation
from agent_sessions.routes import missions as mission_routes
from federation_helpers import until
from test_federation_grants import api_key, live, trusted  # noqa: F401 — `live` is a fixture
from test_federation_routes import _login
from test_federation_service import _Node, _stop
from test_federation_sharing import FakeRuntime, _rows_for, op_turn, paired, ref

LOCAL = "claude:11111111-1111-1111-1111-111111111111"  # a session `fake_jsonl` lays down
SRC = Path(remote.__file__).resolve().parents[1]
#: A listed expiry a week ahead: what an owner's default grant carries.
LATER = float(int(time.time()) + 7 * 86400)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── the remote key ────────────────────────────────────────────────────


def test_the_remote_key_is_built_and_parsed_here_and_is_never_a_local_key():
    peer_id, native = "a" * 32, str(uuid.uuid4())
    key = remote.key(peer_id, "codex-api", native)
    assert key == f"peer:{peer_id}/codex-api:{native}"
    assert remote.parse(key) == (peer_id, "codex-api", native)
    assert remote.is_remote(key) and not remote.is_remote(f"codex-api:{native}")
    with pytest.raises(engines.EngineError):
        engines.parse_key(key)  # the one gate every local consumer uses refuses it
    with pytest.raises(engines.EngineError):
        engines.canonical_key(key)

    # An engine this instance does not have: the id's shape is all that can be asked.
    assert remote.parse(remote.key(peer_id, "other-agent", "abc.DEF_1-2")) == (
        peer_id,
        "other-agent",
        "abc.DEF_1-2",
    )
    bad_parts = [
        ("A" * 32, "codex-api", native),  # not a peer id
        (peer_id[:-1], "codex-api", native),
        (peer_id, "peer", native),  # the reserved namespace is never an engine
        (peer_id, "Codex", native),
        (peer_id, "x", native),
        (peer_id, "codex-api/..", native),
        (peer_id, "codex-api", "not-a-uuid"),  # this instance's own pattern for that engine
        (peer_id, "codex-api", native + "\n"),
        (peer_id, "other-agent", "../../etc/passwd"),
        (peer_id, "other-agent", "a:b"),
        (peer_id, "other-agent", "a/b"),
        (peer_id, "other-agent", "x" * 129),
        (peer_id, "other-agent", ""),
        (peer_id, "other-agent", None),
    ]
    for parts in bad_parts:
        with pytest.raises(ValueError):
            remote.key(*parts)
    for bad in (
        None,
        7,
        "",
        f"codex-api:{native}",
        f"peer:{peer_id}",
        f"peer:{peer_id}/codex-api",
        f"peer:{peer_id}/codex-api:{native}/more",
        f"peer:{peer_id}/codex-api:{native}:more",
        f"peer:{peer_id}//codex-api:{native}",
        f"PEER:{peer_id}/codex-api:{native}",
        f" peer:{peer_id}/codex-api:{native}",
        f"peer:{peer_id}/codex-api:{native}\n",
    ):
        with pytest.raises(ValueError):
            remote.parse(bad)


def test_the_remote_module_never_hands_a_key_to_the_local_gate():
    tree = ast.parse(inspect.getsource(remote))
    called = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not called & {"parse_key", "canonical_key", "session_key", "physical_key"}
    assert "id_pattern" in called  # the registry's own id pattern, asked directly


def test_only_the_federation_store_reads_the_mirror():
    """The mirror is a table of its own: no session scan, lock, archive, cleanup, mission or
    notification module names it, and only the federation routes import the module that does."""
    naming_table, importing = [], []
    for py in SRC.rglob("*.py"):
        text = py.read_text()
        rel = py.relative_to(SRC).as_posix()
        if "remote_shares" in text:
            naming_table.append(rel)
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and any(a.name == "remote" for a in node.names):
                if (node.module or "").endswith("federation") or (
                    node.level and py.parent.name == "federation"
                ):
                    importing.append(rel)
    # the store holds the table; `remote.py` documents it; nothing outside `federation/` names it
    assert sorted(naming_table) == ["federation/remote.py", "federation/store.py"]
    assert sorted(importing) == ["federation/service.py", "routes/federation.py"]


# ── validating what a peer lists ──────────────────────────────────────


def entry(i: int = 1, **over) -> dict:
    out = {
        "grant_id": f"grant-{i:017d}",
        "session": {"engine": "codex-api", "id": str(uuid.UUID(int=i))},
        "title": f"session {i}",
        "kind": "api",
        "level": "view",
        "expires_at": LATER,
        "revision": 1,
    }
    out.update(over)
    return out


def page(entries: list[dict], generation: int = 5, nxt: str | None = None) -> dict:
    return {"generation": generation, "grants": entries, "next": nxt}


def test_a_listed_entry_is_validated_field_by_field():
    assert remote.clean_entry(entry(7)) == {
        "grant_id": "grant-00000000000000007",
        "engine": "codex-api",
        "native_id": str(uuid.UUID(int=7)),
        "title": "session 7",
        "kind": "api",
        "level": "view",
        "expires_at": LATER,
        "revision": 1,
    }
    # A title is cleaned: one line, no control or bidi-override characters; empty is allowed.
    assert remote.clean_entry(entry(title="  a‮b\x07\n\tc  "))["title"] == "ab c"
    assert remote.clean_entry(entry(title=""))["title"] == ""
    assert remote.clean_entry(entry(title="\U0001f600" * 200))["title"] == "\U0001f600" * 200
    assert remote.clean_entry(entry(expires_at=int(LATER)))["expires_at"] == LATER

    # Forward compatibility: the two instances are updated independently. A key this build does
    # not know is ignored (and never stored); an entry with a level this build does not know is
    # skipped, keeping only its id for the page's order checks.
    assert remote.clean_entry({**entry(7), "cwd": "/home/op", "kind": "api"}) == remote.clean_entry(
        entry(7)
    )
    skipped = {"grant_id": "grant-00000000000000007", "skipped": True}
    assert remote.clean_entry(entry(7, level="co_drive")) == skipped
    # The kind (#2056) is optional. An owner on the build before it sends none: its grants are
    # all API grants. A kind this build does not know is skipped like an unknown level, never
    # stored as another kind; so is a level this build does not serve for a kind it knows.
    older = {k: v for k, v in entry(7).items() if k != "kind"}
    assert remote.clean_entry(older) == remote.clean_entry(entry(7))
    assert remote.clean_entry(older)["kind"] == "api"
    assert remote.clean_entry(entry(7, kind="terminal"))["kind"] == "terminal"
    assert remote.clean_entry(entry(7, kind="byte_stream")) == skipped
    assert remote.clean_entry(entry(7, kind="terminal", level="send")) == skipped

    native = str(uuid.uuid4())
    refused = [
        "not a dict",
        None,
        {**entry(), **{f"k{i}": i for i in range(20)}},  # more unknown keys than tolerated
        {k: v for k, v in entry().items() if k != "revision"},
        entry(grant_id="short"),
        entry(grant_id="../../../../etc/passwd"),
        entry(grant_id=7),
        entry(session=f"codex-api:{native}"),
        entry(session={"engine": "codex-api"}),
        entry(session={"engine": "codex-api", "id": native, "cwd": "/"}),
        entry(session={"engine": "peer", "id": native}),
        entry(session={"engine": "Codex", "id": native}),
        entry(session={"engine": "x" * 30, "id": native}),
        entry(session={"engine": 5, "id": native}),
        entry(session={"engine": "codex-api", "id": "not-a-uuid"}),
        entry(session={"engine": "other-agent", "id": "../x"}),
        entry(session={"engine": "other-agent", "id": "a:b"}),
        entry(session={"engine": "other-agent", "id": "x" * 129}),
        entry(session={"engine": "other-agent", "id": 5}),
        entry(title=None),
        entry(title=["a"]),
        entry(title="x" * 201),  # longer than a title after cleaning
        entry(title=" " * 801),  # longer than anything that could be one before it
        entry(level="Send!"),  # not even the shape of a level
        entry(level=None),
        # a kind (#2056) that is present must at least have the shape of one
        entry(kind="TERMINAL"),
        entry(kind="byte-stream!"),
        entry(kind=""),
        entry(kind="x" * 33),
        entry(kind=None),
        entry(kind=["terminal"]),
        entry(kind=7),
        entry(expires_at=True),
        entry(expires_at="tomorrow"),
        entry(expires_at=math.nan),
        entry(expires_at=math.inf),
        entry(expires_at=-1),
        entry(expires_at=10**400),
        entry(revision=0),
        entry(revision=True),
        entry(revision=1.5),
        entry(revision=2**60),
    ]
    for raw in refused:
        with pytest.raises(remote.Invalid):
            remote.clean_entry(raw)

    assert remote.clean_page(page([entry(1), entry(2)], nxt="grant-00000000000000002"))[0] == 5
    for raw in (
        None,
        [],
        {**page([]), "extra": 1},
        {"generation": 5, "grants": []},
        page([], generation=-1),
        page([], generation=True),
        page([], generation="5"),
        page([], generation=2**60),
        {"generation": 5, "grants": "many", "next": None},
        page([entry(i) for i in range(1, 52)]),  # more than a page may hold
        page([entry()], nxt="../x"),
        page([entry()], nxt=5),
        page([entry(), "junk"]),
    ):
        with pytest.raises(remote.Invalid):
            remote.clean_page(raw)


def test_a_listed_expiry_is_one_the_owner_could_have_set(monkeypatch):
    """#2055 review (P1): ``expires_at`` was accepted as any positive finite number, so a peer
    could list ``1e300``; the mirror stored it and the operator's browser threw building a date
    from it, in a row drawn outside the pane's error boundary. An owner's grant ends at most
    ``grants.MAX_EXPIRY_S`` after ITS clock, so the bound is that on this clock plus a year of
    slack for two clocks that disagree; anything later is a malformed field."""
    now = 1_800_000_000.0
    monkeypatch.setattr(grants, "_clock", lambda: now)
    assert remote.EXPIRES_AHEAD_MAX_S == grants.MAX_EXPIRY_S + remote.EXPIRY_CLOCK_SLACK_S
    assert remote.EXPIRY_CLOCK_SLACK_S == 365 * 86400
    bound = now + remote.EXPIRES_AHEAD_MAX_S
    # What an owner produces: the longest grant, the same with its clock a year ahead, and an
    # expiry its clock already passed (this side never decides expiry: the owner does).
    for ok in (now + grants.MAX_EXPIRY_S, bound, now - 3600, 1.0):
        assert remote.clean_entry(entry(expires_at=ok))["expires_at"] == ok
    for bad in (bound + 1, 1e300, 1e15, 8.64e12, 2**53, 10**300, 0, 0.0, -1e300):
        with pytest.raises(remote.Invalid):
            remote.clean_entry(entry(expires_at=bad))


# ── the refresh ───────────────────────────────────────────────────────


class Scripted:
    """Stands where a peer's live channel is and answers `share.list` from a script: a dict, an
    exception to raise, or a function of the call's arguments."""

    closed = False

    def __init__(self, *script) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, dict, float]] = []

    def close(self, reason: str = "") -> None:
        self.closed = True

    async def call(self, verb: str, args: dict | None = None, *, timeout: float = 20.0):
        self.calls.append((verb, dict(args or {}), timeout))
        step = self.script.pop(0)
        if callable(step) and not isinstance(step, type):
            step = step(args)
            if inspect.isawaitable(step):
                step = await step
        if isinstance(step, BaseException):
            raise step
        return step


class Receiver:
    """One service with a trusted peer whose "channel" is scripted, and two rows already
    mirrored for it."""

    def __init__(self, tmp_path) -> None:
        self.fed = _Node("beta", tmp_path, addresses=[], name="beta", static=_key())
        self.peer = trusted(self.fed.store, "owner")
        self.other = trusted(self.fed.store, "other")
        self.old = [remote.clean_entry(entry(900)), remote.clean_entry(entry(901))]
        self.fed.store.replace_remote(self.peer.peer_id, self.old, now=1.0)
        self.fed.store.replace_remote(self.other.peer_id, [remote.clean_entry(entry(950))])

    def script(self, *steps) -> Scripted:
        channel = Scripted(*steps)
        self.fed._channels[self.peer.peer_id] = channel
        return channel

    def rows(self) -> list[store.RemoteShare]:
        return self.fed.store.list_remote(self.peer.peer_id)

    def unchanged(self) -> bool:
        return [(r.grant_id, r.seen_at) for r in self.rows()] == [
            (r["grant_id"], 1.0) for r in self.old
        ]


def _key() -> X25519PrivateKey:
    return X25519PrivateKey.generate()


def last(entries: list[dict]) -> str:
    return entries[-1]["grant_id"]


@pytest.mark.anyio
async def test_a_complete_refresh_reads_every_page_and_replaces_the_rows_in_one_transaction(
    tmp_path,
):
    r = Receiver(tmp_path)
    one, two, three = (
        [entry(i) for i in range(1, 51)],
        [entry(i) for i in range(51, 101)],
        [entry(i) for i in range(101, 131)],
    )
    channel = r.script(page(one, nxt=last(one)), page(two, nxt=last(two)), page(three))
    before = time.time()
    assert await remote.refresh(r.fed, r.peer.peer_id) is True
    rows = r.rows()
    assert [row.grant_id for row in rows] == [e["grant_id"] for e in one + two + three]
    assert all(row.seen_at >= before for row in rows)  # the old rows are gone, all rows are new
    assert rows[0].engine == "codex-api" and rows[0].native_id == str(uuid.UUID(int=1))
    assert rows[0].title == "session 1" and rows[0].level == "view" and rows[0].revision == 1
    # another peer's rows are not this refresh's to touch
    assert len(r.fed.store.list_remote(r.other.peer_id)) == 1
    assert [(verb, args) for verb, args, _ in channel.calls] == [
        ("share.list", {"limit": 50}),
        ("share.list", {"limit": 50, "after": last(one)}),
        ("share.list", {"limit": 50, "after": last(two)}),
    ]
    # A peer one version ahead: an entry with a level this build does not know, and entries with
    # a key it does not know. The unknown level is not listed; the rest are; paging still ends.
    ahead = [
        {**entry(1), "kind": "api"},
        entry(2, level="co_drive"),
        {**entry(3), "note": "from a newer build"},
    ]
    r.script(page(ahead[:2], nxt=last(ahead[:2])), page(ahead[2:]))
    assert await remote.refresh(r.fed, r.peer.peer_id) is True
    assert [row.grant_id for row in r.rows()] == [ahead[0]["grant_id"], ahead[2]["grant_id"]]
    # an empty list is a complete answer too: nothing is shared any more
    r.script(page([]))
    assert await remote.refresh(r.fed, r.peer.peer_id) is True
    assert r.rows() == []
    await _stop(r.fed)


def _fifty(start: int) -> list[dict]:
    return [entry(i) for i in range(start, start + 50)]


FAILURES = {
    "the_call_fails": lambda: [ch.CallError("closed")],
    "the_call_times_out": lambda: [ch.CallError("timeout")],
    "the_owner_refuses": lambda: [ch.CallError("no_grant")],
    "a_later_page_fails": lambda: [page(_fifty(1), nxt=last(_fifty(1))), ch.CallError("timeout")],
    "an_entry_is_malformed": lambda: [
        page(_fifty(1), nxt=last(_fifty(1))),
        page([entry(51), entry(52, session={"engine": "peer", "id": "x"})]),
    ],
    "a_title_is_oversize": lambda: [page([entry(1, title="x" * 201)])],
    # #2055 review: a finite expiry no owner could have set (and no browser can turn into a date)
    "an_expiry_is_out_of_range": lambda: [page([entry(1), entry(2, expires_at=1e300)])],
    "the_page_is_not_a_page": lambda: [{"grants": "all of them"}],
    "more_than_200_entries": lambda: [
        page(_fifty(1), nxt=last(_fifty(1))),
        page(_fifty(51), nxt=last(_fifty(51))),
        page(_fifty(101), nxt=last(_fifty(101))),
        page(_fifty(151), nxt=last(_fifty(151))),
        page([entry(201)]),
    ],
    "a_page_does_not_advance": lambda: [page(_fifty(1), nxt="grant-00000000000000001")],
    "an_empty_page_claims_more": lambda: [page([], nxt="grant-00000000000000001")],
    "a_page_goes_backwards": lambda: [
        page(_fifty(51), nxt=last(_fifty(51))),
        page(_fifty(1)),
    ],
    "ids_are_out_of_order": lambda: [page([entry(2), entry(1)])],
    "an_id_is_listed_twice": lambda: [page([entry(1), entry(1)])],
    "a_session_is_listed_twice": lambda: [page([entry(1), entry(2, session=entry(1)["session"])])],
}


@pytest.mark.anyio
@pytest.mark.parametrize("failure", sorted(FAILURES))
async def test_a_failed_or_invalid_refresh_leaves_the_mirror_unchanged(tmp_path, failure):
    r = Receiver(tmp_path)
    channel = r.script(*FAILURES[failure]())
    assert await remote.refresh(r.fed, r.peer.peer_id) is False
    assert r.unchanged()
    assert channel.calls  # it did ask; what it was told was not stored
    await _stop(r.fed)


@pytest.mark.anyio
async def test_a_generation_torn_refresh_is_retried_three_times_then_keeps_the_old_rows(tmp_path):
    r = Receiver(tmp_path)

    def torn(generation: int) -> list[dict]:
        first = _fifty(1)
        return [page(first, generation, nxt=last(first)), page([entry(51)], generation + 1)]

    # The list changes between the two pages on every attempt: 1 try + 3 retries, then give up.
    channel = r.script(*torn(1), *torn(3), *torn(5), *torn(7), page([entry(1)], 9))
    assert await remote.refresh(r.fed, r.peer.peer_id) is False
    assert r.unchanged() and len(channel.calls) == 8 and len(channel.script) == 1

    # Torn once, then stable: the second reading is stored, and it is a whole one.
    stable = _fifty(1)
    channel = r.script(*torn(1), page(stable, 2, nxt=last(stable)), page([entry(51)], 2))
    assert await remote.refresh(r.fed, r.peer.peer_id) is True
    assert len(r.rows()) == 51 and len(channel.calls) == 4
    await _stop(r.fed)


@pytest.mark.anyio
async def test_a_refresh_that_hangs_times_out_and_changes_nothing(tmp_path, monkeypatch):
    r = Receiver(tmp_path)
    monkeypatch.setattr(remote, "REFRESH_TIMEOUT_S", 0.2)

    async def never(_args):
        await asyncio.sleep(30)

    r.script(never)
    started = time.monotonic()
    assert await remote.refresh(r.fed, r.peer.peer_id) is False
    assert time.monotonic() - started < 5 and r.unchanged()
    await _stop(r.fed)


@pytest.mark.anyio
async def test_a_refresh_stores_nothing_without_a_channel_a_trusted_peer_or_a_working_store(
    tmp_path, monkeypatch
):
    r = Receiver(tmp_path)
    assert await remote.refresh(r.fed, r.peer.peer_id) is False  # no channel at all
    assert r.unchanged()

    # The store cannot be written: the answer is dropped, the rows stay.
    def broken(*_a, **_k):
        raise store.StoreError("disk full")

    with monkeypatch.context() as m:
        m.setattr(r.fed.store, "replace_remote", broken)
        r.script(page([entry(1)]))
        assert await remote.refresh(r.fed, r.peer.peer_id) is False
    assert r.unchanged()

    # The peer is revoked while its list is in flight: the revoke removed its rows, and the
    # answer that arrives afterwards is not stored.
    def revoked_meanwhile(_args):
        r.fed.store.revoke(r.peer.peer_id)
        return page([entry(1)])

    r.script(revoked_meanwhile)
    assert await remote.refresh(r.fed, r.peer.peer_id) is False
    assert r.rows() == []
    assert r.fed.store.replace_remote(r.peer.peer_id, [remote.clean_entry(entry(1))]) is False
    assert len(r.fed.store.list_remote(r.other.peer_id)) == 1
    await _stop(r.fed)


def test_replacing_a_peers_rows_is_one_transaction(tmp_path):
    st = store.Store(tmp_path / "fed")
    peer = trusted(st)
    st.replace_remote(peer.peer_id, [remote.clean_entry(entry(1)), remote.clean_entry(entry(2))])
    # The second row cannot be written. The delete before it is rolled back with it.
    with pytest.raises(KeyError):
        st.replace_remote(peer.peer_id, [remote.clean_entry(entry(3)), {"grant_id": "broken"}])
    assert [r.grant_id for r in st.list_remote()] == [entry(1)["grant_id"], entry(2)["grant_id"]]
    # and neither can a duplicate: the primary key refuses it, the old rows stay
    twice = [remote.clean_entry(entry(4)), remote.clean_entry(entry(4))]
    with pytest.raises(Exception, match="UNIQUE"):
        st.replace_remote(peer.peer_id, twice)
    assert [r.grant_id for r in st.list_remote()] == [entry(1)["grant_id"], entry(2)["grant_id"]]


# ── what triggers a refresh ───────────────────────────────────────────


def _mirrored(p, made: store.Grant):
    return p.b.store.get_remote(p.a.peer_id, made.grant_id)


@pytest.mark.anyio
async def test_the_mirror_is_refreshed_when_the_channel_connects(
    tmp_path,
    monkeypatch,
    live,  # noqa: F811
):
    async with paired(tmp_path, monkeypatch) as p:
        p.pause_owner_rounds(monkeypatch)  # so the owner sends no hint in this test
        await asyncio.sleep(0.15)
        key = api_key()
        _rows_for(monkeypatch, {key: "shared before the channel came back"})
        made = await p.grant(key)
        assert _mirrored(p, made) is None
        # The channel drops and beta dials again: the hello is followed by a refresh.
        old = p.b.channel_for(p.a.peer_id)
        old.close("test")
        await until(lambda: _mirrored(p, made) is not None)
        assert p.b.channel_for(p.a.peer_id) is not old
        row = _mirrored(p, made)
        assert row.title == "shared before the channel came back" and row.revision == 1
        assert remote.listing(p.b) == [
            {
                "key": f"peer:{p.a.peer_id}/{key}",
                "peer_id": p.a.peer_id,
                "peer_name": "alpha",
                "connected": True,
                "grant_id": made.grant_id,
                "engine": "codex-api",
                "native_id": key.partition(":")[2],
                "title": "shared before the channel came back",
                "kind": "api",
                "level": "view",
                "expires_at": made.expires_at,
                "revision": 1,
                "seen_at": row.seen_at,
            }
        ]


@pytest.mark.anyio
async def test_the_mirror_is_refreshed_on_the_owners_hint(
    tmp_path,
    monkeypatch,
    live,  # noqa: F811
):
    async with paired(tmp_path, monkeypatch) as p:
        p.pause_owner_rounds(monkeypatch)
        await asyncio.sleep(0.15)
        key = api_key()
        _rows_for(monkeypatch, {key: "hinted"})
        made = await p.grant(key)
        assert _mirrored(p, made) is None
        p.a.notify_share_changed(p.b.peer_id)  # what the operator routes do after a change
        await until(lambda: _mirrored(p, made) is not None)
        # A hint that arrives while a refresh runs is not lost: another one follows it.
        await asyncio.to_thread(grants.revoke, p.a.store, made.grant_id)
        p.a.notify_share_changed(p.b.peer_id)
        p.a.notify_share_changed(p.b.peer_id)
        await until(lambda: _mirrored(p, made) is None)


@pytest.mark.anyio
async def test_the_mirror_is_refreshed_periodically_without_any_hint(
    tmp_path,
    monkeypatch,
    live,  # noqa: F811
):
    monkeypatch.setattr(remote, "REFRESH_EVERY_S", 0.1)  # 60 s, shortened
    assert remote.REFRESH_EVERY_S != 60.0
    async with paired(tmp_path, monkeypatch) as p:
        p.pause_owner_rounds(monkeypatch)
        await asyncio.sleep(0.15)
        key = api_key()
        _rows_for(monkeypatch, {key: "found by the round"})
        made = await p.grant(key)
        await until(lambda: _mirrored(p, made) is not None)  # beta's own dialer round asked


def test_the_periodic_refresh_is_every_60_seconds():
    assert remote.REFRESH_EVERY_S == 60.0 and remote.REFRESH_RETRIES == 3
    assert remote.ENTRIES_MAX == store.GRANTS_PER_PEER_MAX == 200


@pytest.mark.anyio
async def test_the_owner_hints_after_a_change_another_process_or_an_archive_wrote(
    tmp_path,
    monkeypatch,
    live,  # noqa: F811
):
    """No route ran in the owner process: its dialer round notices the store moved and tells
    exactly the peer whose grants changed."""
    async with paired(tmp_path, monkeypatch) as p:
        key = api_key()
        _rows_for(monkeypatch, {key: "granted elsewhere"})
        await asyncio.sleep(0.15)  # the owner's first round has seen the empty store
        elsewhere = store.Store(p.a.dir)  # another process over the owner's store
        made = await asyncio.to_thread(
            grants.create,
            elsewhere,
            session_key=key,
            peer_id=p.b.peer_id,
            level="view",
            expires_in=None,
        )
        await until(lambda: _mirrored(p, made) is not None)
        # An archive ends the grant in the store; the peer's list follows.
        monkeypatch.setenv("AGENT_SESSIONS_FEDERATION_DIR", str(p.a.dir))
        async with shares.archiving(key):
            pass
        await until(lambda: _mirrored(p, made) is None)
        # A revoked peer's rows leave the mirror with the revoke, on the receiving side.
        await asyncio.to_thread(p.b.store.revoke, p.a.peer_id)
        assert p.b.store.list_remote(p.a.peer_id) == [] and remote.listing(p.b) == []


@pytest.mark.anyio
async def test_the_owners_round_sweeps_an_expired_grant_and_the_peers_list_follows(
    tmp_path,
    monkeypatch,
    live,  # noqa: F811
):
    async with paired(tmp_path, monkeypatch) as p:
        key = api_key()
        _rows_for(monkeypatch, {key: "runs out"})
        made = await p.grant(key)
        await until(lambda: _mirrored(p, made) is not None)
        late = made.expires_at + 1
        monkeypatch.setattr(grants, "_clock", lambda: late)
        # Refused at use from this moment; the owner's next round then ends it in the store...
        assert await p.refused("session.snapshot", ref(made)) == "no_grant"
        await until(lambda: p.a.store.get_grant(made.grant_id).state == "expired")
        assert p.a.store.grant_events(made.grant_id)[-1]["actor"] == "system"
        # ...and that change is hinted like any other.
        await until(lambda: _mirrored(p, made) is None)


# ── reads ─────────────────────────────────────────────────────────────


def _snapshot_answer(**over) -> dict:
    doc = {
        "link": "l" * 24,
        "revision": 3,
        "event_cursor": 3,
        "state": "idle",
        "turns": [{"turn_id": str(uuid.uuid4()), "text": "hi", "tools": [{"risk": {"a": "b"}}]}],
        "omitted_turns": 0,
        "pending_requests": [],
        "can_send": False,
    }
    doc.update(over)
    return doc


def _deep(levels: int):
    value: object = "x"
    for _ in range(levels):
        value = {"k": value}
    return value


def test_a_peers_answers_are_checked_before_they_are_served():
    good = _snapshot_answer()
    assert remote.check_snapshot(good) is good
    for bad in (
        _snapshot_answer(can_send=True),  # a view grant never sends, whatever the peer says
        _snapshot_answer(can_send=None),
        _snapshot_answer(turns="all"),
        _snapshot_answer(turns=["text"]),
        _snapshot_answer(turns=[{"turn_id": "../../x"}]),
        _snapshot_answer(turns=[{}] * 1001),
        _snapshot_answer(pending_requests={"a": 1}),
        _snapshot_answer(omitted_turns=-1),
        _snapshot_answer(omitted_turns="0"),
        _snapshot_answer(revision="3"),
        _snapshot_answer(link=None),
        _snapshot_answer(link="l" * 65),
        _snapshot_answer(extra=_deep(13)),  # deeper than any projection is
    ):
        with pytest.raises(remote.Invalid):
            remote.check_snapshot(bad)

    events = {"link": "l", "revision": 4, "next_cursor": 4, "events": [{"cursor": 4, "kind": "t"}]}
    assert remote.check_events(events) is events
    for bad in (
        {**events, "events": [{"cursor": 4, "kind": "t", "data": {"text": "x"}}]},  # never `data`
        {**events, "events": [{"cursor": "4", "kind": "t"}]},
        {**events, "events": [{"cursor": 4, "kind": "k" * 65}]},
        {**events, "events": [{"cursor": 4, "kind": None}]},
        {**events, "events": [{"cursor": 4, "kind": "t"}] * 101},
        {**events, "next_cursor": -1},
        {**events, "session_key": "codex-api:x"},
        {k: v for k, v in events.items() if k != "link"},
    ):
        with pytest.raises(remote.Invalid):
            remote.check_events(bad)

    turn = str(uuid.uuid4())
    history = {"link": "l", "turns": [{"turn_id": turn}], "before": turn, "has_more": True}
    assert remote.check_history(history) is history
    assert remote.check_history({**history, "before": None, "has_more": False})
    for bad in (
        {**history, "before": None},  # more is claimed and there is nowhere to continue
        {**history, "before": "../x"},
        {**history, "has_more": 1},
        {**history, "turns": None},
        {**history, "worker_ends": []},
        {**history, "turns": [{"turn_id": turn, "x": _deep(13)}]},
    ):
        with pytest.raises(remote.Invalid):
            remote.check_history(bad)


@pytest.mark.anyio
async def test_a_read_goes_to_the_owner_with_the_mirrored_revision_and_refuses_bad_answers(
    tmp_path, monkeypatch
):
    r = Receiver(tmp_path)
    grant_id = r.old[0]["grant_id"]

    async def read(verb="session.snapshot", extra=None, check=remote.check_snapshot, **kw):
        return await remote.read(
            r.fed, r.peer.peer_id, grant_id, verb, extra or {}, check=check, **kw
        )

    async def code(**kw) -> str:
        with pytest.raises(remote.ReadError) as exc:
            await read(**kw)
        return exc.value.code

    # No channel: the peer is offline. Nothing is answered from the mirror.
    assert await code() == "peer_offline"

    good = _snapshot_answer()
    channel = r.script(good)
    assert await read() is good
    assert channel.calls == [
        ("session.snapshot", {"grant_id": grant_id, "revision": 1}, ch.CALL_TIMEOUT_S)
    ]
    # The events read carries its own timeout: the wait plus the margin.
    events = {"link": "l", "revision": 4, "next_cursor": 4, "events": []}
    channel = r.script(events)
    assert (
        await read(
            "session.events",
            {"after": 4, "limit": 100, "wait": 20.0},
            remote.check_events,
            timeout=remote.events_timeout(20.0),
        )
        is events
    )
    assert channel.calls[0][1] == {
        "grant_id": grant_id,
        "revision": 1,
        "after": 4,
        "limit": 100,
        "wait": 20.0,
    }
    assert channel.calls[0][2] == 30.0

    # Malformed and oversize answers are refused here and reach nobody.
    r.script(_snapshot_answer(can_send=True))
    assert await code() == "bad_answer"
    r.script({"link": "l", "events": [{"cursor": 1, "kind": "text", "data": "leak"}]})
    assert await code(verb="session.events", check=remote.check_events) == "bad_answer"
    r.script(ch.CallError("too_large"))  # the channel's own cap on an answer
    assert await code() == "too_large"
    r.script(ch.CallError("closed"))
    assert await code() == "peer_offline"
    r.script(ch.CallError("unavailable"))
    assert await code() == "unavailable"
    assert r.unchanged()  # a read never writes the mirror

    # Not mirrored, or mirrored for a peer that is no longer trusted: refused without a call.
    channel = r.script(good)
    unknown = "grant-99999999999999999"
    with pytest.raises(remote.ReadError) as exc:
        await remote.read(
            r.fed, r.peer.peer_id, unknown, "session.snapshot", {}, check=remote.check_snapshot
        )
    assert exc.value.code == "no_grant"
    with pytest.raises(remote.ReadError) as exc:
        await remote.read(
            r.fed, r.other.peer_id, grant_id, "session.snapshot", {}, check=remote.check_snapshot
        )
    assert exc.value.code == "no_grant" and channel.calls == []
    await _stop(r.fed)


# ── a refusal is re-resolved before it is final (#2055) ───────────────


class Refused:
    """A receiver whose owner refuses the first call of a read, and what follows is scripted."""

    def __init__(self, tmp_path) -> None:
        self.r = Receiver(tmp_path)
        self.grant_id = self.r.old[0]["grant_id"]

    async def read(self):
        return await remote.read(
            self.r.fed,
            self.r.peer.peer_id,
            self.grant_id,
            "session.snapshot",
            {},
            check=remote.check_snapshot,
        )

    async def code(self) -> str:
        with pytest.raises(remote.ReadError) as exc:
            await self.read()
        return exc.value.code

    def row(self):
        return self.r.fed.store.get_remote(self.r.peer.peer_id, self.grant_id)


@pytest.mark.anyio
async def test_a_refused_read_refreshes_the_mirror_once_and_retries_once_with_the_new_revision(
    tmp_path,
):
    f = Refused(tmp_path)
    good = _snapshot_answer()
    renewed = [entry(900, revision=2), entry(901)]
    channel = f.r.script(ch.CallError("no_grant"), page(renewed), good)
    assert await f.read() is good
    assert [(verb, args.get("revision")) for verb, args, _t in channel.calls] == [
        ("session.snapshot", 1),
        ("share.list", None),
        ("session.snapshot", 2),
    ]
    assert f.row().revision == 2
    await _stop(f.r.fed)


@pytest.mark.anyio
async def test_a_grant_the_refreshed_list_no_longer_has_is_404_without_a_second_call(tmp_path):
    f = Refused(tmp_path)
    channel = f.r.script(ch.CallError("no_grant"), page([entry(901)]))
    assert await f.code() == "no_grant"
    assert [verb for verb, _a, _t in channel.calls] == ["session.snapshot", "share.list"]
    assert f.row() is None  # the refresh took the row out: the next list read has no such row
    # ...and the next read of it is refused without asking anybody.
    channel = f.r.script()
    assert await f.code() == "no_grant" and channel.calls == []
    await _stop(f.r.fed)


@pytest.mark.anyio
async def test_a_second_refusal_is_final_and_there_is_no_third_call(tmp_path):
    f = Refused(tmp_path)
    channel = f.r.script(
        ch.CallError("no_grant"),
        page([entry(900, revision=7), entry(901)]),
        ch.CallError("no_grant"),
        _snapshot_answer(),  # never asked for
    )
    assert await f.code() == "no_grant"
    assert [(verb, args.get("revision")) for verb, args, _t in channel.calls] == [
        ("session.snapshot", 1),
        ("share.list", None),
        ("session.snapshot", 7),
    ]
    await _stop(f.r.fed)


@pytest.mark.anyio
@pytest.mark.parametrize("failure", ["call fails", "malformed", "channel gone", "hangs"])
async def test_a_refresh_that_fails_after_a_refusal_is_503_and_serves_nothing(
    tmp_path, monkeypatch, failure
):
    f = Refused(tmp_path)
    fed, peer_id = f.r.fed, f.r.peer.peer_id

    def gone(_args):
        fed._channels.pop(peer_id, None)
        return ch.CallError("closed")

    async def hang(_args):
        await asyncio.sleep(30)

    if failure == "hangs":
        monkeypatch.setattr(remote, "RE_RESOLVE_TIMEOUT_S", 0.05)
    step = {
        "call fails": ch.CallError("timeout"),
        "malformed": {"generation": "x", "grants": [], "next": None},
        "channel gone": gone,
        "hangs": hang,
    }[failure]
    channel = f.r.script(ch.CallError("no_grant"), step, _snapshot_answer())
    code = await f.code()
    assert code == ("peer_offline" if failure == "channel gone" else "unavailable")
    assert routes_federation._READ_STATUS[code] == 503
    # Nothing was served: the answer still in the script was never asked for, and the mirror is
    # exactly what it was (a failure decides nothing about the grant).
    assert [verb for verb, _a, _t in channel.calls] == ["session.snapshot", "share.list"]
    assert f.r.unchanged()
    await _stop(fed)


@pytest.mark.anyio
async def test_a_refused_read_for_a_peer_revoked_meanwhile_is_404(tmp_path):
    f = Refused(tmp_path)
    fed, peer_id = f.r.fed, f.r.peer.peer_id

    def revoked(_args):
        fed.store.revoke(peer_id)
        return page([entry(900, revision=2)])

    f.r.script(ch.CallError("no_grant"), revoked, _snapshot_answer())
    assert await f.code() == "no_grant"
    await _stop(fed)


@pytest.mark.anyio
async def test_a_grant_renewed_on_the_owner_keeps_a_read_working(
    tmp_path,
    monkeypatch,
    live,  # noqa: F811
):
    fake = FakeRuntime().install(monkeypatch)
    fake.turns = [op_turn("hello")]
    async with paired(tmp_path, monkeypatch) as p:
        key = api_key()
        _rows_for(monkeypatch, {key: "changing"})
        made = await p.grant(key)
        await until(lambda: _mirrored(p, made) is not None)

        async def read() -> dict:
            return await remote.read(
                p.b, p.a.peer_id, made.grant_id, "session.snapshot", {}, check=remote.check_snapshot
            )

        assert (await read())["turns"][0]["text"] == "hello"
        # The owner renews the grant and its hint is lost (its rounds are paused): the mirror
        # still says revision 1.
        p.pause_owner_rounds(monkeypatch)
        await asyncio.sleep(0.15)
        await asyncio.to_thread(
            grants.change, p.a.store, made.grant_id, expires_in=3600, revision=1
        )
        assert _mirrored(p, made).revision == 1
        # The read is refused once, re-resolved, and answered under revision 2: the operator
        # never sees the refusal.
        assert (await read())["turns"][0]["text"] == "hello"
        assert _mirrored(p, made).revision == 2
        assert await p.refused("session.snapshot", ref(made)) == "no_grant"  # 1 stays refused


@pytest.mark.anyio
async def test_a_truly_ended_grant_is_404_and_its_row_is_gone_from_the_mirror(
    tmp_path,
    monkeypatch,
    live,  # noqa: F811
):
    fake = FakeRuntime().install(monkeypatch)
    fake.turns = [op_turn("hello")]
    async with paired(tmp_path, monkeypatch) as p:
        key = api_key()
        _rows_for(monkeypatch, {key: "ending"})
        made = await p.grant(key)
        await until(lambda: _mirrored(p, made) is not None)
        p.pause_owner_rounds(monkeypatch)  # the owner's hint is lost
        await asyncio.sleep(0.15)
        await asyncio.to_thread(grants.revoke, p.a.store, made.grant_id)
        assert _mirrored(p, made) is not None  # the mirror has not heard
        with pytest.raises(remote.ReadError) as exc:
            await remote.read(
                p.b, p.a.peer_id, made.grant_id, "session.snapshot", {}, check=remote.check_snapshot
            )
        assert exc.value.code == "no_grant"
        assert _mirrored(p, made) is None
        assert remote.listing(p.b) == []


# ── no local consumer takes a remote session for a local one ──────────


@pytest.fixture
def remote_row(fake_jsonl):
    """A mirrored remote session whose engine and native id are those of a REAL local session
    (`LOCAL`), in the store the app itself uses. Returns ``(remote key, store, peer)``."""
    st = store.Store()
    peer = st.trust(noise.public_bytes(_key()), name="owner", endpoints=[])
    engine, _, native = LOCAL.partition(":")
    row = {
        "grant_id": store.new_grant_id(),
        "engine": engine,
        "native_id": native,
        "title": "a remote session",
        "kind": "api",
        "level": "view",
        "expires_at": time.time() + 3600,
        "revision": 1,
    }
    assert st.replace_remote(peer.peer_id, [row]) is True
    return remote.key(peer.peer_id, engine, native), st, peer


def _local_is_live(fake_jsonl) -> bool:
    live_tree = fake_jsonl / ".claude" / "projects" / "-home-user-claude-repo-a"
    return (live_tree / f"{LOCAL.partition(':')[2]}.jsonl").exists()


def _operator(auth_cfg) -> tuple[TestClient, dict]:
    client = TestClient(create_app(auth_cfg), base_url="https://testserver")
    return client, _login(client, auth_cfg)


def test_the_session_lock_refuses_a_remote_key(remote_row):
    key, st, _peer = remote_row
    before = sorted(os.listdir(sessionlock.lock_dir()))
    for take in (sessionlock.acquire, sessionlock.is_locked, sessionlock.lock_path):
        with pytest.raises(ValueError, match="remote session"):
            take(key)
    assert sorted(os.listdir(sessionlock.lock_dir())) == before  # no lock file was made for it
    # the local session of the same engine and id locks as it always has
    lock = sessionlock.acquire(LOCAL)
    assert lock is not None and sessionlock.is_locked(LOCAL)
    lock.release()
    assert len(st.list_remote()) == 1


def test_the_session_scanner_never_lists_a_remote_row(auth_cfg, remote_row):
    key, st, _peer = remote_row
    client, _hdr = _operator(auth_cfg)
    listed = client.get("/api/sessions").json()
    ids = [s["id"] for s in listed["sessions"]]
    assert LOCAL in ids and len(ids) == 4  # the live `fake_jsonl` sessions, and nothing else
    assert not any(remote.is_remote(i) for i in ids)
    assert "a remote session" not in repr(listed) and "peer:" not in repr(listed)
    for path in (key, urllib.parse.quote(key, safe="")):
        assert client.get(f"/api/sessions/{path}").status_code == 404
    assert len(st.list_remote()) == 1


def test_archive_refuses_a_remote_key_and_leaves_the_local_namesake_alone(
    auth_cfg, remote_row, fake_jsonl
):
    key, st, _peer = remote_row
    client, hdr = _operator(auth_cfg)
    for path in (key, urllib.parse.quote(key, safe="")):
        for action in ("archive", "unarchive"):
            r = client.post(f"/api/sessions/{path}/{action}", headers=hdr)
            assert r.status_code in (404, 405), (path, action, r.status_code)
    assert _local_is_live(fake_jsonl)  # the local session with the same engine and id is not moved
    assert [r.title for r in st.list_remote()] == ["a remote session"]
    # and the real key still archives, so the refusals above are about the prefix
    assert client.post(f"/api/sessions/{LOCAL}/archive", headers=hdr).status_code == 200
    assert not _local_is_live(fake_jsonl)
    assert len(st.list_remote()) == 1  # archiving the namesake does not touch the mirror


def test_runtime_cleanup_ignores_a_remote_key(remote_row, fake_jsonl):
    key, st, peer = remote_row
    rest = key[len("peer:") :]
    # As a caller that split the key at its first colon would hand it over.
    assert asyncio.run(runtime_cleanup.cleanup_runtime("peer", rest)) == "gone"
    with pytest.raises(ValueError, match="remote session"):
        runtime_cleanup.unlink_stale_socket("peer", rest, key)
    # nothing was locked under the remote key (a lock file is named after its key)
    assert not [name for name in os.listdir(sessionlock.lock_dir()) if "peer" in name]
    assert _local_is_live(fake_jsonl)
    assert len(st.list_remote(peer.peer_id)) == 1


def test_mission_adopt_refuses_a_remote_key(auth_cfg, remote_row):
    key, st, _peer = remote_row
    admitted, why = mission_routes._adoption_admission(key)
    assert admitted is False and "not a session of an available agent" in why
    with pytest.raises(mission_routes.missions.MissionError) as exc:
        mission_routes._session_key(key)
    assert exc.value.status == 404
    client, hdr = _operator(auth_cfg)
    r = client.post("/api/missions/any/adopt", json={"session_key": key}, headers=hdr)
    assert r.status_code == 404 and r.json()["detail"] == "unknown session id"
    assert len(st.list_remote()) == 1


def test_notifications_never_announce_a_remote_row(remote_row):
    key, st, _peer = remote_row
    assert needs_you_notify.sync_once() == {"opened": 0, "closed": 0}
    listing = notifications.listing()
    assert listing["notifications"] == [] and listing["close_tags"] == []
    assert "peer:" not in repr(listing) and "a remote session" not in repr(listing)
    assert notifications.has_open_episode(key) in (False, None)
    assert len(st.list_remote()) == 1
