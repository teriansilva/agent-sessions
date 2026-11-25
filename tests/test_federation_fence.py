"""The per-grant fence (#2057): one `flock` per grant, shared by every process over a store.

Every race is forced in BOTH orders by events, never by sleeping: `submits.gate` holds a delivery
inside its submit, and `waiting` is set the moment a fence wait finds the fence held (the first
poll of `fence._acquire_until`). "Another process" is modelled twice: by a second `Store` object
over the same directory (fences are files, so a second object contends exactly as a second
process does), and by a REAL second OS process (`_child`).
"""

# ruff: noqa: F811 — fixtures imported from the other federation test modules
from __future__ import annotations

import ast
import asyncio
import contextlib
import os
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import agent_sessions
from agent_sessions import shares
from agent_sessions.federation import channel as ch
from agent_sessions.federation import fence, grants, inputs, store
from federation_helpers import until
from test_federation_grants import api_key, live, trusted  # noqa: F401 — `live` is a fixture
from test_federation_inputs import (  # noqa: F401 — fixtures
    Owner,
    expire,
    held,
    msg,
    owner,
    submits,
)
from test_federation_routes import _client, _login

SRC = Path(agent_sessions.__file__).parent
GID = "g" * 22


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def waiting(monkeypatch):
    """Set when a fence wait finds the fence HELD: the poll loop sleeps only after an attempt
    that failed."""
    found = threading.Event()

    class _Time:
        monotonic = staticmethod(time.monotonic)

        @staticmethod
        def sleep(seconds: float) -> None:
            found.set()
            time.sleep(seconds)

    monkeypatch.setattr(fence, "time", _Time)
    return found


# ── the fence itself ──────────────────────────────────────────────────


def test_two_holders_in_one_process_contend_and_a_release_lets_the_next_in(tmp_path):
    fd = fence.acquire(tmp_path, GID, 0)
    assert fence.held_elsewhere(tmp_path, GID)
    with pytest.raises(fence.FenceBusy):  # a fresh fd each time: the same process contends
        fence.acquire(tmp_path, GID, 0.05)
    assert not fence.held_elsewhere(tmp_path, "h" * 22)  # one fence per grant
    fence.release(fd)
    fence.release(fd)  # idempotent
    fence.release(None)
    again = fence.acquire(tmp_path, GID, 0)
    fence.release(again)
    path = fence.path_for(tmp_path, GID)
    assert path == tmp_path / "fences" / GID and path.exists()  # never unlinked
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("bad", ["", "../../etc/passwd", "a/b", "x" * 200, "short", None, 7])
def test_a_fence_is_only_ever_named_by_a_grant_id(tmp_path, bad):
    with pytest.raises(ValueError):
        fence.path_for(tmp_path, bad)
    with pytest.raises(ValueError):
        fence.acquire(tmp_path, bad, 0)
    with pytest.raises(ValueError), fence.hold(tmp_path, [GID, bad], timeout=0):
        pass
    assert not (tmp_path / "fences").exists() or not list((tmp_path / "fences").iterdir())


def test_hold_takes_fences_in_grant_id_order_under_one_deadline_and_releases_all(
    tmp_path, monkeypatch
):
    order: list[str] = []
    real = fence._acquire_until

    def spy(path, deadline):
        order.append(path.name)
        return real(path, deadline)

    monkeypatch.setattr(fence, "_acquire_until", spy)
    ids = ["c" * 22, "a" * 22, "b" * 22, "a" * 22]
    with fence.hold(tmp_path, ids, timeout=1):
        assert all(fence.held_elsewhere(tmp_path, i) for i in set(ids))
    assert order[:3] == ["a" * 22, "b" * 22, "c" * 22]
    assert not any(fence.held_elsewhere(tmp_path, i) for i in set(ids))

    # one of them is held: the ones already taken are released, the body never runs
    blocker = fence.acquire(tmp_path, "b" * 22, 0)
    ran = []
    started = time.monotonic()
    with pytest.raises(fence.FenceBusy), fence.hold(tmp_path, ids, timeout=0.1):
        ran.append(1)
    assert not ran and time.monotonic() - started < 5
    assert not fence.held_elsewhere(tmp_path, "a" * 22)
    fence.release(blocker)


_HOLDER = """
import sys
from pathlib import Path
from agent_sessions.federation import fence
fd = fence.acquire(Path(sys.argv[1]), sys.argv[2], 10)
print("held", flush=True)
sys.stdin.readline()
"""


