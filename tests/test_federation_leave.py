"""Leaving a share (#2201): the receiving instance gives up ONE grant, so a share ends from
either side.

Three layers, as in the other federation test modules:

* the OWNER's side over one real `service.Federation` and its own store (`owner`): the verb's
  validator, the fenced check-and-end, every refusal, the bell row;
* the RECEIVER's side over a real store and a scripted owner (`receiver`, and the routes through
  `TestClient`): what each answer proves and what it leaves in place;
* two real instances over a real peer channel, in one process (`paired`) and as two running apps
  (`two_instances`).

Every race is forced (a held fence, a hook between two steps), never slept for.
"""

# ruff: noqa: F811 — fixtures imported from the other federation test modules
from __future__ import annotations

import ast
import asyncio
import inspect
import sqlite3
import textwrap
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent_sessions
from agent_sessions import notifications, shares
from agent_sessions.federation import channel as ch
from agent_sessions.federation import (
    fence,
    grants,
    inbox,
    notices,
    remote,
    service,
    sharing,
    store,
    verbs,
)
from federation_helpers import until
from test_federation_fence import waiting  # noqa: F401 — a fixture
from test_federation_grants import api_key, live, trusted  # noqa: F401 — `live` is a fixture
from test_federation_inputs import (  # noqa: F401 — fixtures
    TITLE,
    Owner,
    bell,
    expire,
    held,
    msg,
    owner,
    ref,
    submits,
)
from test_federation_routes import Instance, _client, _login, _wait, two_instances  # noqa: F401
from test_federation_sharing import _pair_instances, _rows_for, paired
from test_federation_together import Receiver, entry, listed, receiver  # noqa: F401

SRC = Path(agent_sessions.__file__).parent
GID = "g" * 22


@pytest.fixture
def anyio_backend():
    return "asyncio"


def left_rows() -> list[dict]:
    """The owner's "<peer> left a share" rows on the bell."""
    return [r for r in bell() if str(r.get("action_id", "")).startswith(notices.LEFT_PREFIX)]


def events(st: store.Store, grant_id: str) -> list[str]:
    return [e["event"] for e in st.grant_events(grant_id)]


# ── the verb table ────────────────────────────────────────────────────


def test_the_verb_takes_a_grant_reference_and_nothing_else():
    spec = verbs.PEER["share.leave"]
    assert spec.handle is sharing.share_leave and spec.modes == frozenset({ch.MODE_PEER})
    assert spec.validate({"grant_id": GID, "revision": 3}) == {"grant_id": GID, "revision": 3}
    bad = [
        {},
        {"grant_id": GID},
        {"revision": 1},
        {"grant_id": GID, "revision": 0},
        {"grant_id": GID, "revision": -1},
        {"grant_id": GID, "revision": True},
        {"grant_id": GID, "revision": 1.0},
        {"grant_id": GID, "revision": "1"},
        {"grant_id": GID, "revision": 2**53 + 1},
        {"grant_id": "short", "revision": 1},
        {"grant_id": "../" + "g" * 22, "revision": 1},
        {"grant_id": ["g" * 22], "revision": 1},
        {"grant_id": None, "revision": 1},
        # nothing beyond the reference: no reason, no session, no peer, no flag
        {"grant_id": GID, "revision": 1, "reason": "done"},
        {"grant_id": GID, "revision": 1, "session_key": "codex-api:x"},
        {"grant_id": GID, "revision": 1, "peer_id": "a" * 32},
        {"grant_id": GID, "revision": 1, "force": True},
        {"grant_id": GID, "revision": 1, "grant_ids": [GID]},
    ]
    for args in bad:
        with pytest.raises(ch.BadArgs):
            spec.validate(dict(args))


def test_the_leave_verb_is_the_only_one_that_ends_a_grant_and_it_can_do_nothing_else():
    """In the source: of the modules a verb runs in, `sharing` alone names `grants.leave`, and
    none names the operator's `revoke`, `create` or `change`. The store's `leave_grant` is
    called from `grants.leave` only, and writes through the cascade every other end uses."""

    def attrs(path: Path) -> set[tuple[str, str]]:
        return {
            (n.value.id, n.attr)
            for n in ast.walk(ast.parse(path.read_text()))
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
        }

    fed = SRC / "federation"
    for path in fed.glob("*.py"):
        named = attrs(path)
        if path.name != "sharing.py":
            assert ("grants", "leave") not in named, path.name
        if path.name not in ("service.py",):  # (the service revokes a PEER: `store.revoke`)
            assert ("grants", "revoke") not in named, path.name
        if path.name != "grants.py":
            assert "leave_grant" not in {a for _, a in named}, path.name
    # the route never ends a grant itself: it asks the owner
    routes = (SRC / "routes" / "federation.py").read_text()
    assert "leave_grant" not in routes and "grants.leave" not in routes
    body = textwrap.dedent(inspect.getsource(store.Store.leave_grant))
    assert "_end_grants(" in body and "_with_fences(" in body and "_holding(" in body
    assert "UPDATE" not in body and "_write(" not in body  # no write of its own


# ── the owner: leave ends the grant ───────────────────────────────────


@pytest.mark.anyio
async def test_leave_ends_the_grant_and_the_owner_is_told_once(owner):
    made = owner.grant("view")
    other = owner.grant("view")
    generation = owner.st.generation()
    owner.fed.watching.touch(made.grant_id)
    assert await owner.call("share.leave", ref(made)) == {}  # no session data, no grant data
    await owner.quiet()
    ended = owner.st.get_grant(made.grant_id)
    assert (ended.state, ended.end_reason) == ("revoked", store.END_PEER_LEFT)
    assert events(owner.st, made.grant_id) == ["granted", "revoked"]
    assert owner.st.generation() == generation + 1
    assert not owner.fed.watching.watching(made.grant_id)  # presence is only under a live grant
    assert owner.st.get_grant(other.grant_id).state == "live"  # and nothing else ended
    # a later read is refused like any call on an ended grant
    assert await owner.refused("session.snapshot", ref(made)) == "no_grant"
    assert await owner.refused("access.status", ref(made)) == "no_grant"
    assert [g.grant_id for g in owner.st.list_grants()] == [other.grant_id]
    # ONE bell row: the peer's name here and the session's title, nothing else
    (row,) = left_rows()
    assert row["action_id"] == f"peer-left:{made.grant_id}"
    assert row["title"] == "GPU box left a share" and row["project"] == TITLE
    assert row["reason"] == notifications.EVENT_REASON["peer_share_left"]
    assert row["session_id"] == made.session_key  # the link is the owner's own session


