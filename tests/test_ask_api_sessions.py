# ruff: noqa: F811 — `host` / `project` are the shared native fixtures imported below
"""HQ starts an API session from a card (#2014).

The real path end to end: `ask_proposals.decide` → `_launch_api` → `mission_structured.hq_start` →
the structured facade → the real native worker over the fake native child (`host`), with the
conversation store under `tmp_path`. A card pins its kind (`runtime: "api"`) when it is shown, and
the tap starts exactly that: one session and one ATTENDED, fenced first turn, ids derived from the
proposal, never skipping permission prompts.
"""

from __future__ import annotations

import asyncio
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


def _card(project, *, engine=CLAUDE, runtime_kind="api") -> tuple[str, str]:
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
        "bypass_default": False,
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
async def test_bypass_on_an_api_card_is_refused_and_nothing_starts(project):
    _cid, pid = _card(project)
    with pytest.raises(ask_proposals.ProposalError) as e:
        await ask_proposals.decide(pid, "start", bypass=True, registry=None)
    assert e.value.status == 422
    key, _b = _key(pid)
    assert not await runtime.may_have_worker(key)
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