def _python(script: str, *args: str) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(SRC.parent)}
    return subprocess.Popen(  # noqa: S603 — the test's own interpreter and script
        [sys.executable, "-c", script, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=env,
    )


def test_a_real_second_process_holds_the_fence_and_its_death_releases_it(owner, monkeypatch):
    made = owner.grant("send")
    child = _python(_HOLDER, str(owner.st.directory), made.grant_id)
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(fence.FenceBusy):
            fence.acquire(owner.st.directory, made.grant_id, 0.05)
        monkeypatch.setattr(fence, "REVOKE_WAIT_S", 0.05)
        before = owner.st.generation()
        with pytest.raises(grants.GrantError) as exc:
            grants.revoke(owner.st, made.grant_id)
        assert (exc.value.code, exc.value.status) == ("busy", 503)
        assert owner.st.get_grant(made.grant_id).state == "live"
        assert owner.st.generation() == before
        # SIGKILL: no handler runs, no `finally`. The kernel closes the fd and with it the lock.
        os.kill(child.pid, signal.SIGKILL)
        child.wait(10)
        monkeypatch.setattr(fence, "REVOKE_WAIT_S", 10.0)
        assert grants.revoke(owner.st, made.grant_id).state == "revoked"
    finally:
        with contextlib.suppress(ProcessLookupError):
            child.kill()
        child.wait(10)


# ── races in one process, both orders ─────────────────────────────────

ENDERS = ["revoke", "peer_revoke", "archive", "extend", "lower"]


def ender(action: str, o: Owner, made: store.Grant, st: store.Store, live_states: dict):
    """An async callable that ends or changes ``made`` through ``st`` (the owner's own store
    object, or a second one over the same directory)."""

    async def run() -> None:
        if action == "revoke":
            await asyncio.to_thread(grants.revoke, st, made.grant_id)
        elif action == "peer_revoke":
            await asyncio.to_thread(st.revoke, o.peer.peer_id)
        elif action == "archive" and st is o.st:
            # the real archive path: the session lock, the link fences, then the grant fences
            async with shares.archiving(made.session_key):
                live_states[made.session_key] = "archived"
        elif action == "archive":
            await asyncio.to_thread(st.end_grants_for_session, made.session_key)
            live_states[made.session_key] = "archived"
        elif action == "extend":
            await asyncio.to_thread(
                lambda: grants.change(
                    st,
                    made.grant_id,
                    expires_in=1800,
                    revision=made.revision,
                    accept_risk=made.level == "send",
                )
            )
        else:
            await asyncio.to_thread(
                lambda: grants.change(
                    st, made.grant_id, expires_in=None, revision=made.revision, level="view"
                )
            )

    return run


def landed(action: str, o: Owner, made: store.Grant) -> bool:
    now = o.st.get_grant(made.grant_id)
    if action == "extend":
        return now.revision == made.revision + 1 and now.state == "live"
    if action == "lower":
        return now.level == "view" and now.revision == made.revision + 1
    if action == "peer_revoke":
        return now.state == "revoked" and not o.st.get(o.peer.peer_id).trusted
    return now.state == "revoked"


def untouched(o: Owner, made: store.Grant, generation: int) -> bool:
    return (
        o.st.get_grant(made.grant_id) == made
        and o.st.generation() == generation
        and o.st.get(o.peer.peer_id).trusted
    )


def stores(o: Owner, writer: str) -> store.Store:
    return o.st if writer == "same" else store.Store(o.st.directory)


async def _held_then_released(o, submits, waiting, effect, change, made, check) -> object:
    """The delivery is admitted FIRST and sits in its submit; the change starts, finds the fence
    held and commits nothing; the submit returns; only then does the change land."""
    generation = o.st.generation()
    submits.gate = asyncio.Event()
    delivery = asyncio.create_task(effect())
    await submits.started.wait()
    waiting.clear()
    changing = asyncio.create_task(change())
    assert await asyncio.to_thread(waiting.wait, 20), "the change never met the fence"
    # In the middle: the delivery is in flight, and NOTHING of the change is committed.
    assert untouched(o, made, generation) and not changing.done()
    assert submits.finished == 0 and fence.held_elsewhere(o.st.directory, made.grant_id)
    submits.gate.set()
    result = (await asyncio.gather(delivery, return_exceptions=True))[0]
    await asyncio.wait_for(changing, 20)
    assert check()
    assert len(submits.delivered) == 1  # admitted under a live grant, exactly once
    assert not fence.held_elsewhere(o.st.directory, made.grant_id)
    return result


@pytest.mark.anyio
@pytest.mark.parametrize("writer", ["same", "other"])
@pytest.mark.parametrize("action", ENDERS)
async def test_send_first_then_a_change_the_change_waits_for_the_submit_and_lands_after(
    owner,
    submits,
    waiting,
    live,
    action,
    writer,
):
    made = owner.grant("send")
    result = await _held_then_released(
        owner,
        submits,
        waiting,
        lambda: owner.call("input.send", msg(made)),
        ender(action, owner, made, stores(owner, writer), live),
        made,
        lambda: landed(action, owner, made),
    )
    # The turn was admitted under a live grant. Its answer is the receipt, or `no_grant` when the
    # change committed before the answer's own check ran: never anything in between.
    if isinstance(result, dict):
        assert result == {"operation_id_known": True, "handoff": "sent"}
    else:
        assert isinstance(result, ch.VerbError) and result.code == "no_grant"


@pytest.mark.anyio
@pytest.mark.parametrize("writer", ["same", "other"])
@pytest.mark.parametrize("action", ENDERS)
async def test_a_change_first_then_send_nothing_is_delivered(
    owner,
    submits,
    live,
    action,
    writer,
):
    made = owner.grant("send")
    await ender(action, owner, made, stores(owner, writer), live)()
    assert landed(action, owner, made)
    # the revision the peer holds is the old one (an extension and a downgrade bump it too)
    assert await owner.refused("input.send", msg(made)) == "no_grant"
    assert submits.calls == [] and owner.st.peer_authors(made.session_key) == {}
    if action == "extend":  # with the new revision the extended grant serves again
        now = owner.st.get_grant(made.grant_id)
        assert (await owner.call("input.send", msg(now)))["handoff"] == "sent"


@pytest.mark.anyio
@pytest.mark.parametrize("writer", ["same", "other"])
@pytest.mark.parametrize("action", ["revoke", "peer_revoke", "archive", "lower"])
async def test_approve_first_then_a_change_the_change_waits_and_lands_after(
    owner,
    submits,
    waiting,
    live,
    action,
    writer,
):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made))
    await owner.quiet()
    result = await _held_then_released(
        owner,
        submits,
        waiting,
        lambda: inputs.decide_approve(owner.fed, one["suggestion_id"], approver="marcus"),
        ender(action, owner, made, stores(owner, writer), live),
        made,
        lambda: landed(action, owner, made),
    )
    assert isinstance(result, store.Suggestion) and result.state == "approved"
    assert owner.suggestion(one["suggestion_id"]).state == "approved"  # not undone by the end