@pytest.mark.anyio
async def test_a_repeated_leave_is_no_grant_and_raises_no_second_notice(owner):
    made = owner.grant("view")
    assert await owner.call("share.leave", ref(made)) == {}
    await owner.quiet()
    assert len(left_rows()) == 1
    generation = owner.st.generation()
    for _ in range(3):
        assert await owner.refused("share.leave", ref(made)) == "no_grant"
    await owner.quiet()
    assert len(left_rows()) == 1 and owner.st.generation() == generation
    # also once the operator read the row away: a repeat never raises it again
    notifications.dismiss_for_action(notices.left_key(made.grant_id))
    assert await owner.refused("share.leave", ref(made)) == "no_grant"
    await owner.quiet()
    assert left_rows() == [] and events(owner.st, made.grant_id) == ["granted", "revoked"]


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["terminal", "suggest", "send"])
async def test_a_terminal_grant_and_every_level_end_the_same_way(owner, kind):
    if kind == "terminal":
        made = owner.grant("view", key=f"claude:{uuid.uuid4()}")
        assert (made.kind, made.level) == ("terminal", "view")
    else:
        made = owner.grant(kind)
    assert await owner.call("share.leave", ref(made)) == {}
    await owner.quiet()
    ended = owner.st.get_grant(made.grant_id)
    assert (ended.state, ended.end_reason) == ("revoked", "peer_left")
    assert [r["action_id"] for r in left_rows()] == [f"peer-left:{made.grant_id}"]


@pytest.mark.anyio
async def test_what_hangs_on_the_grant_ends_with_it(owner):
    made = owner.grant("suggest")
    suggestion = await owner.call("input.suggest", msg(made))
    asked = await owner.call("access.request", {**ref(made), "level": "send"})
    await owner.quiet()
    before = sorted(r["action_id"].split(":")[0] for r in bell())
    assert before == ["peer-access", "peer-suggestion"]
    assert await owner.call("share.leave", ref(made)) == {}
    await owner.quiet()
    row = owner.suggestion(suggestion["suggestion_id"])
    assert (row.state, row.end_reason) == ("withdrawn", "grant_peer_left")
    request = owner.st.get_access_request(asked["request_id"])
    assert (request.state, request.end_reason) == ("expired", "grant_peer_left")
    # their bell rows went with the grant; the one row left says who left
    assert [r["action_id"] for r in bell()] == [f"peer-left:{made.grant_id}"]


# ── the owner: every refusal is `no_grant`, and nothing ends ──────────


def _change(o: Owner, made: store.Grant) -> None:
    grants.change(
        o.st,
        made.grant_id,
        expires_in=1800,
        revision=made.revision,
        accept_risk=made.level == "send",
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "case", ["another_peer", "revoked", "expired", "stale_revision", "peer_revoked", "unknown"]
)
async def test_every_refusal_is_no_grant_and_nothing_ends(owner, case):
    made = owner.grant("send")
    caller, args, state = None, ref(made), "live"
    if case == "another_peer":
        caller = trusted(owner.st, "Other box")
    elif case == "revoked":
        grants.revoke(owner.st, made.grant_id)
        state = "revoked"
    elif case == "expired":
        expire(owner.st, made.grant_id)
    elif case == "stale_revision":
        _change(owner, made)
    elif case == "peer_revoked":
        owner.st.revoke(owner.peer.peer_id)
        state = "revoked"
    else:
        args = {"grant_id": "z" * 22, "revision": 1}
    await owner.quiet()
    before = (
        owner.st.get_grant(made.grant_id),
        owner.st.generation(),
        events(owner.st, made.grant_id),
    )
    assert await owner.refused("share.leave", args, caller) == "no_grant"
    await owner.quiet()
    after = (
        owner.st.get_grant(made.grant_id),
        owner.st.generation(),
        events(owner.st, made.grant_id),
    )
    assert after == before and before[0].state == state
    assert before[0].end_reason != "peer_left" and left_rows() == []


@pytest.mark.anyio
async def test_a_stale_revision_is_refused_and_the_current_one_ends_the_grant(owner):
    """The owner changes the grant just before Leave arrives: refused, the share stays live, and
    a second Leave with the revision the owner now lists ends it."""
    made = owner.grant("view")
    _change(owner, made)
    assert await owner.refused("share.leave", ref(made)) == "no_grant"
    now = owner.st.get_grant(made.grant_id)
    assert now.state == "live" and now.revision == made.revision + 1 and left_rows() == []
    assert await owner.call("share.leave", ref(now)) == {}
    await owner.quiet()
    assert owner.st.get_grant(made.grant_id).end_reason == "peer_left" and len(left_rows()) == 1


# ── the owner: the check and the end are one fenced transaction ───────


def _bump_revision(st: store.Store, grant_id: str) -> None:
    """What a change that HOLDS the fence commits: the grant's revision moves."""
    con = sqlite3.connect(st.db_path)
    con.execute("UPDATE grants SET revision = revision + 1 WHERE grant_id = ?", (grant_id,))
    con.commit()
    con.close()


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["revision", "expiry", "revoke"])
async def test_a_grant_that_changes_while_leave_waits_for_the_fence_is_refused(
    owner, waiting, monkeypatch, change
):
    """Leave passed its arrival check and is waiting for the fence. Whoever holds the fence
    changes the grant and lets go: the re-check INSIDE the fence refuses, and nothing ends."""
    made = owner.grant("send")
    monkeypatch.setattr(grants, "LEAVE_WAIT_S", 20.0)
    checked: list[str] = []
    real = store.Store._holding

    def recorded(con, grant_id, peer_id, now, revision=None):
        try:
            return real(con, grant_id, peer_id, now, revision)
        except store.NotGranted as exc:
            checked.append(str(exc))
            raise

    monkeypatch.setattr(store.Store, "_holding", staticmethod(recorded))
    fd = fence.acquire(owner.st.directory, made.grant_id, 0)
    waiting.clear()
    leaving = asyncio.create_task(owner.refused("share.leave", ref(made)))
    assert await asyncio.to_thread(waiting.wait, 20), "leave never met the fence"
    assert not leaving.done() and owner.st.get_grant(made.grant_id).state == "live"
    generation = owner.st.generation()
    if change == "revision":
        _bump_revision(owner.st, made.grant_id)
    elif change == "expiry":
        expire(owner.st, made.grant_id)
    else:
        con = sqlite3.connect(owner.st.db_path)
        con.execute(
            "UPDATE grants SET state = 'revoked', end_reason = 'revoked', ended_at = ?"
            " WHERE grant_id = ?",
            (time.time(), made.grant_id),
        )
        con.commit()
        con.close()
    fence.release(fd)
    assert await asyncio.wait_for(leaving, 20) == "no_grant"
    await owner.quiet()
    # refused by the read inside the fence, not by the arrival check and not by luck
    assert checked == [{"revision": "stale revision"}.get(change, "grant ended")]
    grant = owner.st.get_grant(made.grant_id)
    assert grant.end_reason != "peer_left" and owner.st.generation() == generation
    assert events(owner.st, made.grant_id) == ["granted"] and left_rows() == []
    assert not fence.held_elsewhere(owner.st.directory, made.grant_id)


