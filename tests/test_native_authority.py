"""A native API turn carries its caller's execution authority across the process boundary (#1897).

The app admits a guarded turn inside `session_input`'s write fence and hands the worker two
durable tokens; every withdrawal in that fence replaces them; the worker claims only while they
are current, under `native_authority`'s lock. These tests drive the real worker (fake native
child) and the real fence, with isolated state (`host` pins every store under `tmp_path`).
"""

# ruff: noqa: F811 — `host` / `project` are the shared fixtures imported below

from __future__ import annotations

import asyncio
import contextlib
import dataclasses

import pytest

from agent_sessions import native_authority, native_ipc, native_state, session_input
from agent_sessions import structured_runtime as runtime
from agent_sessions.structured_types import ExecutionGuard
from test_native_runtime import (  # noqa: F401 — shared isolated fixtures
    ENGINES,
    frames,
    host,
    ident,
    project,
    settle,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@contextlib.asynccontextmanager
async def _never():  # the in-process half is the chat adapter's; a native turn never takes it
    raise AssertionError("a native turn must not take the in-process guard")
    yield  # pragma: no cover


def guard(key: str, *, allowed=lambda: (True, ""), after=None) -> ExecutionGuard:
    """A server guard for ``key``: ``allowed`` is the caller's domain check, run in the fence.
    ``after`` runs once admission returned, i.e. between the app's admit and the worker's claim."""

    def admit():
        tokens = session_input.admit_native(key, allowed)
        if after is not None:
            after()
        return tokens

    return ExecutionGuard("mission:m-1", _never, admit=admit)


def withdraw(key: str) -> None:
    """A mission authority withdrawal, as `mission_fence` commits one."""
    with session_input.sessions_transaction([key]):
        pass


def sent_texts(work) -> list[str]:
    out = []
    for f in frames(work):
        frame = f["frame"]
        if frame.get("method") in {"turn/start", "turn/steer", "session/prompt"}:
            params = frame.get("params") or {}
            items = params.get("input") or params.get("prompt") or []
            out.append("".join(i.get("text", "") for i in items if isinstance(i, dict)))
        elif frame.get("type") == "user":
            content = (frame.get("message") or {}).get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                out.append("".join(c.get("text", "") for c in content if isinstance(c, dict)))
    return out


async def _session(engine: str, work) -> str:
    return (await runtime.create_session(engine, str(work), operation_id=ident()))["session_key"]


# --- the record itself -------------------------------------------------------------------------


def test_tokens_are_replaced_by_every_withdrawal_and_a_lost_record_refuses(host):
    sid = ident()
    tokens = native_authority.admit(sid)
    assert native_authority.current(sid, tokens)
    assert native_authority.admit(sid) == tokens  # admission reads, it does not withdraw
    native_authority.bump_session(sid)
    assert not native_authority.current(sid, tokens)
    fresh = native_authority.admit(sid)
    native_authority.bump_policy()
    assert not native_authority.current(sid, fresh)
    # A record that is lost and recreated never matches a token handed out before it.
    latest = native_authority.admit(sid)
    native_authority._path(sid).unlink()
    assert not native_authority.current(sid, latest)
    assert native_authority.admit(sid)["session"] != latest["session"]
    with pytest.raises(native_authority.AuthorityError):
        with native_authority.held(sid, {"session": None, "policy": None}):
            pass


def test_a_malformed_record_refuses_and_a_session_never_guarded_gets_no_record(host):
    sid = ident()
    tokens = native_authority.admit(sid)
    native_state.write(native_authority._path(sid), {"token": "not-a-uuid"})
    assert not native_authority.current(sid, tokens)
    other = ident()
    native_authority.bump_session(other)
    assert not native_authority._path(other).exists()


def test_a_withdrawal_that_cannot_remove_the_record_is_aborted_before_it_commits(host, monkeypatch):
    key = f"{ENGINES['claude']}:{ident()}"
    sid = key.partition(":")[2]
    tokens = session_input.admit_native(key, lambda: (True, ""))
    real = native_authority._path

    class Stuck(type(real(sid))):
        def unlink(self, missing_ok=False):
            raise OSError("read-only")

    monkeypatch.setattr(native_authority, "_path", lambda name: Stuck(real(name)))
    committed = []
    with pytest.raises(session_input.AuthorityFenceBusy):
        with session_input.sessions_transaction([key]):
            committed.append(True)  # the store write: it must never run
    assert committed == []
    monkeypatch.setattr(native_authority, "_path", real)
    assert native_authority.current(sid, tokens)  # nothing withdrawn, nothing committed


def test_a_busy_authority_lock_aborts_the_withdrawal_within_its_bound(host, monkeypatch):
    import threading
    import time

    key = f"{ENGINES['claude']}:{ident()}"
    session_input.admit_native(key, lambda: (True, ""))
    monkeypatch.setattr(native_authority, "LOCK_WAIT", 0.2)
    held, release = threading.Event(), threading.Event()

    def worker_claiming():
        with native_authority._locked(wait=5):
            held.set()
            release.wait(10)

    t = threading.Thread(target=worker_claiming)
    t.start()
    try:
        assert held.wait(5)
        started = time.monotonic()
        committed = []
        with pytest.raises(session_input.AuthorityFenceBusy):
            with session_input.sessions_transaction([key]):
                committed.append(True)
        assert committed == [] and time.monotonic() - started < 3
    finally:
        release.set()
        t.join(10)
    with session_input.sessions_transaction([key]):  # the fence is not wedged afterwards
        pass


def test_the_fence_withdraws_before_its_change_commits_and_only_for_authority(host):
    key = f"{ENGINES['claude']}:{ident()}"
    sid = key.partition(":")[2]
    tokens = session_input.admit_native(key, lambda: (True, ""))
    seen = []
    with session_input.sessions_transaction([key]):
        # Inside the withdrawal, before its store write: a claim made now must already refuse.
        seen.append(native_authority.current(sid, tokens))
    assert seen == [False]
    tokens = session_input.admit_native(key, lambda: (True, ""))
    with session_input.session_transaction(key):
        seen.append(native_authority.current(sid, tokens))
    assert seen == [False, False]
    # A viewer attach / writer registration is not an authority withdrawal: tokens survive.
    tokens = session_input.admit_native(key, lambda: (True, ""))
    session_input.bump_epoch(key)
    assert native_authority.current(sid, tokens)
    # A roster pseudo-key names no session, whatever its id looks like.
    with session_input.sessions_transaction([f"mission-roster:{sid}"]):
        pass
    assert native_authority.current(sid, tokens)
    # Any policy edit, of any scope, withdraws; a rejected save over-refuses (the safe direction).
    with session_input.policy_transaction("session"):
        pass
    assert not native_authority.current(sid, tokens)
    tokens = session_input.admit_native(key, lambda: (True, ""))
    with contextlib.suppress(RuntimeError), session_input.policy_transaction("mission"):
        raise RuntimeError("rejected save")
    assert not native_authority.current(sid, tokens)
    with pytest.raises(session_input.AuthorityRefused, match="stood down"):
        session_input.admit_native(key, lambda: (False, "the mission stood down"))
    with pytest.raises(session_input.AuthorityRefused):
        session_input.admit_native("claude:not-a-uuid", lambda: (True, ""))


# --- the IPC frame ----------------------------------------------------------------------------


def test_the_binding_is_identity_and_the_tokens_are_per_attempt():
    base = {"operation_id": ident(), "expected_revision": 0, "text": "hi", "context": {}}
    tokens = {"authority_session": ident(), "authority_policy": ident()}
    with pytest.raises(native_ipc.IPCError):
        native_ipc._params("submit", {**base, "authority": "mission:m-1"})
    with pytest.raises(native_ipc.IPCError):
        native_ipc._params("submit", {**base, **tokens})
    with pytest.raises(native_ipc.IPCError):
        native_ipc._params("submit", {**base, **tokens, "authority": "../etc"})
    live = native_ipc._params("submit", {**base, **tokens, "authority": "mission:m-1"})
    assert live["authority"] == "mission:m-1"
    stored = native_ipc.normalize_immutable_request(
        {"action": "submit", "params": {"text": "hi", "context": {}, "authority": "mission:m-1"}}
    )
    assert stored["params"]["authority"] == "mission:m-1"
    with pytest.raises(native_ipc.IPCError):  # a token is never part of a stored identity
        native_ipc.normalize_immutable_request(
            {"action": "submit", "params": {"text": "hi", "context": {}, **tokens}}
        )


# --- end to end, through the real worker -------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_a_guarded_turn_is_claimed_and_replays_without_authority(host, project, source):
    key = await _session(ENGINES[source], project)
    turn = ident()
    first = await runtime.submit_turn(
        key, operation_id=turn, text="hello", execution_admission=guard(key)
    )
    assert first["operation_id"] == turn
    _, settled = await settle(key, turn)
    assert settled["state"] == "completed"
    withdraw(key)
    # An exact repeat observes the original effect; it is not a new one, so it needs no authority.
    repeat = await runtime.submit_turn(
        key,
        operation_id=turn,
        text="hello",
        execution_admission=guard(key, allowed=lambda: (False, "gone")),
    )
    assert repeat["state"] == "completed"
    # The binding is identity: the same id under another binding, or unguarded, is a conflict.
    with pytest.raises(runtime.StructuredError) as changed:
        await runtime.submit_turn(key, operation_id=turn, text="hello")
    assert changed.value.status == 409
    assert sent_texts(project).count("hello") == 1


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_a_withdrawal_between_admission_and_claim_sends_nothing(host, project, source):
    key = await _session(ENGINES[source], project)
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.submit_turn(
            key,
            operation_id=ident(),
            text="FENCED",
            execution_admission=guard(key, after=lambda: withdraw(key)),
        )
    assert refused.value.status == 409
    assert refused.value.reason == native_ipc.AUTHORITY_WITHDRAWN
    assert "FENCED" not in sent_texts(project)
    # The session itself is unharmed: the operator's own (unguarded) turn still goes through.
    mine = ident()
    await runtime.submit_turn(key, operation_id=mine, text="mine")
    _, settled = await settle(key, mine)
    assert settled["state"] == "completed"


@pytest.mark.anyio
async def test_a_domain_refusal_in_the_fence_is_a_known_non_send(host, project):
    key = await _session(ENGINES["claude"], project)
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.submit_turn(
            key,
            operation_id=ident(),
            text="NOPE",
            execution_admission=guard(key, allowed=lambda: (False, "the mission is paused")),
        )
    assert refused.value.reason == native_ipc.AUTHORITY_WITHDRAWN
    assert "paused" in refused.value.detail
    assert "NOPE" not in sent_texts(project)


@pytest.mark.anyio
async def test_a_queued_guarded_message_withdrawn_while_it_waits_is_not_sent(host, project):
    key = await _session(ENGINES["codex"], project)
    busy = ident()
    await runtime.submit_turn(key, operation_id=busy, text="WAIT_RELEASE")
    for _ in range(400):
        snap = await runtime.snapshot(key)
        if snap["state"] != "idle":
            break
        await asyncio.sleep(0.05)
    waiting = ident()
    queued = await runtime.submit_turn(
        key, operation_id=waiting, text="later", execution_admission=guard(key)
    )
    assert queued["state"] == "queued"
    withdraw(key)
    (project / "release-queued-turn").touch()
    snap, settled = await settle(key, waiting)
    assert settled["state"] == "failed"
    assert settled["not_sent"]["code"] == "authority_withdrawn"
    assert "later" not in sent_texts(project)


@pytest.mark.anyio
async def test_a_queued_guarded_message_still_authorized_is_delivered(host, project):
    key = await _session(ENGINES["codex"], project)
    await runtime.submit_turn(key, operation_id=ident(), text="WAIT_RELEASE")
    for _ in range(400):
        if (await runtime.snapshot(key))["state"] != "idle":
            break
        await asyncio.sleep(0.05)
    waiting = ident()
    await runtime.submit_turn(
        key, operation_id=waiting, text="later", execution_admission=guard(key)
    )
    (project / "release-queued-turn").touch()
    _, settled = await settle(key, waiting)
    assert settled["state"] == "completed"
    assert sent_texts(project).count("later") == 1


@pytest.mark.anyio
async def test_guards_stay_refused_where_they_cannot_be_enforced(host, project):
    key = await _session(ENGINES["claude"], project)
    in_process_only = ExecutionGuard("mission:m-1", _never)
    with pytest.raises(runtime.StructuredError, match="authority can't be checked") as refused:
        await runtime.submit_turn(
            key, operation_id=ident(), text="x", execution_admission=in_process_only
        )
    assert refused.value.status == 409
    with pytest.raises(runtime.StructuredError, match="caller authority"):
        await runtime.decide(
            key,
            turn_id=ident(),
            request_id="r",
            decision_id=ident(),
            decision="reject",
            user="op",
            execution_admission=guard(key),
        )
    # Guarded work never skips permission prompts, and a guarded create checks authority now.
    with pytest.raises(runtime.StructuredError, match="skip permission"):
        await runtime.create_session(
            ENGINES["claude"],
            str(project),
            operation_id=ident(),
            bypass=True,
            execution_admission=guard(f"{ENGINES['claude']}:{ident()}"),
        )
    sid = ident()
    with pytest.raises(runtime.StructuredError) as stood_down:
        await runtime.create_session(
            ENGINES["claude"],
            str(project),
            operation_id=sid,
            execution_admission=guard(
                f"{ENGINES['claude']}:{sid}", allowed=lambda: (False, "stood down")
            ),
        )
    assert stood_down.value.reason == native_ipc.AUTHORITY_WITHDRAWN
    created = await runtime.create_session(
        ENGINES["claude"],
        str(project),
        operation_id=sid,
        execution_admission=guard(f"{ENGINES['claude']}:{sid}"),
    )
    assert created["session_key"] == f"{ENGINES['claude']}:{sid}"
    # Admission is not the capability bit: a guarded create neither needs nor changes it.
    assert runtime.describe(ENGINES["claude"]).mission_ready is True
    assert runtime.describe(ENGINES["codex"]).mission_ready is False


@pytest.mark.anyio
async def test_a_claim_inside_a_withdrawal_before_its_commit_is_refused(host, project):
    """The tight window: the withdrawal has started but its store write has not committed yet.
    A claim landing exactly then must refuse (a withdrawal that commits must win)."""
    import threading

    key = await _session(ENGINES["claude"], project)
    inside, commit = threading.Event(), threading.Event()

    def withdrawal():
        with session_input.sessions_transaction([key]):
            inside.set()
            commit.wait(10)  # the caller's store write, still in progress

    def start_withdrawal():
        threading.Thread(target=withdrawal, daemon=True).start()
        assert inside.wait(5)

    try:
        with pytest.raises(runtime.StructuredError) as refused:
            await runtime.submit_turn(
                key,
                operation_id=ident(),
                text="WINDOW",
                execution_admission=guard(key, after=start_withdrawal),
            )
        assert refused.value.reason == native_ipc.AUTHORITY_WITHDRAWN
    finally:
        commit.set()
    assert "WINDOW" not in sent_texts(project)


@pytest.mark.anyio
async def test_a_worker_that_cannot_check_authority_gets_no_guarded_turn(
    host, project, monkeypatch
):
    key = await _session(ENGINES["claude"], project)
    real = native_state.read_lifecycle

    def older(worker_id):
        life = dict(real(worker_id))
        life.pop("authority", None)  # a worker from before #1897
        return life

    monkeypatch.setattr(native_state, "read_lifecycle", older)
    with pytest.raises(runtime.StructuredError) as refused:
        await runtime.submit_turn(
            key, operation_id=ident(), text="OLD", execution_admission=guard(key)
        )
    assert refused.value.status == 409
    assert refused.value.reason == native_ipc.AUTHORITY_UNSUPPORTED
    assert refused.value.remedy == "stop_resume"
    assert "OLD" not in sent_texts(project)
    monkeypatch.setattr(native_state, "read_lifecycle", real)
    mine = ident()  # the operator's own turn is unaffected
    await runtime.submit_turn(key, operation_id=mine, text="mine")
    _, settled = await settle(key, mine)
    assert settled["state"] == "completed"


def test_a_malformed_record_is_replaced_at_the_next_admission(host):
    sid = ident()
    tokens = native_authority.admit(sid)
    native_state.write(native_authority._path(sid), {"token": "not-a-uuid"})
    fresh = native_authority.admit(sid)  # not a 503 forever
    assert fresh["session"] != tokens["session"]
    assert native_authority.current(sid, fresh) and not native_authority.current(sid, tokens)


def test_an_unexpected_storage_error_aborts_the_withdrawal_as_fence_busy(host, monkeypatch):
    key = f"{ENGINES['claude']}:{ident()}"
    session_input.admit_native(key, lambda: (True, ""))

    def broken(*_a, **_k):
        raise ValueError("plugin state is not private")  # `storage.StateError`

    monkeypatch.setattr(native_authority, "_locked", broken)
    committed = []
    with pytest.raises(session_input.AuthorityFenceBusy):
        with session_input.sessions_transaction([key]):
            committed.append(True)
    assert committed == []


def test_a_single_authority_token_is_never_accepted():
    base = {"operation_id": ident(), "expected_revision": 0, "text": "hi", "context": {}}
    for partial in (
        {"authority_session": ident()},
        {"authority_policy": ident()},
        {"authority": "mission:m-1", "authority_session": ident()},
    ):
        with pytest.raises(native_ipc.IPCError):
            native_ipc._params("submit", {**base, **partial})


@pytest.mark.anyio
async def test_the_same_turn_under_another_binding_is_a_conflict_not_a_replay(host, project):
    key = await _session(ENGINES["claude"], project)
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="hello", execution_admission=guard(key))
    await settle(key, turn)
    other = ExecutionGuard(
        "mission:m-2", _never, admit=lambda: session_input.admit_native(key, lambda: (True, ""))
    )
    with pytest.raises(runtime.StructuredError) as changed:
        await runtime.submit_turn(key, operation_id=turn, text="hello", execution_admission=other)
    assert changed.value.status == 409
    assert sent_texts(project).count("hello") == 1