@pytest.mark.anyio
@pytest.mark.parametrize("writer", ["same", "other"])
@pytest.mark.parametrize("action", ["revoke", "peer_revoke", "archive", "lower"])
async def test_a_change_first_then_approve_nothing_is_delivered(
    owner,
    submits,
    live,
    action,
    writer,
):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made))
    await ender(action, owner, made, stores(owner, writer), live)()
    with pytest.raises(inputs.InputError) as exc:
        await inputs.decide_approve(owner.fed, one["suggestion_id"], approver="marcus")
    assert (exc.value.status, exc.value.state) == (409, "withdrawn")
    assert submits.calls == [] and landed(action, owner, made)


@pytest.mark.anyio
@pytest.mark.parametrize("order", ["replay_first", "revoke_first"])
async def test_a_revoke_racing_a_replay_has_exactly_two_outcomes(
    owner,
    submits,
    waiting,
    live,
    order,
):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made))
    sid = one["suggestion_id"]
    await asyncio.to_thread(inputs._begin_approval, owner.st, sid, None, "marcus")
    other = store.Store(owner.st.directory)
    if order == "replay_first":
        result = await _held_then_released(
            owner,
            submits,
            waiting,
            lambda: inputs.resume_approving(owner.fed, sid),
            ender("revoke", owner, made, other, live),
            made,
            lambda: landed("revoke", owner, made),
        )
        assert result.state == "approved" and len(submits.delivered) == 1
    else:
        # the replay is parked on the fence while the revoke commits under it
        fd = await asyncio.to_thread(fence.acquire, owner.st.directory, made.grant_id, 5)
        waiting.clear()
        replay = asyncio.create_task(inputs.resume_approving(owner.fed, sid))
        assert await asyncio.to_thread(waiting.wait, 20)
        assert submits.calls == [] and owner.suggestion(sid).state == "approving"
        with other._write() as con:  # what the holder of the fence commits
            other._end_grants(
                con, "grant_id = ?", (made.grant_id,), reason="revoked", actor="operator", now=1.0
            )
        fence.release(fd)
        result = await replay
        assert result.state == "withdrawn" and submits.calls == []


# ── released at the admission boundary ────────────────────────────────


@pytest.mark.anyio
async def test_a_cancelled_caller_leaves_the_fence_held_until_the_submit_returned(
    owner, submits, waiting
):
    made = owner.grant("send")
    generation = owner.st.generation()
    submits.gate = asyncio.Event()
    caller = asyncio.create_task(owner.call("input.send", msg(made)))
    await submits.started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    # The caller is gone. The submit is not: it was never asked to stop, and the fence is held.
    assert submits.finished == 0 and fence.in_flight() == 1
    assert fence.held_elsewhere(owner.st.directory, made.grant_id)
    waiting.clear()
    revoking = asyncio.create_task(asyncio.to_thread(grants.revoke, owner.st, made.grant_id))
    assert await asyncio.to_thread(waiting.wait, 20)
    assert untouched(owner, made, generation) and not revoking.done()
    submits.gate.set()
    assert (await asyncio.wait_for(revoking, 20)).state == "revoked"  # and lands after it
    assert submits.finished == 1 and len(submits.delivered) == 1
    await until(lambda: fence.in_flight() == 0)
    assert not fence.held_elsewhere(owner.st.directory, made.grant_id)
    # the turn was admitted under a live grant and is attributed
    assert len(owner.st.peer_authors(made.session_key)) == 1