@pytest.mark.anyio
async def test_a_change_between_the_arrival_check_and_the_fence_is_caught_inside_the_fence(
    owner, monkeypatch
):
    made = owner.grant("view")
    real = owner.st.leave_grant
    seen: list[int] = []

    def then_changed(grant_id, **kwargs):
        # After the plain check, before the fenced transaction: another process changes the grant.
        grants.change(store.Store(owner.st.directory), grant_id, expires_in=600, revision=1)
        seen.append(owner.st.get_grant(grant_id).revision)
        return real(grant_id, **kwargs)

    monkeypatch.setattr(owner.st, "leave_grant", then_changed)
    assert await owner.refused("share.leave", ref(made)) == "no_grant"
    await owner.quiet()
    assert seen == [2] and owner.st.get_grant(made.grant_id).state == "live" and left_rows() == []


@pytest.mark.anyio
async def test_a_busy_fence_commits_nothing_and_raises_no_notice(owner, waiting, monkeypatch):
    made = owner.grant("send")
    monkeypatch.setattr(grants, "LEAVE_WAIT_S", 0.05)
    before = (owner.st.get_grant(made.grant_id), owner.st.generation())
    fd = fence.acquire(owner.st.directory, made.grant_id, 0)
    try:
        assert await owner.refused("share.leave", ref(made)) == "busy"
        assert waiting.is_set()  # it did wait on the fence, and gave up
        await owner.quiet()
        assert (owner.st.get_grant(made.grant_id), owner.st.generation()) == before
        assert events(owner.st, made.grant_id) == ["granted"] and left_rows() == []
        # Only the holder of a live grant ever waits on its fence: to anybody else the grant
        # does not exist, held fence or not (no `busy` that would say "this id is real").
        stranger = trusted(owner.st, "Other box")
        waiting.clear()
        assert await owner.refused("share.leave", ref(made), stranger) == "no_grant"
        stale = {"grant_id": made.grant_id, "revision": made.revision + 1}
        assert await owner.refused("share.leave", stale) == "no_grant"
        assert not waiting.is_set()
    finally:
        fence.release(fd)
    # the fence is free again: the same call ends the grant, and now the owner is told
    assert await owner.call("share.leave", ref(made)) == {}
    await owner.quiet()
    assert owner.st.get_grant(made.grant_id).end_reason == "peer_left" and len(left_rows()) == 1


@pytest.mark.anyio
async def test_a_caller_that_went_away_does_not_cut_the_leave_in_half(owner, monkeypatch):
    """The channel cancels a handler when it closes. The write is the service's own task: it
    commits or it does not, and a committed end is announced with nobody waiting for the answer."""
    made = owner.grant("view")
    entered, go = asyncio.Event(), asyncio.Event()
    real = sharing._leave

    async def slow(fed, peer_id, grant_id, revision):
        entered.set()
        await go.wait()
        return await real(fed, peer_id, grant_id, revision)

    monkeypatch.setattr(sharing, "_leave", slow)
    call = asyncio.create_task(owner.call("share.leave", ref(made)))
    await entered.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    go.set()
    await owner.quiet()
    assert owner.st.get_grant(made.grant_id).end_reason == "peer_left" and len(left_rows()) == 1


def test_the_store_ends_a_left_grant_under_its_fence_or_not_at_all(tmp_path, live):
    """`Store.leave_grant` alone, with another store object (another process) holding the fence."""
    st = store.Store(tmp_path / "fed")
    peer, stranger = trusted(st, "box"), trusted(st, "other")
    made = grants.create(
        st, session_key=api_key(), peer_id=peer.peer_id, level="view", expires_in=None
    )
    ok = {"peer_id": peer.peer_id, "revision": made.revision}
    for kwargs in (
        {"peer_id": stranger.peer_id, "revision": made.revision},
        {"peer_id": peer.peer_id, "revision": made.revision + 1},
        {**ok, "clock": lambda: made.expires_at + 1},  # expired on this clock, inside the fence
    ):
        with pytest.raises(store.NotGranted):
            st.leave_grant(made.grant_id, **kwargs)
    with pytest.raises(store.NotGranted):
        st.leave_grant("z" * 22, **ok)
    fd = fence.acquire(st.directory, made.grant_id, 0)
    try:
        with pytest.raises(store.Busy):
            store.Store(st.directory).leave_grant(made.grant_id, **ok, wait=0.05)
    finally:
        fence.release(fd)
    assert st.get_grant(made.grant_id).state == "live" and events(st, made.grant_id) == ["granted"]
    was = st.leave_grant(made.grant_id, **ok)
    assert was.state == "live" and was.grant_id == made.grant_id  # the grant as it was
    assert st.get_grant(made.grant_id).end_reason == "peer_left"
    with pytest.raises(store.NotGranted):  # a repeat finds it ended
        st.leave_grant(made.grant_id, **ok)
    assert events(st, made.grant_id) == ["granted", "revoked"]


# ── the receiver: what each answer proves ─────────────────────────────


def _bell_for(r: Receiver, grant_id: str = GID) -> list[str]:
    return [k for k in r.keys() if k.endswith(f":{grant_id}")]


async def _announced(r: Receiver, *grant_ids: str) -> None:
    """Put this instance's own rows about those grants on the bell."""
    for grant_id in grant_ids:
        for kind in ("shared", "note"):
            notices.announce_remote(r.st, kind, r.peer.peer_id, grant_id, "Their session")


@pytest.mark.anyio
async def test_only_ok_drops_the_mirror_row_and_this_instances_bell_rows(receiver):
    r, pid = receiver, receiver.peer.peer_id
    mine, kept = entry(GID, "send", "First"), entry("h" * 22, "view", "Second")
    r.mirror(mine, kept)
    await _announced(r, GID, "h" * 22)
    assert len(_bell_for(r)) == 2
    r.answers["share.leave"] = {}
    r.answers["share.list"] = listed(kept)
    assert await remote.leave(r.fed, pid, GID) == {"left": True}
    await r.quiet()
    assert r.calls[0] == ("share.leave", {"grant_id": GID, "revision": 1})
    assert [v for v, _ in r.calls].count("share.leave") == 1
    assert [row.grant_id for row in r.st.list_remote(pid)] == ["h" * 22]
    assert _bell_for(r) == [] and len(_bell_for(r, "h" * 22)) == 2  # this grant's rows only
    # nobody is told "stopped sharing a session" about a share this instance left itself
    assert not [k for k in r.keys() if k.startswith("peer-remote-ended:")]


# ── a refresh and a confirmed leave are ordered at the WRITE ──────────
#
# A refresh reads the owner's list and replaces the peer's rows in a worker thread. A Leave that
# is confirmed in between must win whatever happens next: the list was read before the owner
# ended the grant and still names it. The order is one boundary inside the store
# (`Store.drop_remote` moves the peer's mirror epoch, `Store.swap_remote` refuses a list read
# under an older one), so it holds without any repair refresh and across a restart.

HID, NEW = "h" * 22, "n" * 22