@pytest.mark.anyio
async def test_send_now_cannot_promote_a_withdrawn_guarded_message(host, project):
    key = await _session(ENGINES["codex"], project)
    busy = ident()
    await runtime.submit_turn(key, operation_id=busy, text="WAIT_RELEASE")
    for _ in range(400):
        if (await runtime.snapshot(key))["state"] != "idle":
            break
        await asyncio.sleep(0.05)
    waiting = ident()
    queued = await runtime.submit_turn(
        key, operation_id=waiting, text="later", execution_admission=guard(key)
    )
    assert queued["state"] == "queued"
    withdraw(key)
    with pytest.raises(runtime.StructuredError):
        await runtime.send_now(key, operation_id=ident(), turn_id=busy, queued_turn_id=waiting)
    assert "later" not in sent_texts(project)
    (project / "release-queued-turn").touch()
    _, settled = await settle(key, waiting)
    assert settled["not_sent"]["code"] == "authority_withdrawn"
    assert "later" not in sent_texts(project)


@pytest.mark.anyio
async def test_guarded_work_is_refused_in_a_session_that_skips_permissions(host, project):
    snap = await runtime.create_session(
        ENGINES["claude"], str(project), operation_id=ident(), bypass=True
    )
    key = snap["session_key"]
    await runtime.start_session(key)
    with pytest.raises(runtime.StructuredError, match="skips permission") as refused:
        await runtime.submit_turn(
            key, operation_id=ident(), text="SKIP", execution_admission=guard(key)
        )
    assert refused.value.status == 409
    assert "SKIP" not in sent_texts(project)