@pytest.mark.anyio
async def test_a_submit_that_raises_releases_the_fence_only_then(owner, submits, waiting):
    from agent_sessions import structured_runtime as runtime

    made = owner.grant("send")
    submits.gate = asyncio.Event()
    submits.error = runtime.StructuredError(503, "unavailable")
    caller = asyncio.create_task(owner.refused("input.send", msg(made)))
    await submits.started.wait()
    assert fence.held_elsewhere(owner.st.directory, made.grant_id)
    waiting.clear()
    revoking = asyncio.create_task(asyncio.to_thread(grants.revoke, owner.st, made.grant_id))
    assert await asyncio.to_thread(waiting.wait, 20)
    assert not revoking.done() and owner.st.get_grant(made.grant_id).state == "live"
    submits.gate.set()
    assert await caller == "unavailable"
    assert (await asyncio.wait_for(revoking, 20)).state == "revoked"
    assert submits.delivered == []


@pytest.mark.anyio
async def test_a_caller_cancelled_while_the_fence_is_still_being_waited_for_leaks_nothing(
    owner, submits, waiting
):
    made = owner.grant("send")
    fd = await asyncio.to_thread(fence.acquire, owner.st.directory, made.grant_id, 5)
    waiting.clear()
    caller = asyncio.create_task(owner.call("input.send", msg(made)))
    assert await asyncio.to_thread(waiting.wait, 20)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    fence.release(fd)
    # the delivery was not cancelled with its caller: it gets the fence, delivers, releases
    await until(lambda: fence.in_flight() == 0)
    assert len(submits.delivered) == 1
    assert not fence.held_elsewhere(owner.st.directory, made.grant_id)


@pytest.mark.anyio
async def test_a_delivery_that_cannot_get_the_fence_is_busy_and_delivers_nothing(
    owner, submits, monkeypatch
):
    made = owner.grant("send")
    monkeypatch.setattr(fence, "DELIVERY_WAIT_S", 0.05)
    fd = await asyncio.to_thread(fence.acquire, owner.st.directory, made.grant_id, 5)
    first = msg(made)
    assert await owner.refused("input.send", first) == "busy"
    one = await owner.call("input.suggest", msg(made))
    with pytest.raises(inputs.InputError) as exc:
        await inputs.decide_approve(owner.fed, one["suggestion_id"], approver="marcus")
    assert (exc.value.code, exc.value.status) == ("busy", 503)
    assert submits.calls == [] and owner.suggestion(one["suggestion_id"]).state == "pending"
    assert owner.st.get_peer_turn(made.grant_id, first["client_key"]) is None
    fence.release(fd)
    assert (await owner.call("input.send", first))["handoff"] == "sent"


# ── a change that cannot get the fence commits nothing ────────────────


@pytest.mark.anyio
async def test_a_change_whose_wait_runs_out_commits_nothing_and_a_retry_succeeds(
    owner,
    submits,
    live,
    monkeypatch,
):
    made = owner.grant("send")
    generation = owner.st.generation()
    events = owner.st.grant_events(made.grant_id)
    submits.gate = asyncio.Event()
    sending = asyncio.create_task(owner.call("input.send", msg(made)))
    await submits.started.wait()
    monkeypatch.setattr(fence, "REVOKE_WAIT_S", 0.05)
    other = store.Store(owner.st.directory)

    def busy(fn, *args, **kwargs) -> None:
        with pytest.raises((grants.GrantError, store.Busy)) as exc:
            fn(*args, **kwargs)
        assert exc.value.status == 503
        assert getattr(exc.value, "code", "busy") == "busy"

    for st in (owner.st, other):
        await asyncio.to_thread(busy, grants.revoke, st, made.grant_id)
        await asyncio.to_thread(
            busy, grants.change, st, made.grant_id, expires_in=600, revision=1, accept_risk=True
        )
        await asyncio.to_thread(
            busy, grants.change, st, made.grant_id, expires_in=None, revision=1, level="view"
        )
        await asyncio.to_thread(busy, st.revoke, owner.peer.peer_id)
        await asyncio.to_thread(busy, st.end_grants_for_session, made.session_key)
    with pytest.raises(shares.SharesUnavailable):  # the archive is REFUSED: its body never runs
        async with shares.archiving(made.session_key):
            raise AssertionError("archived around a delivery in flight")
    with pytest.raises(Exception) as exc:
        await owner.fed.revoke(owner.peer.peer_id)
    assert (exc.value.code, exc.value.status) == ("busy", 503)

    # Not one of them committed anything: the grant, its revision, its events, the generation
    # and the peer's trust are exactly as before, and the delivery is still in flight.
    assert untouched(owner, made, generation)
    assert owner.st.grant_events(made.grant_id) == events
    assert submits.finished == 0

    submits.gate.set()
    assert (await sending)["handoff"] == "sent"
    monkeypatch.setattr(fence, "REVOKE_WAIT_S", 10.0)
    assert (await asyncio.to_thread(grants.revoke, owner.st, made.grant_id)).state == "revoked"
    assert len(submits.delivered) == 1