class _HeldWrite:
    """Holds the FIRST mirror replacement of a refresh at the store's door, in its worker
    thread, until the test lets it go: the reviewer's interleaving, forced."""

    def __init__(self, st: store.Store, monkeypatch) -> None:
        self.arrived, self.go = threading.Event(), threading.Event()
        self.held = False
        # (`replace_remote` too: the name the refresh wrote through before the order was the
        # store's, so this test is red on that code for the right reason.)
        for name in ("swap_remote", "replace_remote"):
            real = getattr(st, name, None)
            if real is not None:
                monkeypatch.setattr(st, name, self._gate(real))

    def _gate(self, real):
        def write(*args, **kwargs):
            if not self.held:
                self.held = True
                self.arrived.set()
                assert self.go.wait(20), "the held write was never released"
            return real(*args, **kwargs)

        return write


def _lists(r: Receiver) -> int:
    return [v for v, _ in r.calls].count("share.list")


async def _leave_inside_a_refresh(r: Receiver, monkeypatch, stale: dict) -> asyncio.Task:
    """A refresh has read ``stale`` (the owner's list from BEFORE the leave) and is held at
    its write. The Leave is confirmed and completes. Returns the refresh, still held."""
    pid = r.peer.peer_id
    r.answers["share.list"] = stale
    gate = _HeldWrite(r.st, monkeypatch)
    refreshing = asyncio.create_task(remote.refresh(r.fed, pid))
    assert await asyncio.to_thread(gate.arrived.wait, 20), "the refresh never reached its write"
    r.answers["share.leave"] = {}
    assert await remote.leave(r.fed, pid, GID) == {"left": True}
    assert r.st.get_remote(pid, GID) is None
    r.release = gate.go.set
    return refreshing


@pytest.mark.anyio
async def test_a_list_read_before_a_confirmed_leave_is_never_written_after_it(
    auth_cfg, monkeypatch
):
    """The owner disconnects right after answering Leave, so no later list read succeeds. The
    replacement that was computed before the leave lands afterwards: it writes NOTHING. The
    share is absent in the store, for a fresh reader of the list route, and after a restart."""
    c = _client(auth_cfg)
    _login(c, auth_cfg)
    r = Receiver(c.app.state.federation)
    pid = r.peer.peer_id
    mine, other = entry(GID, "view", "First"), entry(HID, "view", "Second")
    r.mirror(mine, other)
    # The list the refresh read before the leave: the left grant (changed), the other grant at
    # a newer revision, and a grant this instance had not heard of yet.
    stale = listed(
        {**mine, "level": "suggest", "revision": 2},
        {**other, "revision": 2},
        entry(NEW, "view", "Third"),
    )
    refreshing = await _leave_inside_a_refresh(r, monkeypatch, stale)
    r.answers["share.list"] = ch.CallError("closed")  # the owner is gone: every later list fails
    r.release()
    assert await refreshing is False  # nothing was replaced
    await r.quiet()

    assert r.st.get_remote(pid, GID) is None
    # Nothing of that list was written: the other rows are exactly as they were. A list from
    # before the leave is refused whole, like any refresh that failed; the next one that the
    # owner answers brings them up to date.
    rows = {row.grant_id: row.revision for row in r.st.list_remote(pid)}
    assert rows == {HID: 1}
    # a fresh consumer of the list (a new browser)
    listed_now = c.get("/api/federation/remote").json()["remote"]
    assert [x["grant_id"] for x in listed_now] == [HID]
    # the bell: nothing about the left grant, raised or left behind
    assert [k for k in r.keys() if k.endswith(f":{GID}")] == []
    assert not [k for k in r.keys() if k.startswith("peer-remote-ended:")]

    # a restart: a new store and a new service over the same directory, the owner still away
    again = service.Federation(static=r.fed._static, name="beta")
    assert again.store is not r.st and again.store.directory == r.st.directory
    assert again.store.get_remote(pid, GID) is None
    assert [x["grant_id"] for x in remote.listing(again)] == [HID]
    assert await remote.refresh(again, pid) is False  # nobody to ask: nothing re-inserts it
    assert again.store.get_remote(pid, GID) is None
    assert again.store.remote_epoch(pid) == r.st.remote_epoch(pid) == 1  # the order is durable


@pytest.mark.anyio
async def test_the_refused_stale_list_is_read_again_once_and_the_fresh_one_is_applied(
    receiver, monkeypatch
):
    r, pid = receiver, receiver.peer.peer_id
    mine, other = entry(GID, "view", "First"), entry(HID, "view", "Second")
    r.mirror(mine, other)
    refreshing = await _leave_inside_a_refresh(r, monkeypatch, listed(mine, other))
    # The owner is still there. Its list no longer has the left grant, and the rest moved on.
    r.answers["share.list"] = listed({**other, "revision": 3}, entry(NEW, "view", "Third"))
    before = _lists(r)
    r.release()
    assert await refreshing is True
    await r.quiet()
    assert _lists(r) == before + 1  # exactly one more read of the list, by that same refresh
    assert [v for v, _ in r.calls].count("share.leave") == 1
    rows = {row.grant_id: row.revision for row in r.st.list_remote(pid)}
    assert rows == {HID: 3, NEW: 1}  # unrelated grants: applied from the list read AFTER the leave
    assert not [k for k in r.keys() if k.endswith(f":{GID}")]
    assert not [k for k in r.keys() if k.startswith("peer-remote-ended:")]
    assert f"peer-remote-share:{pid}:{NEW}" in r.keys()  # what is really new is still announced


@pytest.mark.anyio
async def test_a_refresh_after_the_leave_keeps_it_out_and_a_new_grant_of_that_session_shows(
    receiver,
):
    r, pid = receiver, receiver.peer.peer_id
    mine, other = entry(GID, "view", "First"), entry(HID, "view", "Second")
    r.mirror(mine, other)
    r.answers["share.leave"] = {}
    await remote.leave(r.fed, pid, GID)
    await r.quiet()
    assert _lists(r) == 0  # a confirmed leave needs no repair refresh, and starts none
    # a refresh that starts strictly after the leave: the owner no longer lists the grant
    r.answers["share.list"] = listed(other)
    assert await remote.refresh(r.fed, pid) is True
    await r.quiet()
    assert [row.grant_id for row in r.st.list_remote(pid)] == [HID] and r.keys() == []
    # The owner shares the SAME session again: a new grant id. Nothing kept about the leave
    # hides it, in the store or on the bell.
    again = {**mine, "grant_id": NEW}
    r.answers["share.list"] = listed(other, again)
    assert await remote.refresh(r.fed, pid) is True
    await r.quiet()
    back = r.st.get_remote(pid, NEW)
    assert back is not None and (back.engine, back.native_id) == (mine["engine"], mine["native_id"])
    assert [x["grant_id"] for x in remote.listing(r.fed)] == [HID, NEW]
    assert r.keys() == [f"peer-remote-share:{pid}:{NEW}"]
    assert r.fed.has_left(pid, GID) and not r.fed.has_left(pid, NEW)