@pytest.mark.anyio
async def test_a_turn_the_operator_is_behind_goes_into_a_session_that_skips_permissions(
    host, project
):
    """#2008 (the operator's approval is on the issue): HQ's own send in a turn the Commander
    started, and the operator's tap, carry `attended`; the same turn without it is refused."""
    snap = await runtime.create_session(
        ENGINES["claude"], str(project), operation_id=ident(), bypass=True
    )
    key = snap["session_key"]
    await runtime.start_session(key)
    attended = dataclasses.replace(guard(key), attended=True)
    sent = await runtime.submit_turn(
        key, operation_id=ident(), text="ATTENDED", execution_admission=attended
    )
    await settle(key, sent["turn_id"])
    assert "ATTENDED" in sent_texts(project)
    with pytest.raises(runtime.StructuredError, match="skips permission"):
        await runtime.submit_turn(
            key, operation_id=ident(), text="UNATTENDED", execution_admission=guard(key)
        )
    assert "UNATTENDED" not in sent_texts(project)


@pytest.mark.anyio
async def test_an_attended_turn_still_needs_its_authority(host, project):
    """Attended lifts the skip-permissions refusal and nothing else: a withdrawn authority is
    still refused in the fence, before anything is sent."""
    snap = await runtime.create_session(
        ENGINES["claude"], str(project), operation_id=ident(), bypass=True
    )
    key = snap["session_key"]
    await runtime.start_session(key)
    refused = guard(key, allowed=lambda: (False, "session assistance is off"))
    with pytest.raises(runtime.StructuredError):
        await runtime.submit_turn(
            key,
            operation_id=ident(),
            text="NOPE",
            execution_admission=dataclasses.replace(refused, attended=True),
        )
    assert "NOPE" not in sent_texts(project)


def test_attended_is_a_real_bool_and_off_unless_set():
    assert ExecutionGuard("mission:m-1", _never).attended is False
    for bad in ("yes", 1, None):
        with pytest.raises(ValueError):
            ExecutionGuard("mission:m-1", _never, attended=bad)


def test_an_api_sessions_fence_key_is_the_key_its_record_is_named_after(host):
    """`mission_fence.held_keys` hands the fence PHYSICAL keys. An API session's id is minted
    before launch, so it has no placeholder: its physical key is its own key, and a withdrawal
    through that key (as for an adopted session) reaches its record. A console placeholder
    (`new-<uuid>`) is no native session and is skipped."""
    from agent_sessions import engines

    key = f"{ENGINES['claude']}:{ident()}"
    assert engines.physical_key(key) == key
    sid = key.partition(":")[2]
    tokens = session_input.admit_native(key, lambda: (True, ""))
    with session_input.sessions_transaction([engines.physical_key(key)]):
        pass
    assert not native_authority.current(sid, tokens)
    assert session_input._native_session_id(f"opencode:new-{sid}") is None