def test_the_grant_and_peer_routes_answer_503_busy_and_the_retry_succeeds(
    auth_cfg,
    monkeypatch,
    live,
):
    c = _client(auth_cfg)
    hdr = _login(c, auth_cfg)
    st = c.app.state.federation.store
    peer = trusted(st, "GPU box")
    made = grants.create(
        st, session_key=api_key(), peer_id=peer.peer_id, level="send", expires_in=None,
        accept_risk=True,
    )  # fmt: skip
    monkeypatch.setattr(fence, "REVOKE_WAIT_S", 0.05)
    fd = fence.acquire(st.directory, made.grant_id, 0)  # a delivery in flight, from anywhere
    base = "/api/federation"
    for method, path, body in (
        ("POST", f"{base}/grants/{made.grant_id}/revoke", None),
        ("PATCH", f"{base}/grants/{made.grant_id}", {"expires_in": 600, "revision": 1,
                                                    "accept_risk": True}),
        ("PATCH", f"{base}/grants/{made.grant_id}", {"level": "view", "revision": 1}),
        ("POST", f"{base}/peers/{peer.peer_id}/revoke", None),
    ):  # fmt: skip
        r = c.request(method, path, json=body, headers=hdr)
        assert r.status_code == 503, (path, r.text)
        assert r.json()["code"] == "busy"
    listed = c.get(f"{base}/grants").json()["grants"]
    assert [(g["grant_id"], g["state"], g["revision"]) for g in listed] == [
        (made.grant_id, "live", 1)
    ]
    assert c.get(base).json()["peers"][0]["state"] == "trusted"
    fence.release(fd)
    assert c.post(f"{base}/grants/{made.grant_id}/revoke", headers=hdr).status_code == 200
    assert c.post(f"{base}/peers/{peer.peer_id}/revoke", headers=hdr).status_code == 200
    assert st.get_grant(made.grant_id).state == "revoked" and not st.get(peer.peer_id).trusted


def test_revoking_a_peer_that_only_views_waits_on_no_fence(owner, waiting, monkeypatch):
    view = owner.grant("view")
    taken: list[str] = []
    real = fence._acquire_until

    def spy(path, deadline):
        taken.append(path.name)
        return real(path, deadline)

    monkeypatch.setattr(fence, "_acquire_until", spy)
    # Even with the view grant's fence held by someone, the peer revoke does not ask for it.
    blocker = real(fence.path_for(owner.st.directory, view.grant_id), time.monotonic())
    monkeypatch.setattr(fence, "REVOKE_WAIT_S", 0.0)
    assert not owner.st.revoke(owner.peer.peer_id).trusted
    assert taken == [] and not waiting.is_set()
    assert owner.st.get_grant(view.grant_id).end_reason == "peer_revoked"
    fence.release(blocker)


def test_a_peer_revoke_takes_exactly_its_suggest_and_send_fences_in_order(owner, monkeypatch):
    other = trusted(owner.st, "other")
    mine = [owner.grant(level) for level in ("send", "view", "suggest", "send", "view")]
    theirs = owner.grant("send", peer=other)
    order: list[str] = []
    real = fence._acquire_until
    in_txn: list[bool] = []
    real_write = store.Store._write

    def spy(path, deadline):
        order.append(path.name)
        return real(path, deadline)

    def is_held(directory, grant_id) -> bool:
        try:
            fence.release(real(fence.path_for(directory, grant_id), time.monotonic()))
        except fence.FenceBusy:
            return True
        return False

    @contextlib.contextmanager
    def write(self):
        # the transaction begins only once every fence is held
        in_txn.append(all(is_held(self.directory, g) for g in list(order)))
        with real_write(self) as con:
            yield con

    monkeypatch.setattr(fence, "_acquire_until", spy)
    monkeypatch.setattr(store.Store, "_write", write)
    owner.st.revoke(owner.peer.peer_id)
    expected = sorted(g.grant_id for g in mine if g.level != "view")
    assert order == expected and in_txn == [True]
    assert theirs.grant_id not in order
    assert all(owner.st.get_grant(g.grant_id).state == "revoked" for g in mine)
    assert owner.st.get_grant(theirs.grant_id).state == "live"