def test_the_stores_mirror_write_is_ordered_against_a_leave(tmp_path):
    """`Store` alone: the epoch moves with every confirmed leave, in its transaction, and a
    replacement that names an older epoch writes nothing."""
    st = store.Store(tmp_path / "fed")
    peer, other_peer = trusted(st, "box"), trusted(st, "other")
    pid = peer.peer_id
    rows = [entry(GID), entry(HID)]
    assert st.remote_epoch(pid) == 0
    assert st.swap_remote(pid, rows, epoch=0) == []  # the rows as they were: none
    epoch = st.remote_epoch(pid)  # a refresh reads it, then asks the owner ...
    assert st.drop_remote(pid, GID) is True  # ... and a leave is confirmed meanwhile
    assert st.remote_epoch(pid) == epoch + 1 and st.remote_epoch(other_peer.peer_id) == 0
    with pytest.raises(store.StaleMirror):
        st.swap_remote(pid, rows, epoch=epoch)
    assert [r.grant_id for r in st.list_remote(pid)] == [HID]  # nothing of it was written
    # a leave of a row that is not there (already refreshed away) still orders what follows
    assert st.drop_remote(pid, GID) is False and st.remote_epoch(pid) == epoch + 2
    # the current epoch writes, and answers with exactly the rows it replaced
    was = st.swap_remote(pid, [entry(NEW)], epoch=epoch + 2)
    assert [r.grant_id for r in was] == [HID]
    assert [r.grant_id for r in st.list_remote(pid)] == [NEW]
    # another store object over the same file (another process, a restart) sees the same order
    assert store.Store(st.directory).remote_epoch(pid) == epoch + 2
    # a peer that is no longer trusted: nothing, as before
    st.revoke(pid)
    assert st.swap_remote(pid, rows, epoch=epoch + 2) is None and st.list_remote(pid) == []
    # reading the epoch never creates a store
    empty = store.Store(tmp_path / "none")
    assert empty.remote_epoch(pid) == 0 and not empty.db_path.exists()


def test_a_notice_computed_around_a_leave_says_nothing_about_the_left_grant(receiver):
    """`inbox.refreshed` runs beside the request that left the share, with a before/after pair
    from a write that landed just before the leave's drop. Whatever the pair says, no row about
    the left grant is raised or left behind; other grants are announced as always."""
    r, pid = receiver, receiver.peer.peer_id
    mine, other = entry(GID, "view", "First"), entry(HID, "view", "Second")
    r.mirror(mine, other)
    was = r.st.list_remote(pid)
    # the leave: marked first, the mirror row dropped, then its rows retracted
    r.fed.left_remote(pid, GID)
    remote._forget(r.fed, pid, GID)
    # (a) the pair says the grant is gone from the owner's list: NOT "stopped sharing"
    inbox.refreshed(r.fed, pid, was, [remote.clean_entry(listed(other)["grants"][0])])
    assert r.keys() == []
    # (b) the pair says the grant is new: the row is taken back, the mirror does not have it
    inbox.refreshed(r.fed, pid, [], [mine])
    assert r.keys() == []
    # (c) the pair says its level changed
    inbox.refreshed(r.fed, pid, was, [{**mine, "level": "send"}, other])
    assert r.keys() == []
    # a grant that the OWNER ended is still announced, and a new one too
    r.st.replace_remote(pid, [entry(NEW, "view", "Third")])
    inbox.refreshed(
        r.fed, pid, [w for w in was if w.grant_id == HID], [entry(NEW, "view", "Third")]
    )
    assert r.keys() == [f"peer-remote-ended:{pid}:{HID}", f"peer-remote-share:{pid}:{NEW}"]


@pytest.mark.anyio
async def test_a_hint_check_that_ends_after_the_leave_raises_no_row(receiver):
    """`inbox.check` asked the owner about a note while the share was being left: its answer
    arrives afterwards. The row it posts is taken back."""
    r, pid = receiver, receiver.peer.peer_id
    r.mirror(entry(GID, "view", "First"))
    asked = asyncio.Event()
    answer = asyncio.Event()

    async def call(verb, args=None, **_kw):
        r.calls.append((verb, dict(args or {})))
        if verb == "note.list":
            asked.set()
            await answer.wait()
            return {
                "reset": False,
                "seq": 4,
                "changes": [],
                "more": False,
                "next": None,
                "unread": 2,
            }
        return {}

    r.call = call  # type: ignore[method-assign]
    checking = asyncio.create_task(inbox.check(r.fed, pid, GID, inbox.NOTE))
    await asked.wait()
    await remote.leave(r.fed, pid, GID)
    answer.set()
    await checking
    await r.quiet()
    assert r.keys() == []


@pytest.mark.anyio
async def test_a_refusal_proves_nothing_the_rows_stay_and_the_list_is_read_not_the_write(
    receiver, monkeypatch
):
    r, pid = receiver, receiver.peer.peer_id
    mine = entry(GID, "view", "First")
    r.mirror(mine)
    await _announced(r, GID)
    refreshes: list[str] = []
    real_refresh = r.fed.refresh_remote
    monkeypatch.setattr(r.fed, "refresh_remote", refreshes.append)
    r.answers["share.leave"] = None  # the owner refuses: `no_grant`
    with pytest.raises(remote.ReadError) as exc:
        await remote.leave(r.fed, pid, GID)
    await r.quiet()
    assert exc.value.code == "no_grant"
    assert r.calls == [("share.leave", {"grant_id": GID, "revision": 1})]  # once, never replayed
    assert refreshes == [pid]  # the LIST is asked for, in the background
    assert (
        len(r.st.list_remote(pid)) == 1 and len(_bell_for(r)) == 2 and not r.fed.has_left(pid, GID)
    )

    # that refresh FAILS (the owner went away): the row and the bell rows stay
    monkeypatch.setattr(r.fed, "refresh_remote", real_refresh)
    r.calls.clear()
    r.answers["share.list"] = ch.CallError("closed")
    assert await remote.refresh(r.fed, pid) is False
    await r.quiet()
    assert len(r.st.list_remote(pid)) == 1 and len(_bell_for(r)) == 2
    assert "share.leave" not in [v for v, _ in r.calls]

    # it succeeds and still lists the grant (its revision had moved): the row stays, renewed
    r.answers["share.list"] = listed({**mine, "revision": 2})
    assert await remote.refresh(r.fed, pid) is True
    await r.quiet()
    (row,) = r.st.list_remote(pid)
    assert row.revision == 2 and len(_bell_for(r)) == 2
    # and Leave again carries the revision the owner now lists
    r.calls.clear()
    r.answers["share.leave"] = {}
    r.answers["share.list"] = listed()
    await remote.leave(r.fed, pid, GID)
    assert r.calls[0] == ("share.leave", {"grant_id": GID, "revision": 2})
    await r.quiet()
    assert r.st.list_remote(pid) == []


