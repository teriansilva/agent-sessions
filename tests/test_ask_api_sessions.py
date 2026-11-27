# ruff: noqa: F811 — `host` / `project` are the shared native fixtures imported below
"""HQ starts an API session from a card (#2014).

The real path end to end: `ask_proposals.decide` → `_launch_api` → `mission_structured.hq_start` →
the structured facade → the real native worker over the fake native child (`host`), with the
conversation store under `tmp_path`. A card pins its kind (`runtime: "api"`) when it is shown, and
the tap starts exactly that: one session and one ATTENDED, fenced first turn, ids derived from the
proposal — skipping permission prompts only when the operator confirmed it on a card whose adapter
can (#2351): a guarded create, then the operator's own `enable_bypass` switch before the brief,
all under the attended guard.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
import uuid

import pytest

from agent_sessions import (
    ask_proposals,
    ask_store,
    automation_runner,
    mission_structured,
    mission_targets,
    prefs,
)
from agent_sessions import structured_runtime as runtime
from test_native_runtime import (  # noqa: F401 — shared isolated fixtures
    ENGINES,
    frames,
    host,
    project,
)

CLAUDE = ENGINES["claude"]
BRIEF = "Refresh the WG overview and draft replies for approval."


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def env(host, project, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_ASK_DIR", str(tmp_path / "ask"))
    monkeypatch.setenv("AGENT_SESSIONS_LOCK_DIR", str(tmp_path / "locks"))
    prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})
    prefs.set_agent_defaults({"bypass": False})
    state = {"project": {"id": "p1", "name": "WG", "cwd": str(project)}}

    def resolve(pid, *, index=None):
        p = state["project"]
        if p is not None and p["id"] == pid:
            return dict(p), ""
        return None, "the chosen project no longer exists"

    monkeypatch.setattr(mission_targets, "resolve_chosen_project", resolve)
    monkeypatch.setattr(automation_runner, "folder_ok", lambda _p: (True, ""))
    ask_proposals._TASKS.clear()
    yield state
    ask_proposals._TASKS.clear()


def _users(work) -> list[str]:
    out = []
    for f in frames(work):
        frame = f["frame"]
        if frame.get("type") == "user":
            content = (frame.get("message") or {}).get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                out.append("".join(c.get("text", "") for c in content if isinstance(c, dict)))
    return out


def _card(project, *, engine=CLAUDE, runtime_kind="api", bypass_default=False) -> tuple[str, str]:
    cid = ask_store.create()
    pid = uuid.uuid4().hex
    entry = {
        "id": pid,
        "turn_id": str(uuid.uuid4()),
        "kind": "session",
        "state": ask_proposals.PENDING,
        "ts": time.time(),
        "engine": engine,
        "agent": "Claude — API",
        "project_id": "p1",
        "project": "WG",
        "cwd": str(project),
        "real": os.path.realpath(project),
        "brief": BRIEF,
        "bypass_default": bypass_default,
        "bypass": None,
        **({"runtime": runtime_kind} if runtime_kind else {}),
    }
    ask_proposals.add(cid, entry)
    return cid, pid


async def _tap(pid: str, *, bypass=False) -> dict:
    status, view = await ask_proposals.decide(pid, "start", bypass=bypass, registry=None)
    assert status in (200, 202), view
    task = ask_proposals._TASKS.get(pid)
    if task is not None:
        await task
    cid = ask_proposals.locate(pid)
    return ask_proposals.view(ask_proposals._row(cid, pid))


def _key(pid: str) -> tuple[str, str]:
    session_id, brief_id = mission_structured.hq_ids(pid)
    return f"{CLAUDE}:{session_id}", brief_id


@pytest.mark.anyio
async def test_a_tap_starts_one_api_session_and_one_attended_fenced_first_turn(project):
    _cid, pid = _card(project)
    view = await _tap(pid)
    key, brief_id = _key(pid)
    assert view["state"] == ask_proposals.STARTED, view
    assert view["runtime"] == "api" and view["session_key"] == key
    snap = await runtime.snapshot(key)
    assert not snap["bypass"], "an API session HQ starts never skips permission prompts"
    first = next(t for t in snap["turns"] if t["turn_id"] == brief_id)
    assert first["context"] == {"reservation_id": pid}
    assert _users(project).count(BRIEF) == 1
    await runtime.stop(key)


@pytest.mark.anyio
async def test_a_replayed_tap_starts_nothing_more(project):
    _cid, pid = _card(project)
    await _tap(pid)
    again = await _tap(pid)  # the same tap again: the settled entry comes back as it is
    assert again["state"] == ask_proposals.STARTED
    key, brief_id = _key(pid)
    snap = await runtime.snapshot(key)
    assert [t["turn_id"] for t in snap["turns"] if t.get("kind") != "agent"] == [brief_id]
    assert _users(project).count(BRIEF) == 1
    await runtime.stop(key)


@pytest.mark.anyio
async def test_the_same_proposal_replays_the_same_create_and_turn(project):
    """Defence in depth under the decide lock: `hq_start` twice for one proposal derives the same
    ids and replays, never a second session or a second brief."""
    _cid, pid = _card(project)
    for _ in range(2):
        out = await mission_structured.hq_start(
            proposal_id=pid,
            engine=CLAUDE,
            cwd=str(project),
            brief=BRIEF,
            on_key=lambda _k: None,
            check=lambda: (True, ""),
        )
        assert out.ok, out.reason
    key, brief_id = _key(pid)
    snap = await runtime.snapshot(key)
    assert [t["turn_id"] for t in snap["turns"] if t.get("kind") != "agent"] == [brief_id]
    assert _users(project).count(BRIEF) == 1
    await runtime.stop(key)


@pytest.mark.anyio
async def test_bypass_on_an_api_card_that_cannot_skip_is_refused_and_nothing_starts(project):
    """An adapter outside `API_BYPASS_KINDS` (opencode2, #2108) never skips prompts: the card
    offers no switch, and a tap asking for bypass anyway is refused before anything runs."""
    engine = ENGINES["opencode2"]
    assert ask_proposals.can_bypass(engine) is False
    assert ask_proposals.bypass_default("p1", engine=engine) is False
    _cid, pid = _card(project, engine=engine)
    assert ask_proposals.view(ask_proposals._row(_cid, pid))["can_bypass"] is False
    with pytest.raises(ask_proposals.ProposalError) as e:
        await ask_proposals.decide(pid, "start", bypass=True, registry=None)
    assert e.value.status == 422
    session_id, _b = mission_structured.hq_ids(pid)
    assert not await runtime.may_have_worker(f"{engine}:{session_id}")
    assert _users(project) == []


@pytest.mark.anyio
async def test_bypass_on_a_capable_api_card_creates_starts_and_briefs_once(project):
    """#2351: the operator confirmed bypass on a card whose adapter maps it — the session is
    created guarded, switched to skip prompts before the brief, and the brief is its ONE first
    turn."""
    prefs.set_agent_defaults({"bypass": True})
    assert ask_proposals.can_bypass(CLAUDE) is True
    _cid, pid = _card(project, bypass_default=True)
    view = await _tap(pid, bypass=True)
    key, brief_id = _key(pid)
    assert view["state"] == ask_proposals.STARTED, view
    assert view["bypass"] is True
    snap = await runtime.snapshot(key)
    assert snap["bypass"] is True and not snap.get("pending_start")
    assert [t["turn_id"] for t in snap["turns"] if t.get("kind") != "agent"] == [brief_id]
    assert _users(project).count(BRIEF) == 1
    # The same confirmation again: the settled entry comes back, nothing more starts.
    again = await _tap(pid, bypass=True)
    assert again["state"] == ask_proposals.STARTED
    assert _users(project).count(BRIEF) == 1
    await runtime.stop(key)


@pytest.mark.anyio
async def test_a_default_changed_inside_the_api_launch_launches_nothing_and_asks_again(
    project, monkeypatch
):
    """The bypass default flips after the tap's own check and before the create's admission: the
    fenced check refuses, nothing is launched, and the card asks again with the new value."""
    prefs.set_agent_defaults({"bypass": True})
    _cid, pid = _card(project, bypass_default=True)
    real = runtime.create_session

    async def flip_then_create(*a, **kw):
        prefs.set_agent_defaults({"bypass": False})
        return await real(*a, **kw)

    monkeypatch.setattr(runtime, "create_session", flip_then_create)
    view = await _tap(pid, bypass=True)
    assert view["state"] == ask_proposals.PENDING and view["notice"] == "default_changed"
    assert view["bypass_default"] is False
    key, _b = _key(pid)
    assert not await runtime.may_have_worker(key)
    assert _users(project) == []


@pytest.mark.anyio
async def test_a_default_changed_after_the_create_keeps_the_confirmed_value_and_starts_once(
    project, monkeypatch
):
    """Hermes on #2357: the default flips AFTER the create's admission. That is after the API
    card's spawn boundary, as on a terminal card: the operator's confirmed value stands, the
    session is switched and briefed once, and nothing is reconfirmed or replayed."""
    prefs.set_agent_defaults({"bypass": True})
    _cid, pid = _card(project, bypass_default=True)
    real = runtime.create_session

    async def create_then_flip(*a, **kw):
        out = await real(*a, **kw)
        prefs.set_agent_defaults({"bypass": False})
        return out

    monkeypatch.setattr(runtime, "create_session", create_then_flip)
    view = await _tap(pid, bypass=True)
    key, brief_id = _key(pid)
    assert view["state"] == ask_proposals.STARTED, view
    snap = await runtime.snapshot(key)
    assert snap["bypass"] is True
    assert [t["turn_id"] for t in snap["turns"] if t.get("kind") != "agent"] == [brief_id]
    assert _users(project).count(BRIEF) == 1
    await runtime.stop(key)


@pytest.mark.anyio
async def test_a_card_that_asks_again_starts_once_with_the_value_confirmed_second(
    project, monkeypatch
):
    """The first confirmation (bypass on) is refused at the create's admission by a changed
    default; the refreshed card is confirmed with bypass OFF. Nothing from the first attempt
    exists, so the second is one ordinary guarded start and one brief."""
    prefs.set_agent_defaults({"bypass": True})
    _cid, pid = _card(project, bypass_default=True)
    real = runtime.create_session
    flips = {"n": 0}

    async def flip_once_then_create(*a, **kw):
        if flips["n"] == 0:
            prefs.set_agent_defaults({"bypass": False})
        flips["n"] += 1
        return await real(*a, **kw)

    monkeypatch.setattr(runtime, "create_session", flip_once_then_create)
    first = await _tap(pid, bypass=True)
    assert first["state"] == ask_proposals.PENDING and first["notice"] == "default_changed"
    second = await _tap(pid, bypass=False)
    key, brief_id = _key(pid)
    assert second["state"] == ask_proposals.STARTED, second
    snap = await runtime.snapshot(key)
    assert not snap["bypass"]
    assert [t["turn_id"] for t in snap["turns"] if t.get("kind") != "agent"] == [brief_id]
    assert _users(project).count(BRIEF) == 1
    await runtime.stop(key)


@pytest.mark.anyio
async def test_a_withdrawal_between_create_and_switch_skips_nothing_and_sends_nothing(
    project, monkeypatch
):
    """Session assistance is turned off after the create and before the bypass switch: the
    switch's own admission refuses, the session never skips prompts, no brief is sent and the
    worker is contained."""
    prefs.set_agent_defaults({"bypass": True})
    _cid, pid = _card(project, bypass_default=True)
    real = runtime.create_session

    async def create_then_withdraw(*a, **kw):
        out = await real(*a, **kw)
        prefs.set_orchestrator({"enabled": False, "autonomy": "suggest"})
        return out

    monkeypatch.setattr(runtime, "create_session", create_then_withdraw)
    view = await _tap(pid, bypass=True)
    assert view["state"] in (ask_proposals.FAILED, ask_proposals.STALE), view
    key, _b = _key(pid)
    assert (await runtime.probe(key))["containment"] == "gone"
    assert not (await runtime.snapshot(key))["bypass"]
    assert _users(project) == []


@pytest.mark.anyio
async def test_a_withdrawal_after_the_switch_admission_runs_no_brief(project, monkeypatch):
    """Hermes on #2357: authority is withdrawn AFTER the switch's admission, while the switch
    runs. The switch contains the first worker and launches nothing; the brief's own fenced
    admission then refuses, so no worker is relaunched, nothing reaches the agent, and what was
    created is contained."""
    prefs.set_agent_defaults({"bypass": True})
    _cid, pid = _card(project, bypass_default=True)
    real = runtime.enable_bypass

    async def withdraw_during_switch(*a, **kw):
        prefs.set_orchestrator({"enabled": False, "autonomy": "suggest"})
        return await real(*a, **kw)

    monkeypatch.setattr(runtime, "enable_bypass", withdraw_during_switch)
    view = await _tap(pid, bypass=True)
    assert view["state"] in (ask_proposals.FAILED, ask_proposals.STALE), view
    key, _b = _key(pid)
    assert (await runtime.probe(key))["containment"] == "gone"
    assert _users(project) == []


@pytest.mark.parametrize(
    "hook",
    # After the brief's submit admission, at each step of its relaunch up to the last one before
    # the generation's first durable write (Hermes on #2357: the entry, then after a released
    # comparison at the session slot).
    ["_relaunch_sync_admitted", "_session_slot", "_launch_generation"],
)
@pytest.mark.anyio
async def test_a_withdrawal_after_the_brief_admission_launches_no_worker(
    project, monkeypatch, hook
):
    """Hermes on #2357: authority is withdrawn AFTER the brief's submit was admitted, while its
    relaunch into the switched session runs. The relaunch holds the submit's tokens from the
    generation's first durable write through its spawn, so no generation is added, nothing
    reaches the agent, and what was created is contained."""
    from agent_sessions import native_runtime, native_state

    prefs.set_agent_defaults({"bypass": True})
    _cid, pid = _card(project, bypass_default=True)
    key, _b = _key(pid)
    session_id = key.split(":", 1)[1]
    real = getattr(native_runtime, hook)
    seen = {}

    def withdraw_then_continue(*a, **kw):
        if "generations" not in seen and native_state.read_session(session_id):
            # Only the brief's relaunch: the create's own first launch runs before the switch.
            record = native_state.read_session(session_id)
            if record.get("permission_change", {}).get("state") == "enabled":
                seen["generations"] = record.get("generations", 0)
                prefs.set_orchestrator({"enabled": False, "autonomy": "suggest"})
        return real(*a, **kw)

    monkeypatch.setattr(native_runtime, hook, withdraw_then_continue)
    view = await _tap(pid, bypass=True)
    assert "generations" in seen, "the withdrawal ran inside the brief's relaunch"
    assert view["state"] in (ask_proposals.FAILED, ask_proposals.STALE), view
    after = native_state.read_session(session_id).get("generations", 0)
    assert after == seen["generations"], "no worker was launched after the withdrawal"
    assert (await runtime.probe(key))["containment"] == "gone"
    assert _users(project) == []


@pytest.mark.anyio
async def test_a_withdrawal_during_the_relaunch_spawn_waits_for_it(project, monkeypatch):
    """The other side of the ordering: a withdrawal that arrives while the brief's relaunch is
    spawning cannot commit until the spawn returned (the tokens are held through it). The turn
    was admitted first; the worker then refuses it at its own claim, so nothing reaches the
    agent either way."""
    import threading

    from agent_sessions import native_authority, native_runtime, native_state

    prefs.set_agent_defaults({"bypass": True})
    _cid, pid = _card(project, bypass_default=True)
    key, _b = _key(pid)
    session_id = key.split(":", 1)[1]
    real_launch = native_runtime.HOST.launch
    seen: dict = {}

    def launch_while_withdrawing(argv):
        record = native_state.read_session(session_id) or {}
        if "bumped" in seen or record.get("permission_change", {}).get("state") != "enabled":
            return real_launch(argv)
        done = threading.Event()

        def withdraw():
            native_authority.bump_session(session_id)
            done.set()

        seen["done"] = done
        seen["bumped"] = threading.Thread(target=withdraw)
        seen["bumped"].start()
        seen["waited"] = not done.wait(0.3)  # still blocked on the held tokens
        out = real_launch(argv)
        seen["inside"] = done.is_set()  # the spawn returned, the block has not ended yet
        return out

    monkeypatch.setattr(native_runtime.HOST, "launch", launch_while_withdrawing)
    view = await _tap(pid, bypass=True)
    seen["bumped"].join(5)
    assert seen["waited"] is True and seen["inside"] is False, "it could not commit inside"
    assert seen["done"].is_set(), "it committed once the spawn returned"
    assert view["state"] != ask_proposals.STARTED, view
    assert _users(project) == []
    with contextlib.suppress(Exception):
        await runtime.stop(key)


@pytest.mark.anyio
async def test_no_guard_attended_or_not_can_make_a_skip_creation(project):
    """#1897 holds for every guard: an HQ card's bypass is the operator's switch AFTER a guarded
    create, never a skip creation — the runtime refuses one even under an attended guard."""
    from agent_sessions import native_runtime
    from agent_sessions.structured_types import ExecutionGuard

    for attended in (False, True):
        guard = ExecutionGuard(
            "ask:p", lambda: contextlib.nullcontext(), admit=lambda: {}, attended=attended
        )
        with pytest.raises(native_runtime.NativeError) as e:
            await native_runtime.create(
                CLAUDE,
                str(project),
                session_id=str(uuid.uuid4()),
                bypass=True,
                execution_admission=guard,
            )
        assert e.value.status == 422
    assert _users(project) == []


@pytest.mark.anyio
async def test_scope_withdrawn_inside_the_fence_refuses_the_first_turn_and_stops_the_worker(
    project, monkeypatch
):
    """Session assistance is turned off AFTER the create and before the first turn's admission:
    the turn is refused inside the write fence, nothing reaches the agent, the worker is stopped
    and the card is out of date."""
    _cid, pid = _card(project)
    real_create = runtime.create_session

    async def create_then_withdraw(*a, **kw):
        out = await real_create(*a, **kw)
        prefs.set_orchestrator({"enabled": False, "autonomy": "suggest"})
        return out

    monkeypatch.setattr(runtime, "create_session", create_then_withdraw)
    view = await _tap(pid)
    assert view["state"] in (ask_proposals.FAILED, ask_proposals.STALE), view
    assert _users(project) == []
    key, _b = _key(pid)
    assert (await runtime.probe(key))["containment"] == "gone"


@pytest.mark.anyio
async def test_scope_withdrawn_before_the_create_is_stale_and_starts_nothing(project, monkeypatch):
    _cid, pid = _card(project)
    real_create = runtime.create_session

    async def withdraw_then_create(*a, **kw):
        prefs.set_orchestrator({"enabled": False, "autonomy": "suggest"})
        return await real_create(*a, **kw)

    monkeypatch.setattr(runtime, "create_session", withdraw_then_create)
    view = await _tap(pid)
    assert view["state"] == ask_proposals.STALE, view
    assert view["session_key"] == ""
    assert _users(project) == []
    key, _b = _key(pid)
    assert not await runtime.may_have_worker(key)


@pytest.mark.anyio
async def test_a_first_turn_the_agent_did_not_take_fails_and_its_worker_is_stopped(
    project, monkeypatch
):
    _cid, pid = _card(project)

    async def not_taken(*_a, **_kw):
        return {"handoff": "not_sent"}

    monkeypatch.setattr(runtime, "submit_turn", not_taken)
    view = await _tap(pid)
    assert view["state"] == ask_proposals.FAILED, view
    assert "nothing is running" in view["reason"]
    key, _b = _key(pid)
    assert (await runtime.probe(key))["containment"] == "gone"


@pytest.mark.anyio
async def test_an_uncertain_first_turn_is_unconfirmed_and_never_resent(project, monkeypatch):
    _cid, pid = _card(project)
    sent = []

    async def lost(*_a, **_kw):
        sent.append(1)
        raise runtime.StructuredError(504, "the answer was lost")

    monkeypatch.setattr(runtime, "submit_turn", lost)
    view = await _tap(pid)
    assert view["state"] == ask_proposals.UNCONFIRMED, view
    key, _b = _key(pid)
    assert view["session_key"] == key
    await _tap(pid)  # a second tap on the settled card sends nothing
    assert sent == [1]
    await runtime.stop(key)


@pytest.mark.anyio
async def test_a_card_whose_agent_changed_kind_is_stale(project):
    """The kind shown is the kind started: an entry pinned `api` for an engine that is not an
    API client (or the reverse) never launches the other kind."""
    _cid, pid = _card(project, runtime_kind=None)  # a terminal entry naming an API client
    view = await _tap(pid)
    assert view["state"] == ask_proposals.STALE, view
    assert "kind of session" in view["reason"]
    key, _b = _key(pid)
    assert not await runtime.may_have_worker(key)


def test_a_terminal_entry_views_as_pty_and_an_older_entry_reads_unchanged(project):
    cid, pid = _card(project, runtime_kind=None)
    row = ask_proposals._row(cid, pid)
    assert "runtime" not in row
    assert ask_proposals.view(row)["runtime"] == "pty"


def test_an_unknown_runtime_is_refused_by_the_store(project):
    with pytest.raises(ValueError, match="runtime"):
        _card(project, runtime_kind="terminal")


@pytest.mark.anyio
async def test_the_launch_runs_inside_the_event_loop_without_blocking_the_decide(project):
    """The decide answers 202 at once; the launch is the server task (as for a terminal card)."""
    _cid, pid = _card(project)
    status, view = await ask_proposals.decide(pid, "start", bypass=False, registry=None)
    assert status == 202 and view["state"] == ask_proposals.DECIDING
    await asyncio.wait_for(ask_proposals._TASKS[pid], timeout=60)
    key, _b = _key(pid)
    await runtime.stop(key)