def test_a_grant_raised_between_the_read_and_the_transaction_is_fenced_before_anything_commits(
    owner, monkeypatch
):
    """A peer revoke reads "no send grants", and the grant is raised before its transaction.
    The transaction sees a grant it holds no fence for, writes nothing, and starts again."""
    view = owner.grant("view")
    other = store.Store(owner.st.directory)
    holds: list[list[str]] = []
    real = fence.hold

    @contextlib.contextmanager
    def racing(directory, ids, *, timeout):
        ids = sorted(ids)
        holds.append(ids)
        if len(holds) == 1:
            other.change_grant(
                view.grant_id, expires_at=time.time() + 600, revision=1, level="send"
            )
        with real(directory, ids, timeout=timeout):
            yield

    monkeypatch.setattr(fence, "hold", racing)
    assert not owner.st.revoke(owner.peer.peer_id).trusted
    assert holds[0] == [] and holds[-1] == [view.grant_id]

    # and while that grant's delivery is in flight, the revoke fails closed
    again = trusted(owner.st, "again")
    raised = owner.grant("view", peer=again)
    holds.clear()
    monkeypatch.setattr(fence, "REVOKE_WAIT_S", 0.05)
    blocker = fence.acquire(owner.st.directory, raised.grant_id, 0)

    @contextlib.contextmanager
    def racing_busy(directory, ids, *, timeout):
        ids = sorted(ids)
        holds.append(ids)
        if len(holds) == 1:
            with other._write() as con:  # the holder of the fence raises its own grant
                con.execute(
                    "UPDATE grants SET level = 'send', revision = 2 WHERE grant_id = ?",
                    (raised.grant_id,),
                )
        with real(directory, ids, timeout=timeout):
            yield

    monkeypatch.setattr(fence, "hold", racing_busy)
    with pytest.raises(store.Busy):
        owner.st.revoke(again.peer_id)
    assert owner.st.get(again.peer_id).trusted
    assert owner.st.get_grant(raised.grant_id).state == "live"
    fence.release(blocker)


def test_an_archive_takes_the_session_lock_then_every_grant_fence_in_order_then_one_transaction(
    owner,
    live,
    monkeypatch,
):
    key = api_key()
    peers = [owner.peer, trusted(owner.st, "b"), trusted(owner.st, "c")]
    made = [
        owner.grant(level, key, peer=p)
        for level, p in zip(("view", "send", "suggest"), peers, strict=False)
    ]
    elsewhere = owner.grant("send")
    log: list[tuple] = []
    real, real_write = fence._acquire_until, store.Store._write
    lock = shares.store().session_lock(key)

    def spy(path, deadline):
        log.append(("fence", path.name, lock.locked()))
        return real(path, deadline)

    @contextlib.contextmanager
    def write(self):
        log.append(("txn", lock.locked()))
        with real_write(self) as con:
            yield con

    monkeypatch.setattr(fence, "_acquire_until", spy)
    monkeypatch.setattr(store.Store, "_write", write)

    async def archive() -> None:
        async with shares.archiving(key):
            log.append(("move", lock.locked()))

    asyncio.run(archive())
    ordered = sorted(g.grant_id for g in made)  # a view grant's too: all of the session's
    assert log == [*(("fence", g, True) for g in ordered), ("txn", True), ("move", True)]
    assert all(owner.st.get_grant(g.grant_id).end_reason == "archived" for g in made)
    assert owner.st.get_grant(elsewhere.grant_id).state == "live" and not lock.locked()


def test_every_grant_write_in_the_store_goes_through_the_fenced_helper():
    """In the source: `_end_grants` and the statements that change a grant row run only inside a
    function handed to `_with_fences`, and `fence.hold` is called from that helper alone."""
    source = (SRC / "federation" / "store.py").read_text()
    tree = ast.parse(source)
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Store")
    methods = {n.name: n for n in cls.body if isinstance(n, ast.FunctionDef)}

    def calls(node: ast.AST, name: str) -> bool:
        return any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == name
            for n in ast.walk(node)
        )

    holders = {name for name, node in methods.items() if calls(node, "hold")}
    assert holders == {"_with_fences"}
    enders = {name for name, node in methods.items() if calls(node, "_end_grants")}
    assert enders == {"revoke", "create_grant", "revoke_grant", "end_grants_for_session",
                      "expire_due", "leave_grant"}  # fmt: skip
    writers = {
        name
        for name, node in methods.items()
        if any(
            isinstance(n, ast.Constant)
            and isinstance(n.value, str)
            and ("UPDATE grants" in n.value or "INSERT INTO grants" in n.value)
            for n in ast.walk(node)
        )
    }
    assert writers == {"_end_grants", "create_grant", "change_grant"}
    for name in enders | (writers - {"_end_grants"}):
        node = methods[name]
        assert calls(node, "_with_fences"), name
        assert not calls(node, "_write"), name  # no transaction of its own beside the fenced one
    # the helper takes the fences BEFORE it begins: `_write` is nested inside `fence.hold`
    helper = ast.unparse(methods["_with_fences"])
    assert helper.index("fence.hold(") < helper.index("self._write()")
    # and nothing outside the store writes a grant row
    for path in SRC.rglob("*.py"):
        if path.name != "store.py" or path.parent.name != "federation":
            text = path.read_text()
            assert "UPDATE grants" not in text and "INSERT INTO grants" not in text, path
    # a delivery never takes the session lock or a public link's fence
    for module in ("inputs.py", "fence.py"):
        text = (SRC / "federation" / module).read_text()
        names = {n.attr for n in ast.walk(ast.parse(text)) if isinstance(n, ast.Attribute)}
        assert not names & {"session_lock", "archiving", "revoke_for_session"}, module