@pytest.mark.anyio
async def test_a_refused_leave_whose_grant_a_complete_refresh_no_longer_lists_loses_the_row(
    receiver,
):
    """Leave raced the owner's own Revoke: refused. The row goes when, and only when, a complete
    refresh of the owner's list no longer has it (the existing refresh rule)."""
    r, pid = receiver, receiver.peer.peer_id
    r.mirror(entry(GID, "view", "First"))
    r.answers["share.leave"] = None
    r.answers["share.list"] = listed()
    with pytest.raises(remote.ReadError):
        await remote.leave(r.fed, pid, GID)
    await until(lambda: r.st.list_remote(pid) == [])
    await r.quiet()
    assert [v for v, _ in r.calls].count("share.leave") == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("failure", "code", "status", "refreshed"),
    [
        (ch.CallError(ch.ANSWER_LOST), "peer_lost", 504, True),  # withheld after the handler ran
        (ch.CallError("closed"), "peer_lost", 504, True),  # closed after dispatch
        (ch.CallError("timeout"), "timeout", 504, True),
        (ch.CallError("busy"), "busy", 429, True),  # the owner's fence: nothing committed
        (ch.CallError("unavailable"), "unavailable", 503, True),
        (ch.CallError("closed", dispatched=False), "peer_offline", 503, False),  # never left
        (ch.CallError("refused"), "not_possible", 422, False),  # an owner before this verb
    ],
)
async def test_every_other_answer_keeps_the_rows_and_is_never_sent_twice(
    receiver, monkeypatch, failure, code, status, refreshed
):
    from agent_sessions.routes import federation as fed_routes

    r, pid = receiver, receiver.peer.peer_id
    r.mirror(entry(GID, "view", "First"))
    await _announced(r, GID)
    refreshes: list[str] = []
    monkeypatch.setattr(r.fed, "refresh_remote", refreshes.append)
    r.answers["share.leave"] = failure
    with pytest.raises(remote.ReadError) as exc:
        await remote.leave(r.fed, pid, GID)
    await r.quiet()
    assert exc.value.code == code
    answer = fed_routes._read_error(exc.value)
    assert (answer.status, answer.code) == (status, code)
    assert r.calls == [("share.leave", {"grant_id": GID, "revision": 1})]  # once
    assert len(r.st.list_remote(pid)) == 1 and len(_bell_for(r)) == 2  # nothing is hidden
    assert not r.fed.has_left(pid, GID)
    assert refreshes == ([pid] if refreshed else [])  # a list read, never the write again


@pytest.mark.anyio
async def test_an_unreachable_owner_is_never_called_and_nothing_changes(receiver):
    r, pid = receiver, receiver.peer.peer_id
    r.mirror(entry(GID, "view", "First"))
    await _announced(r, GID)
    r.fed.channel_for = lambda peer_id: None
    with pytest.raises(remote.ReadError) as exc:
        await remote.leave(r.fed, pid, GID)
    await r.quiet()
    assert exc.value.code == "peer_offline" and r.calls == []
    assert len(r.st.list_remote(pid)) == 1 and len(_bell_for(r)) == 2


@pytest.mark.anyio
async def test_a_share_of_either_kind_is_left_and_a_named_revision_is_the_one_sent(receiver):
    r, pid = receiver, receiver.peer.peer_id
    r.mirror({**entry(GID, "view", "A screen"), "kind": "terminal", "revision": 7})
    r.answers["share.leave"] = {"anything": "the answer's content is never read"}
    r.answers["share.list"] = listed()
    # the revision the operator was shown, not whatever the mirror holds by now
    assert await remote.leave(r.fed, pid, GID, 6) == {"left": True}
    assert r.calls[0] == ("share.leave", {"grant_id": GID, "revision": 6})
    await r.quiet()
    assert r.st.list_remote(pid) == []
    # a grant this instance does not hold: refused here, nobody is called
    r.calls.clear()
    with pytest.raises(remote.ReadError) as exc:
        await remote.leave(r.fed, pid, "h" * 22)
    assert exc.value.code == "no_grant" and r.calls == []


# ── the route ─────────────────────────────────────────────────────────

LEAVE = f"/api/federation/remote/{'a' * 32}/{GID}/leave"


def test_the_leave_route_needs_a_session_csrf_and_the_origin(auth_cfg, monkeypatch):
    c = _client(auth_cfg)
    called: list = []

    async def never(*args, **kwargs):
        called.append(args)
        raise AssertionError("reached without authorization")

    monkeypatch.setattr(remote, "leave", never)
    monkeypatch.setattr(remote, "read", never)
    origin = {"Origin": auth_cfg.origin}
    assert c.post(LEAVE, headers=origin).status_code == 401
    assert c.post(LEAVE, headers={**origin, "X-CSRF-Token": "x"}).status_code == 401
    hdr = _login(c, auth_cfg)
    assert c.post(LEAVE, headers=origin).status_code == 403  # no CSRF token
    assert c.post(LEAVE, headers={**hdr, "X-CSRF-Token": "0" * 43}).status_code == 403
    assert c.post(LEAVE, headers={**hdr, "Origin": "https://evil.example"}).status_code == 403
    assert c.post(LEAVE, headers={"X-CSRF-Token": hdr["X-CSRF-Token"]}).status_code == 403
    # shapes, before anything is called
    for url in (
        f"/api/federation/remote/NOT-A-PEER/{GID}/leave",
        f"/api/federation/remote/{'a' * 32}/short/leave",
    ):
        r = c.post(url, headers=hdr)
        assert r.status_code == 404 and r.json()["code"] == "no_grant", url
    for body in (
        {"revision": 0},
        {"revision": "1"},
        {"revision": True},
        {"revision": 1.5},
        {"revision": 1, "force": True},
        {"reason": "done"},
        {"grant_id": "h" * 22},
    ):
        r = c.post(LEAVE, json=body, headers=hdr)
        assert r.status_code == 422 and r.json()["code"] == "invalid", body
    assert called == []
    assert c.get(LEAVE).status_code in (404, 405)  # a write, and nothing on another method
    assert c.delete(LEAVE, headers=hdr).status_code == 405


class _Channel:
    """The owner as the leave route meets it: one scripted answer per call."""

    closed = False

    def __init__(self) -> None:
        self.asked: list[tuple[str, dict]] = []
        self.answer: object = {}

    async def call(self, verb, args=None, *, timeout=ch.CALL_TIMEOUT_S):
        self.asked.append((verb, dict(args or {})))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _receiving(c, monkeypatch):
    """A receiving app with one mirrored grant (revision 4), a scripted channel, its own bell
    rows about that grant, and the background refresh recorded instead of run."""
    fed = c.app.state.federation
    peer = trusted(fed.store, "GPU box")
    row = {**entry(GID, "view", "shared with us"), "revision": 4}
    assert fed.store.replace_remote(peer.peer_id, [row])
    for kind in ("shared", "note"):
        notices.announce_remote(fed.store, kind, peer.peer_id, GID, "shared with us")
    channel = _Channel()
    monkeypatch.setattr(fed, "channel_for", lambda peer_id: channel)
    background: list[str] = []
    monkeypatch.setattr(fed, "refresh_remote", background.append)
    return fed, peer.peer_id, channel, background


