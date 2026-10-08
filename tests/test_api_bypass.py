"""#1339 Phase 1: an API session can skip permission prompts — fixed at create, never flipped.

The operator approved the grant in #1339 (comment 89017). These pin that the mode reaches the
native protocol exactly as recorded, that every later generation reads it from the record (never
from a caller), and that an adapter which has not mapped it refuses rather than guessing."""

from __future__ import annotations

import pytest

from agent_sessions import native_protocol, native_runtime, native_state
from agent_sessions import structured_runtime as runtime
from agent_sessions.plugins import kinds
from test_native_clients import app_client  # noqa: F401 — fixture
from test_native_runtime import (  # noqa: F401 — fixtures
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


def _codex_params(work, method: str) -> list[dict]:
    return [f["frame"]["params"] for f in frames(work) if f["frame"].get("method") == method]


def _claude_argvs(work) -> set[tuple[str, ...]]:
    return {tuple(f["argv"]) for f in frames(work)}


def _mode(argv: tuple[str, ...]) -> str:
    return argv[argv.index("--permission-mode") + 1]


# --- the codecs -------------------------------------------------------------------------------


def test_codex_params_follow_the_mode():
    guarded = native_protocol.CodexCodec.create_params("/project", None)
    assert guarded["approvalPolicy"] == "untrusted" and guarded["sandbox"] == "read-only"
    skip = native_protocol.CodexCodec.create_params("/project", None, bypass=True)
    assert skip["approvalPolicy"] == "never" and skip["sandbox"] == "danger-full-access"


def test_claude_argv_follows_the_mode():
    sid = ident()
    assert _mode(tuple(native_protocol.ClaudeCodec.argv("/bin/claude", sid))) == "default"
    argv = native_protocol.ClaudeCodec.argv("/bin/claude", sid, bypass=True)
    assert _mode(tuple(argv)) == "bypassPermissions"
    assert "--dangerously-skip-permissions" not in argv  # the documented mode, not the flag


@pytest.mark.parametrize("truthy", ["true", 1, "yes"])
def test_only_a_real_true_skips(truthy):
    """A truthy non-bool never reaches the codec as a bypass."""
    params = native_protocol.CodexCodec.create_params("/project", None, bypass=truthy)
    assert params["approvalPolicy"] == "untrusted"
    argv = native_protocol.ClaudeCodec.argv("/bin/claude", ident(), bypass=truthy)
    assert _mode(tuple(argv)) == "default"


# --- create, record, resume -------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_a_bypass_session_never_asks_and_says_so(host, project, source):  # noqa: F811
    op = ident()
    snap = await runtime.create_session(ENGINES[source], str(project), operation_id=op, bypass=True)
    assert snap["bypass"] is True and snap["pending_start"] is True
    assert not host.launches  # phase 1 launches nothing
    started = await runtime.start_session(snap["session_key"])
    assert started["pending_start"] is False and len(host.launches) == 1
    turn = ident()
    await runtime.submit_turn(snap["session_key"], operation_id=turn, text="hello")
    await settle(snap["session_key"], turn)
    if source == "codex":
        (start,) = _codex_params(project, "thread/start")
        assert start["approvalPolicy"] == "never" and start["sandbox"] == "danger-full-access"
    else:
        assert {_mode(a) for a in _claude_argvs(project)} == {"bypassPermissions"}
    assert native_state.read_session(op)["request"]["bypass"] is True


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_a_guarded_session_is_unchanged(host, project, source):  # noqa: F811
    op = ident()
    snap = await runtime.create_session(ENGINES[source], str(project), operation_id=op)
    assert snap["bypass"] is False
    turn = ident()
    await runtime.submit_turn(snap["session_key"], operation_id=turn, text="hello")
    await settle(snap["session_key"], turn)
    if source == "codex":
        (start,) = _codex_params(project, "thread/start")
        assert start["approvalPolicy"] == "untrusted" and start["sandbox"] == "read-only"
    else:
        assert {_mode(a) for a in _claude_argvs(project)} == {"default"}
    # Byte-identical to a record written before the field existed: no `bypass` key at all.
    assert "bypass" not in native_state.read_session(op)["request"]


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude"])
@pytest.mark.parametrize("bypass", [True, False])
async def test_a_resume_keeps_the_recorded_mode(host, project, source, bypass):  # noqa: F811
    key = (
        await runtime.create_session(
            ENGINES[source], str(project), operation_id=ident(), bypass=bypass
        )
    )["session_key"]
    if bypass:
        await runtime.start_session(key)
    died = ident()
    await runtime.submit_turn(key, operation_id=died, text="DIE")
    await settle(key, died, state=("uncertain",))
    nxt = ident()
    await runtime.submit_turn(key, operation_id=nxt, text="again")
    await settle(key, nxt)
    assert len(host.launches) == 2  # a second generation, resumed from the record
    if source == "codex":
        (resume,) = _codex_params(project, "thread/resume")
        assert resume["approvalPolicy"] == ("never" if bypass else "untrusted")
        assert resume["sandbox"] == ("danger-full-access" if bypass else "read-only")
    else:
        resumed = [a for a in _claude_argvs(project) if any(x.startswith("--resume=") for x in a)]
        assert resumed and {_mode(a) for a in resumed} == {
            "bypassPermissions" if bypass else "default"
        }
    assert (await runtime.snapshot(key))["bypass"] is bypass


@pytest.mark.anyio
@pytest.mark.parametrize("first", [True, False])
async def test_a_replay_with_the_other_mode_conflicts(host, project, first):  # noqa: F811
    op = ident()
    snap = await runtime.create_session(
        ENGINES["claude"], str(project), operation_id=op, bypass=first
    )
    again = await runtime.create_session(
        ENGINES["claude"], str(project), operation_id=op, bypass=first
    )
    assert again["session_key"] == snap["session_key"]  # an exact replay observes
    with pytest.raises(runtime.StructuredError) as exc:
        await runtime.create_session(
            ENGINES["claude"], str(project), operation_id=op, bypass=not first
        )
    assert exc.value.status == 409
    assert len(host.launches) == (0 if first else 1)  # a skip creation waits for `start`


@pytest.mark.anyio
async def test_an_adapter_without_a_mapping_refuses(host, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(kinds, "API_BYPASS_KINDS", frozenset({"claude-stream-json"}))
    op = ident()
    with pytest.raises(runtime.StructuredError) as exc:
        await runtime.create_session(ENGINES["codex"], str(project), operation_id=op, bypass=True)
    assert exc.value.status == 422
    assert native_state.read_session(op) is None and not host.launches
    # The same client still creates guarded sessions.
    await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident())


@pytest.mark.anyio
async def test_a_non_bool_bypass_is_refused_before_anything_is_written(host, project):  # noqa: F811
    with pytest.raises(native_runtime.NativeError) as exc:
        await native_runtime.create(
            ENGINES["claude"], str(project), session_id=ident(), bypass="true"
        )
    assert exc.value.status == 422 and not host.launches


@pytest.mark.anyio
async def test_the_chat_runtime_has_no_prompts_to_skip():
    with pytest.raises(runtime.StructuredError) as exc:
        await runtime._ChatAdapter().create("apichat", "/tmp", session_id=ident(), bypass=True)
    assert exc.value.status == 422


# --- the route and the roster -----------------------------------------------------------------


@pytest.mark.parametrize("value", ["true", 1, None, [True]])
def test_the_route_takes_only_a_boolean(app_client, value):  # noqa: F811
    r = app_client.post(
        "/api/structured/sessions",
        json={"engine": "codex-api", "cwd": "/tmp", "operation_id": ident(), "bypass": value},
    )
    assert r.status_code == 422 and "bypass" in r.json()["detail"]


def test_the_route_still_refuses_unknown_fields(app_client):  # noqa: F811
    r = app_client.post(
        "/api/structured/sessions",
        json={
            "engine": "codex-api",
            "cwd": "/tmp",
            "operation_id": ident(),
            "bypass": True,
            "approvalPolicy": "never",
        },
    )
    assert r.status_code == 422


def test_the_roster_says_which_clients_can_skip(app_client):  # noqa: F811
    rows = {r["id"]: r for r in app_client.get("/api/engines").json()["engines"]}
    assert rows["codex-api"]["api"]["can_bypass"] is True
    assert rows["claude-api"]["api"]["can_bypass"] is True
    assert rows["codex"]["api"] is None


def test_every_bypass_kind_is_a_real_api_kind():
    assert kinds.API_BYPASS_KINDS <= kinds.API_KINDS


def test_no_shell_reaches_the_bypass_launch():
    """The shell-free launcher rule holds for the new argv too: a literal list, no shell."""
    argv = native_protocol.ClaudeCodec.argv("/bin/claude", ident(), bypass=True)
    assert isinstance(argv, list) and all(isinstance(a, str) for a in argv)
    assert not any(a in ("sh", "bash", "-c") for a in argv)


# --- the two-phase start (operator decision on #1341, option 1) -------------------------------


async def _pending(source, project):  # noqa: F811
    op = ident()
    snap = await runtime.create_session(ENGINES[source], str(project), operation_id=op, bypass=True)
    return op, snap["session_key"]


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_a_skip_creation_reserves_and_launches_nothing_until_start(host, project, source):  # noqa: F811
    from agent_sessions import native_ownership

    op, key = await _pending(source, project)
    snap = await runtime.snapshot(key)
    assert snap["pending_start"] is True and snap["start_expires_at"] > 0
    assert not host.launches
    assert native_ownership.lookup(key) is None  # console launches of the source are not held
    # Every other path that could launch it refuses: a turn, a replayed create.
    with pytest.raises(runtime.StructuredError) as exc:
        await runtime.submit_turn(key, operation_id=ident(), text="hello")
    assert exc.value.status == 409
    assert (await runtime.snapshot(key))["turns"] == []  # refused before anything was recorded
    again = await runtime.create_session(
        ENGINES[source], str(project), operation_id=op, bypass=True
    )
    assert again["pending_start"] is True and not host.launches


@pytest.mark.anyio
async def test_start_is_idempotent(host, project):  # noqa: F811
    _, key = await _pending("claude", project)
    await runtime.start_session(key)
    again = await runtime.start_session(key)  # a retried start after a lost response
    assert again["pending_start"] is False and len(host.launches) == 1


@pytest.mark.anyio
async def test_an_unstarted_skip_creation_expires_and_never_runs(host, project, monkeypatch):  # noqa: F811
    monkeypatch.setattr(native_runtime, "PENDING_START_TTL", -1.0)
    op, key = await _pending("codex", project)
    snap = await runtime.snapshot(key)
    assert snap["pending_start"] is False and snap["start_expired"] is True
    with pytest.raises(runtime.StructuredError) as exc:
        await runtime.start_session(key)
    assert exc.value.status == 410
    assert native_state.read_session(op)["start"]["state"] == "expired" and not host.launches
    with pytest.raises(runtime.StructuredError):  # terminal: a later start is refused too
        await runtime.start_session(key)
    assert not host.launches


@pytest.mark.anyio
async def test_a_discarded_skip_creation_can_never_be_started(host, project):  # noqa: F811
    _, key = await _pending("claude", project)
    await runtime.stop(key)  # Discard
    with pytest.raises(runtime.StructuredError) as exc:
        await runtime.start_session(key)
    assert exc.value.status == 409 and not host.launches


@pytest.mark.anyio
async def test_a_guarded_creation_has_nothing_to_start(host, project):  # noqa: F811
    snap = await runtime.create_session(ENGINES["codex"], str(project), operation_id=ident())
    assert snap["pending_start"] is False and len(host.launches) == 1
    again = await runtime.start_session(snap["session_key"])  # observes, never a second launch
    assert again["pending_start"] is False and len(host.launches) == 1


def test_the_start_route_needs_a_known_session(app_client):  # noqa: F811
    r = app_client.post(f"/api/structured/sessions/codex-api:{ident()}/start")
    assert r.status_code == 404


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_an_interrupted_start_is_reconciled_only_by_start(host, project, source, monkeypatch):  # noqa: F811
    """Hermes 5908: start used to clear `pending_start` BEFORE the launch, so a failure between
    the two left a record that looked started — and an exact create replay then launched it.
    Now start records an authorization first; until a launch happened, nothing but `start` may
    launch it, and the view keeps offering Start."""
    op, key = await _pending(source, project)
    real = native_runtime._reserve_and_launch_locked

    def fail(*a, **k):
        raise native_runtime.NativeError(409, "the agent changed or was removed")

    monkeypatch.setattr(native_runtime, "_reserve_and_launch_locked", fail)
    with pytest.raises(runtime.StructuredError):
        await runtime.start_session(key)
    record = native_state.read_session(op)
    assert record["start"]["state"] == "failed" and not record.get("generations")
    snap = await runtime.snapshot(key)
    assert snap["pending_start"] is True and snap["start_incomplete"] is True
    monkeypatch.setattr(native_runtime, "_reserve_and_launch_locked", real)
    # An exact create replay only observes; a turn is refused without side effects.
    again = await runtime.create_session(
        ENGINES[source], str(project), operation_id=op, bypass=True
    )
    assert again["pending_start"] is True and not host.launches
    with pytest.raises(runtime.StructuredError) as exc:
        await runtime.submit_turn(key, operation_id=ident(), text="hello")
    assert exc.value.status == 409 and not host.launches
    # Only a start reconciles it — and launches exactly once.
    done = await runtime.start_session(key)
    assert done["pending_start"] is False and len(host.launches) == 1
    await runtime.start_session(key)
    assert len(host.launches) == 1


@pytest.mark.anyio
async def test_an_authorized_start_outlives_its_expiry(host, project, monkeypatch):  # noqa: F811
    """The expiry bounds a PENDING start; a failed one (already confirmed) can still be retried."""
    op, key = await _pending("claude", project)
    real = native_runtime._reserve_and_launch_locked
    monkeypatch.setattr(
        native_runtime,
        "_reserve_and_launch_locked",
        lambda *a, **k: (_ for _ in ()).throw(native_runtime.NativeError(409, "x")),
    )
    with pytest.raises(runtime.StructuredError):
        await runtime.start_session(key)
    monkeypatch.setattr(native_runtime, "_reserve_and_launch_locked", real)
    record = native_state.read_session(op)
    record["start"]["expires_at"] = 0
    native_state.write_session(op, record)
    await runtime.start_session(key)
    assert len(host.launches) == 1


@pytest.mark.anyio
async def test_a_retried_start_rechecks_readiness(host, project, monkeypatch):  # noqa: F811
    """Hermes 5913: an authorized-but-unlaunched start skipped readiness on retry."""
    op, key = await _pending("codex", project)
    real = native_runtime._reserve_and_launch_locked
    monkeypatch.setattr(
        native_runtime,
        "_reserve_and_launch_locked",
        lambda *a, **k: (_ for _ in ()).throw(native_runtime.NativeError(409, "x")),
    )
    with pytest.raises(runtime.StructuredError):
        await runtime.start_session(key)
    monkeypatch.setattr(native_runtime, "_reserve_and_launch_locked", real)
    monkeypatch.setattr(native_runtime, "readiness", lambda prov: (False, "the CLI went away"))
    with pytest.raises(runtime.StructuredError) as exc:
        await runtime.start_session(key)
    assert "went away" in exc.value.detail and not host.launches


@pytest.mark.anyio
async def test_a_waiting_skip_session_is_listed_like_any_session(host, project):  # noqa: F811
    """A lost skip create keeps no client slot (Hermes 5913): the server's list is the record."""
    from agent_sessions import chat_store, engines

    _, key = await _pending("claude", project)
    native = key.partition(":")[2]
    prov = engines.get(ENGINES["claude"])
    if prov.kind is None:
        prov.attach_kind(chat_store.ChatStoreKind())
    assert native in [r.uuid for r in prov.kind.scan()]


@pytest.mark.anyio
@pytest.mark.parametrize("source", ["codex", "claude"])
async def test_a_launch_that_fails_after_its_intent_is_not_a_start(
    host,  # noqa: F811
    project,  # noqa: F811
    source,
    monkeypatch,
):
    """Hermes 5920: the generation counter was bumped at launch INTENT, so a launch that then
    failed read as started — a retry no-op'd, and (Claude) an ordinary turn launched the skip
    worker. A start now completes only when the worker is READY; until then it stays retryable."""
    op, key = await _pending(source, project)
    real = native_runtime._wait_ready
    calls = {"n": 0}

    async def fail_once(worker_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise native_runtime.NativeError(503, "the native worker stopped: boom")
        return await real(worker_id)

    monkeypatch.setattr(native_runtime, "_wait_ready", fail_once)
    with pytest.raises(runtime.StructuredError):
        await runtime.start_session(key)
    snap = await runtime.snapshot(key)
    assert snap["pending_start"] is True and snap["start_incomplete"] is True
    first_launches = len(host.launches)
    with pytest.raises(runtime.StructuredError) as exc:  # a turn never completes a failed start
        await runtime.submit_turn(key, operation_id=ident(), text="hello")
    assert exc.value.status == 409 and len(host.launches) == first_launches
    done = await runtime.start_session(key)  # the retry really launches again
    assert done["pending_start"] is False and len(host.launches) == first_launches + 1
    assert native_state.read_session(op)["start"]["state"] == "started"
    await runtime.start_session(key)  # complete: a further start only observes
    assert len(host.launches) == first_launches + 1
    turn = ident()
    await runtime.submit_turn(key, operation_id=turn, text="hello")
    await settle(key, turn)