@pytest.mark.anyio
async def test_the_expiry_sweep_skips_a_grant_in_flight_and_ends_it_on_a_later_round(
    owner, submits
):
    made = owner.grant("send")
    idle = owner.grant("send")
    one = await owner.call("input.suggest", msg(idle))
    submits.gate = asyncio.Event()
    sending = asyncio.create_task(owner.call("input.send", msg(made)))
    await submits.started.wait()
    expire(owner.st, made.grant_id)  # the clock passes while the admitted submit unwinds
    expire(owner.st, idle.grant_id)
    ended = await asyncio.to_thread(grants.sweep, owner.st)
    assert [g.grant_id for g in ended] == [idle.grant_id]  # it never waits
    assert owner.st.get_grant(made.grant_id).state == "live"
    assert owner.suggestion(one["suggestion_id"]).state == "withdrawn"
    submits.gate.set()
    with contextlib.suppress(ch.VerbError):  # delivered; its answer's check sees the expiry
        await sending
    assert len(submits.delivered) == 1
    assert [g.grant_id for g in await asyncio.to_thread(grants.sweep, owner.st)] == [made.grant_id]
    assert owner.st.get_grant(made.grant_id).end_reason == "expired"


@pytest.mark.anyio
async def test_an_extension_under_the_fence_is_seen_by_the_sweep(owner):
    made = owner.grant("suggest")
    expire(owner.st, made.grant_id)
    assert owner.st.get_grant(made.grant_id).expires_at < time.time()
    # the sweep read "due", and the grant is extended before the sweep's own transaction
    real = fence.hold

    @contextlib.contextmanager
    def racing(directory, ids, *, timeout):
        with real(directory, ids, timeout=timeout):
            con = sqlite3.connect(owner.st.db_path)
            con.execute(
                "UPDATE grants SET expires_at = ? WHERE grant_id = ?",
                (time.time() + 600, made.grant_id),
            )
            con.commit()
            con.close()
            yield

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fence, "hold", racing)
        assert grants.sweep(owner.st) == []
    assert owner.st.get_grant(made.grant_id).state == "live"


# ── two real processes over one store ─────────────────────────────────

_CHILD = """
import sys, time
from pathlib import Path
from agent_sessions.federation import fence, store

directory, action, grant_id, peer_id = sys.argv[1:5]


class _Time:
    monotonic = staticmethod(time.monotonic)
    said = False

    @staticmethod
    def sleep(seconds):
        if not _Time.said:
            _Time.said = True
            print("waiting", flush=True)  # this process found the fence held
        time.sleep(seconds)


fence.time = _Time
st = store.Store(Path(directory))
print("start", flush=True)
if action == "revoke":
    st.revoke_grant(grant_id)
elif action == "peer_revoke":
    st.revoke(peer_id)
elif action == "extend":
    st.change_grant(grant_id, expires_at=time.time() + 1800, revision=1)
else:
    st.change_grant(grant_id, expires_at=time.time() + 600, revision=1, level="view")
print("done", flush=True)
"""

CHILD_ACTIONS = ["revoke", "peer_revoke", "extend", "lower"]


def _child(o: Owner, action: str, made: store.Grant) -> subprocess.Popen:
    return _python(_CHILD, str(o.st.directory), action, made.grant_id, o.peer.peer_id)


@pytest.mark.anyio
@pytest.mark.parametrize("action", CHILD_ACTIONS)
async def test_a_second_process_writes_while_the_first_delivers_and_lands_after_the_delivery(
    owner, submits, action
):
    made = owner.grant("send")
    generation = owner.st.generation()
    submits.gate = asyncio.Event()
    sending = asyncio.create_task(owner.call("input.send", msg(made)))
    await submits.started.wait()
    child = _child(owner, action, made)
    try:
        line = child.stdout.readline
        assert (await asyncio.to_thread(line)).strip() == "start"
        # The other process met the fence this one holds...
        assert (await asyncio.to_thread(line)).strip() == "waiting"
        # ...and committed nothing while the delivery is in flight.
        assert untouched(owner, made, generation) and child.poll() is None
        assert submits.finished == 0
        submits.gate.set()
        result = (await asyncio.gather(sending, return_exceptions=True))[0]
        assert (await asyncio.to_thread(line)).strip() == "done"
        assert await asyncio.to_thread(child.wait, 20) == 0
    finally:
        child.kill()
        child.wait(10)
    assert landed(action, owner, made)  # it follows the delivery
    assert len(submits.delivered) == 1
    if isinstance(result, dict):
        assert result == {"operation_id_known": True, "handoff": "sent"}
    else:
        assert isinstance(result, ch.VerbError) and result.code == "no_grant"


@pytest.mark.anyio
@pytest.mark.parametrize("action", CHILD_ACTIONS)
async def test_a_second_process_writes_first_and_the_delivery_does_not_get_through(
    owner, submits, action
):
    made = owner.grant("send")
    child = _child(owner, action, made)
    try:
        out, _ = await asyncio.to_thread(child.communicate, None, 30)
    finally:
        child.kill()
        child.wait(10)
    assert child.returncode == 0 and out.split() == ["start", "done"]  # it met no fence
    assert landed(action, owner, made)
    # under the OLD revision nothing gets through, for an extension and a downgrade too
    assert await owner.refused("input.send", msg(made)) == "no_grant"
    assert submits.calls == []
    now = owner.st.get_grant(made.grant_id)
    if action == "lower":  # and the new revision is a view grant: still nothing
        assert await owner.refused("input.send", msg(now)) == "no_grant"
        assert submits.calls == []