def _listed(c) -> list[str]:
    return [r["grant_id"] for r in c.get("/api/federation/remote").json()["remote"]]


def _remote_bell(peer_id: str) -> list[str]:
    return sorted(r["action_id"] for r in bell() if f":{peer_id}:{GID}" in r["action_id"])


def test_the_route_sends_leave_once_and_only_ok_removes_the_rows(auth_cfg, monkeypatch):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    fed, peer_id, channel, background = _receiving(c, monkeypatch)
    url = f"/api/federation/remote/{peer_id}/{GID}/leave"
    rows = _remote_bell(peer_id)
    assert len(rows) == 2 and _listed(c) == [GID]

    def kept(expected_calls: list) -> None:
        assert channel.asked == expected_calls
        assert _listed(c) == [GID] and _remote_bell(peer_id) == rows  # nothing is hidden
        channel.asked.clear()

    sent = [("share.leave", {"grant_id": GID, "revision": 4})]
    # a lost or withheld answer: 504, the rows stay, and the list is read (never the write)
    for failure in (ch.CallError(ch.ANSWER_LOST), ch.CallError("closed")):
        channel.answer = failure
        r = c.post(url, headers=hdr)
        assert (r.status_code, r.json()["code"]) == (504, "peer_lost"), r.text
        kept(sent)
    channel.answer = ch.CallError("timeout")
    r = c.post(url, headers=hdr)
    assert (r.status_code, r.json()["code"]) == (504, "timeout")
    kept(sent)
    # the owner's fence was busy: nothing was committed there, nothing is removed here
    channel.answer = ch.CallError("busy")
    r = c.post(url, headers=hdr)
    assert (r.status_code, r.json()["code"]) == (429, "busy")
    kept(sent)
    assert background == [peer_id] * 4
    # it never left this instance
    background.clear()
    channel.answer = ch.CallError("closed", dispatched=False)
    r = c.post(url, headers=hdr)
    assert (r.status_code, r.json()["code"]) == (503, "peer_offline")
    kept(sent)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fed, "channel_for", lambda peer_id: None)
        r = c.post(url, headers=hdr)
        assert (r.status_code, r.json()["code"]) == (503, "peer_offline")
    kept([])
    assert background == []
    # refused: 404 at once, one call under the revision asked for, the list read in the background
    channel.answer = ch.CallError("no_grant")
    r = c.post(url, json={"revision": 3}, headers=hdr)
    assert (r.status_code, r.json()["code"]) == (404, "no_grant")
    kept([("share.leave", {"grant_id": GID, "revision": 3})])
    assert background == [peer_id]
    # ok: the row and this instance's bell rows about that grant go
    channel.answer = {}
    r = c.post(url, json={"revision": 4}, headers=hdr)
    assert r.status_code == 200 and r.json() == {"left": True}, r.text
    assert r.headers["cache-control"] == "no-store"
    assert channel.asked == sent
    assert _listed(c) == [] and _remote_bell(peer_id) == []
    # again: this instance no longer holds that share, and nobody is asked
    channel.asked.clear()
    r = c.post(url, headers=hdr)
    assert (r.status_code, r.json()["code"]) == (404, "no_grant") and channel.asked == []


# ── two real instances ────────────────────────────────────────────────


async def _shared(p, monkeypatch, *, level: str = "view", key: str | None = None) -> store.Grant:
    """Alpha grants beta one session, and beta's mirror lists it."""
    key = key or api_key()
    _rows_for(monkeypatch, {key: "the shared session"})
    made = await asyncio.to_thread(
        grants.create,
        p.a.store,
        session_key=key,
        peer_id=p.b.peer_id,
        level=level,
        expires_in=None,
        accept_risk=level == "send",
    )
    p.a.notify_share_changed(p.b.peer_id)
    await until(lambda: p.b.store.get_remote(p.a.peer_id, made.grant_id) is not None)
    return made


def _keys() -> list[str]:
    return sorted(r["action_id"] for r in bell())


@pytest.mark.anyio
@pytest.mark.parametrize("what", ["api", "terminal", "send"])
async def test_two_instances_the_receiver_leaves_and_the_share_ends_on_both(
    tmp_path, monkeypatch, live, held, what
):
    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    async with paired(tmp_path, monkeypatch) as p:
        key = f"claude:{uuid.uuid4()}" if what == "terminal" else api_key()
        made = await _shared(p, monkeypatch, level="send" if what == "send" else "view", key=key)
        assert made.kind == ("terminal" if what == "terminal" else "api")
        share_row = f"peer-remote-share:{p.a.peer_id}:{made.grant_id}"
        await until(lambda: share_row in _keys())  # beta was told a session is shared with it

        assert await remote.leave(p.b, p.a.peer_id, made.grant_id) == {"left": True}

        # the owner: the grant ended, a later read is refused, and its operator is told once
        ended = p.a.store.get_grant(made.grant_id)
        assert (ended.state, ended.end_reason) == ("revoked", "peer_left")
        assert await p.refused("session.snapshot", {"grant_id": made.grant_id, "revision": 1}) == (
            "no_grant"
        )
        assert await p.refused("terminal.frame", {"grant_id": made.grant_id, "revision": 1}) == (
            "no_grant"
        )
        left = f"peer-left:{made.grant_id}"
        await until(lambda: left in _keys())
        # the receiver: its mirror row and its own bell rows about that grant are gone
        assert p.b.store.get_remote(p.a.peer_id, made.grant_id) is None
        assert remote.listing(p.b) == []
        # ... and stay gone through the refreshes the owner's hint and the rounds cause, with
        # no "stopped sharing a session" row about a share this instance left itself
        assert await remote.refresh(p.b, p.a.peer_id) is True
        await until(lambda: not p.b._refreshing)
        assert p.b.store.list_remote(p.a.peer_id) == []
        assert _keys() == [left]
        (row,) = [r for r in bell() if r["action_id"] == left]
        assert row["title"] == "beta left a share" and row["project"] == TITLE

        # a repeat: the receiver holds no such share any more and asks nobody
        with pytest.raises(remote.ReadError) as exc:
            await remote.leave(p.b, p.a.peer_id, made.grant_id)
        assert exc.value.code == "no_grant"
        # and the verb itself, repeated on the wire, is `no_grant` with no second notice
        assert await p.refused("share.leave", {"grant_id": made.grant_id, "revision": 1}) == (
            "no_grant"
        )
        await asyncio.sleep(0.1)
        assert _keys() == [left]


@pytest.mark.anyio
async def test_two_instances_a_revision_moved_just_before_leave_refuses_then_the_next_ends_it(
    tmp_path, monkeypatch, live
):
    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    async with paired(tmp_path, monkeypatch) as p:
        made = await _shared(p, monkeypatch)
        # The owner's operator changes the grant; beta has not heard yet (no hint, no round).
        p.pause_owner_rounds(monkeypatch)
        await p.quiet_receiver(monkeypatch)
        await asyncio.to_thread(
            grants.change, p.a.store, made.grant_id, expires_in=3600, revision=made.revision
        )
        assert p.b.store.get_remote(p.a.peer_id, made.grant_id).revision == 1  # still stale

        with pytest.raises(remote.ReadError) as exc:
            await remote.leave(p.b, p.a.peer_id, made.grant_id)
        assert exc.value.code == "no_grant"
        # refused: the share is live on the owner and stays visible on the receiver
        assert p.a.store.get_grant(made.grant_id).state == "live"
        assert p.b.store.get_remote(p.a.peer_id, made.grant_id) is not None
        assert f"peer-left:{made.grant_id}" not in _keys()
        # the refusal asked for the owner's LIST (not for the write again): the mirror follows
        await until(lambda: p.b.store.get_remote(p.a.peer_id, made.grant_id).revision == 2)
        assert p.a.store.get_grant(made.grant_id).state == "live"

        # a second Leave, with the revision the owner now lists, ends it
        assert await remote.leave(p.b, p.a.peer_id, made.grant_id) == {"left": True}
        assert p.a.store.get_grant(made.grant_id).end_reason == "peer_left"
        assert p.b.store.get_remote(p.a.peer_id, made.grant_id) is None
        await until(lambda: f"peer-left:{made.grant_id}" in _keys())


@pytest.mark.anyio
async def test_two_instances_an_unreachable_owner_leaves_both_sides_unchanged(
    tmp_path, monkeypatch, live
):
    async with paired(tmp_path, monkeypatch) as p:
        made = await _shared(p, monkeypatch)
        await p.quiet_receiver(monkeypatch)
        before = (p.a.store.get_grant(made.grant_id), p.a.store.generation(), _keys())
        # the owner is gone: its address answers nobody and the channel is down
        p.net.down.add("alpha")
        p.b.channel_for(p.a.peer_id).close("the link dropped")
        await until(lambda: p.b.channel_for(p.a.peer_id) is None)
        with pytest.raises(remote.ReadError) as exc:
            await remote.leave(p.b, p.a.peer_id, made.grant_id)
        assert exc.value.code == "peer_offline"  # it never left this instance
        assert (p.a.store.get_grant(made.grant_id), p.a.store.generation(), _keys()) == before
        assert p.b.store.get_remote(p.a.peer_id, made.grant_id) is not None  # nothing is hidden


@pytest.mark.anyio
async def test_two_instances_an_answer_lost_after_the_owner_ended_it_is_not_known_then_learned(
    tmp_path, monkeypatch, live
):
    """The owner ends the grant and the channel dies before its answer arrives. The receiver
    is told `peer_lost` (504 on the route), keeps its row, sends nothing again, and drops the
    row when the owner's own list no longer has it."""
    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    async with paired(tmp_path, monkeypatch) as p:
        made = await _shared(p, monkeypatch)
        real = sharing._leave
        calls: list[str] = []

        async def ended_then_cut(fed, peer_id, grant_id, revision):
            calls.append(grant_id)
            outcome = await real(fed, peer_id, grant_id, revision)
            fed.channel_for(peer_id).close("the link dropped")  # the answer never leaves
            await asyncio.sleep(0.05)
            return outcome

        monkeypatch.setattr(sharing, "_leave", ended_then_cut)
        with pytest.raises(remote.ReadError) as exc:
            await remote.leave(p.b, p.a.peer_id, made.grant_id)
        assert exc.value.code == remote.PEER_LOST
        assert p.a.store.get_grant(made.grant_id).end_reason == "peer_left"  # it did end
        assert not p.b.has_left(p.a.peer_id, made.grant_id)  # ... which this side does not know
        # the list, read again once the owner is back, settles it; Leave was sent exactly once
        await until(lambda: p.b.store.get_remote(p.a.peer_id, made.grant_id) is None, timeout=10)
        assert calls == [made.grant_id]
        await until(lambda: f"peer-left:{made.grant_id}" in _keys())


def test_two_running_apps_leave_through_the_route(two_instances, monkeypatch):
    a, b = two_instances
    a_id, b_id = _pair_instances(a, b)
    monkeypatch.setattr(shares, "session_state", lambda prov, native: "live")
    monkeypatch.setattr(notices, "_session_title", lambda key: TITLE)
    key = api_key()
    native = key.partition(":")[2]
    monkeypatch.setattr(
        sharing,
        "_session_rows",
        lambda _engine: {
            native: SimpleNamespace(uuid=native, archived=False, first_user_message="shared")
        },
    )

    def remote_rows() -> list[dict]:
        r = b.http.get("/api/federation/remote", headers=b.read)
        assert r.status_code == 200, r.text
        return r.json()["remote"]

    made = a.post("/grants", {"session_key": key, "peer_id": b_id, "level": "view"})
    assert made.status_code == 201, made.text
    grant = made.json()["grant"]
    (row,) = _wait(remote_rows)
    assert row["grant_id"] == grant["grant_id"] and row["revision"] == grant["revision"]
    base = f"/api/federation/remote/{a_id}/{grant['grant_id']}"
    assert b.http.post(f"{base}/leave", headers=b.read).status_code == 403  # no CSRF

    # a revision the owner no longer lists is refused, and nothing ends on either side
    stale = b.http.post(f"{base}/leave", json={"revision": row["revision"] + 1}, headers=b.write)
    assert (stale.status_code, stale.json()["code"]) == (404, "no_grant")
    owned = a.http.get("/api/federation/grants", headers=a.read).json()["grants"]
    assert [g["grant_id"] for g in owned] == [grant["grant_id"]]
    assert [r["grant_id"] for r in remote_rows()] == [grant["grant_id"]]

    left = b.http.post(f"{base}/leave", json={"revision": row["revision"]}, headers=b.write)
    assert left.status_code == 200 and left.json() == {"left": True}, left.text
    assert remote_rows() == []  # gone from the receiver's list with the answer
    assert a.http.get("/api/federation/grants", headers=a.read).json()["grants"] == []
    stored = a.fed.store.get_grant(grant["grant_id"])
    assert (stored.state, stored.end_reason) == ("revoked", "peer_left")
    assert b.http.get(f"{base}/snapshot", headers=b.read).status_code == 404
    _wait(lambda: [r for r in bell() if r["action_id"] == f"peer-left:{grant['grant_id']}"])
    again = b.http.post(f"{base}/leave", headers=b.write)
    assert (again.status_code, again.json()["code"]) == (404, "no_grant")
    time.sleep(0.3)
    assert len([r for r in bell() if r["action_id"].startswith("peer-left:")]) == 1
    assert remote_rows() == []