@pytest.mark.anyio
async def test_a_second_process_revokes_while_the_first_delivers_an_approval(owner, submits):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made))
    generation = owner.st.generation()
    submits.gate = asyncio.Event()
    approving = asyncio.create_task(
        inputs.decide_approve(owner.fed, one["suggestion_id"], approver="marcus")
    )
    await submits.started.wait()
    child = _child(owner, "revoke", made)
    try:
        line = child.stdout.readline
        assert (await asyncio.to_thread(line)).strip() == "start"
        assert (await asyncio.to_thread(line)).strip() == "waiting"
        assert untouched(owner, made, generation)
        submits.gate.set()
        assert (await approving).state == "approved"
        assert await asyncio.to_thread(child.wait, 20) == 0
    finally:
        child.kill()
        child.wait(10)
    assert landed("revoke", owner, made) and len(submits.delivered) == 1
    assert owner.suggestion(one["suggestion_id"]).state == "approved"


def test_the_fence_module_never_cancels_a_delivery():
    """Asking a submit to stop is not its completion: nothing in the fence or the delivery code
    cancels the body, and the only release is in the body's own `finally`."""
    for module in ("fence.py", "inputs.py"):
        tree = ast.parse((SRC / "federation" / module).read_text())
        cancels = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("cancel", "wait_for")
        ]
        assert cancels == [], module
    tree = ast.parse((SRC / "federation" / "fence.py").read_text())
    start = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "start_fenced"
    )
    run = next(n for n in ast.walk(start) if isinstance(n, ast.AsyncFunctionDef))
    guarded = next(
        n for n in ast.walk(run) if isinstance(n, ast.Try) and "await body()" in ast.unparse(n.body)
    )
    assert "release(fd)" in ast.unparse(guarded.finalbody) and not guarded.handlers


@pytest.mark.anyio
@pytest.mark.parametrize("action", ENDERS)
async def test_a_change_between_the_arrival_check_and_the_fence_is_caught_inside_the_fence(
    owner,
    submits,
    live,
    monkeypatch,
    action,
):
    """The call's first check passed. The grant ends or changes before the delivery holds the
    fence. The re-check INSIDE the fence is what refuses it: nothing is recorded or submitted."""
    made = owner.grant("send")
    other = store.Store(owner.st.directory)
    real = fence.start_fenced
    change = ender(action, owner, made, other, live)

    def racing(directory, grant_id, body, *, wait=None):
        async def after_the_change():
            await change()  # commits under the fence, before this delivery takes it
            return await real(directory, grant_id, body, wait=wait)

        return asyncio.ensure_future(after_the_change())

    monkeypatch.setattr(fence, "start_fenced", racing)
    first = msg(made)
    assert await owner.refused("input.send", first) == "no_grant"
    assert landed(action, owner, made)
    assert (
        submits.calls == [] and owner.st.get_peer_turn(made.grant_id, first["client_key"]) is None
    )


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["revoke", "peer_revoke", "archive", "lower"])
async def test_a_change_between_the_approve_tap_and_the_fence_is_caught_inside_the_fence(
    owner,
    submits,
    live,
    monkeypatch,
    action,
):
    made = owner.grant("suggest")
    one = await owner.call("input.suggest", msg(made))
    other = store.Store(owner.st.directory)
    real = fence.run_fenced
    change = ender(action, owner, made, other, live)

    async def racing(directory, grant_id, body, *, wait=None):
        await change()
        return await real(directory, grant_id, body, wait=wait)

    monkeypatch.setattr(fence, "run_fenced", racing)
    with pytest.raises(inputs.InputError) as exc:
        await inputs.decide_approve(owner.fed, one["suggestion_id"], approver="marcus")
    assert exc.value.status == 409
    assert submits.calls == [] and owner.suggestion(one["suggestion_id"]).state == "withdrawn"
    assert owner.suggestion(one["suggestion_id"]).approved_by is None


def test_a_fence_that_cannot_be_opened_fails_closed(owner, monkeypatch):
    made = owner.grant("send")

    def broken(path, deadline):
        raise PermissionError("fences/ is not writable")

    monkeypatch.setattr(fence, "_acquire_until", broken)
    with pytest.raises(grants.GrantError) as exc:
        grants.revoke(owner.st, made.grant_id)
    assert exc.value.status == 503
    with pytest.raises(store.StoreError) as exc:
        owner.st.revoke(owner.peer.peer_id)
    assert exc.value.status == 503
    assert owner.st.get_grant(made.grant_id).state == "live"
    assert owner.st.get(owner.peer.peer_id).trusted
