"""Ask proposes a session to start; the operator's tap starts it (#1443 Phase 3a).

* **propose_session** picks a project and an agent BY INDEX into lists this turn's own tools
  returned, and writes a card plus a sidecar entry (``ask_proposals``). Nothing starts.
* **The tap** (``POST /api/ask/proposals/{id}/decide``) launches through
  ``headless_dispatch.dispatch`` with EXACTLY the bypass value the tap carries (absent → ``False``),
  once: a repeat returns the entry, never a second launch.
* **Attended bypass:** the card's starting value is ``agent_defaults.bypass``, never the unattended
  grant; a change to it between showing and tapping — before, or inside the launch fence — asks
  again and spawns nothing.
* **Stale** (scope off, project gone or moved, folder out of scope, agent not startable) → out of
  date, nothing started. **Crash** in ``deciding`` → ``interrupted``, never replayed. An uncertain
  launch is ``unconfirmed``, never ``started``, never retried.

The dispatcher is a stand-in that takes the REAL launch fence around ``authorize`` — the boundary
this module relies on — and reports the facts a test chooses.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from agent_sessions import (
    ask_orchestrator,
    ask_proposals,
    ask_store,
    automation_runner,
    headless_dispatch,
    mission_plan,
    prefs,
    session_input,
    template_secrets,
)
from agent_sessions.main import create_app
from test_ask_orchestrator import (  # noqa: F401 — fixtures are used by name
    CRED,
    App,
    _login,
    ai,
    answer,
    app,
    call,
    calls,
)

BRIEF = "Rebase the api branch onto main and run the test suite."
#: The real resolvers, kept before any fixture patches them: a race on the REAL project store's
#: flock needs the real reads.
REAL_RESOLVE = mission_plan.resolve_chosen_project
REAL_OPTIONS = mission_plan.project_options


def _ok(out):
    out.launched = out.started = out.briefed = True


def _unbound(out):
    out.launched = out.started = out.briefed = True
    out.bound = False
    out.reason = "the session it became could not be identified"


def _stopped(out):
    out.launched = True
    out.teardown = "stopped"
    out.reason = "no start evidence within 90s"


def _leaked(out):
    out.launched = True
    out.teardown = "leaked"
    out.reason = "no start evidence within 90s (and a process is STILL RUNNING)"


def _leaked_before_launched(out):
    """Hermes on #1491, 3: spawned, the socket never answered (`launched` stays false), and the
    teardown left a live process."""
    out.teardown = "leaked"
    out.reason = "dtach never accepted (and a process is STILL RUNNING)"


def _spared(out):
    out.teardown = "spared"
    out.reason = "the session was adopted while it was being stopped"


@pytest.fixture
def world(monkeypatch, tmp_path):
    """One project folder inside $HOME, Claude startable, session assistance `suggest`, the
    bypass default OFF — and a dispatcher that records what it was asked to launch."""
    home = tmp_path / "home"
    proj = home / "work" / "api"
    proj.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})
    prefs.set_agent_defaults({"bypass": False})
    state: dict = {
        "projects": [{"id": "p1", "name": "api", "cwd": str(proj)}],
        "agents": [{"index": 0, "id": "claude", "name": "Claude Code", "can_start": True}],
        "engine": (True, ""),
        "launches": [],
        "spawned": [],
        "result": _ok,
        "gate": None,
        "on_fence": None,
        "after_check": None,
        "probe": False,
        "kwargs": [],
        "proj": proj,
    }

    def resolve(pid, *, index=None):
        for p in state["projects"]:
            if p["id"] == pid:
                return dict(p), ""
        return None, "the chosen project no longer exists"

    monkeypatch.setattr(mission_plan, "project_options", lambda: state["projects"])
    monkeypatch.setattr(mission_plan, "resolve_chosen_project", resolve)
    monkeypatch.setattr(ask_orchestrator, "_agent_rows", lambda: state["agents"])
    monkeypatch.setattr(automation_runner, "engine_state", lambda e: state["engine"])

    async def dispatch(
        *,
        engine,
        cwd,
        brief,
        registry,
        bypass=False,
        on_key=None,
        authorize=None,
        spawn_fence=None,
        guard_secrets=False,
    ):
        state["launches"].append({"engine": engine, "cwd": cwd, "brief": brief, "bypass": bypass})
        state["kwargs"].append({"guard_secrets": guard_secrets})
        native = str(uuid.uuid4())
        out = headless_dispatch.Dispatch(
            key=f"{engine}:{native}", engine=engine, native=native, cwd=cwd
        )
        if on_key is not None:
            try:
                on_key(out.key)
            except Exception as e:  # noqa: BLE001 — as the real dispatcher reports it
                out.reason = f"the dispatch could not be recorded ({type(e).__name__})"
                return out
        if state["gate"] is not None:
            await state["gate"].wait()

        def fenced():
            # As `headless_dispatch._fenced_spawn` does: THE REAL launch fence, then the caller's
            # `spawn_fence` held across its `authorize` AND the spawn (recorded here).
            if state["probe"] and authorize is not None:
                # A late-id engine's preflight probe: `authorize` under the launch fence alone.
                with session_input.launch_fence(timeout=5) as epoch:
                    why = authorize(epoch)
                    if why:
                        return why
            with session_input.launch_fence(timeout=5) as epoch:
                if state["on_fence"] is not None:
                    state["on_fence"]()
                with spawn_fence() if spawn_fence is not None else contextlib.nullcontext():
                    why = authorize(epoch) if authorize is not None else None
                    if why:
                        return why
                    if state["after_check"] is not None:
                        state["after_check"]()
                    state["spawned"].append(out.key)
                    return None

        why = await asyncio.to_thread(fenced)
        if why:
            out.reason = why
            return out
        state["result"](out)
        return out

    monkeypatch.setattr(headless_dispatch, "dispatch", dispatch)
    return state


class Live(App):
    """A context-managed client: one event loop for the whole test, so the launch task outlives
    the POST that started it — as it does in the running app."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.app = create_app(cfg)
        self._cm = TestClient(self.app, base_url="https://testserver")
        self.c = self._cm.__enter__()
        self.hdr = _login(self.c, cfg)

    def close(self):
        self._cm.__exit__(None, None, None)

    def decide(self, pid: str, body: dict | None = None):
        return self.c.post(
            f"/api/ask/proposals/{pid}/decide",
            json={"decision": "start"} if body is None else body,
            headers=self.hdr,
        )

    def card(self, cid: str) -> dict:
        (card,) = [p for t in self.get(cid)["turns"] for p in t["proposals"]]
        return card

    def settled(self, cid: str, timeout: float = 10.0) -> dict:
        end = time.monotonic() + timeout
        while True:
            card = self.card(cid)
            if card["state"] != "deciding" or time.monotonic() > end:
                return card
            time.sleep(0.05)


@pytest.fixture
def live(auth_cfg):
    a = Live(auth_cfg)
    try:
        yield a
    finally:
        a.close()


def _session_turn(ai, *, project=0, agent=0, brief=BRIEF):
    return ai(
        calls(("list_projects", {}), ("list_agents", {})),
        call(
            "propose_session",
            {"project_index": project, "engine_index": agent, "brief": brief},
            cid="s",
        ),
        answer("I proposed the session; start it from the card."),
    )


def _propose(a, ai, **kw) -> tuple[str, dict]:
    script = _session_turn(ai, **kw)
    cid = a.new()
    turn = a.ask(cid, "start a claude session on api to rebase").json()["turn"]
    return cid, {"turn": turn, "script": script}


def _result(script, n=2) -> dict:
    return json.loads(script.tool_results(n)[-1]["content"])


# ---- proposing ---------------------------------------------------------------------------------


def test_a_session_proposal_is_a_card_and_a_pending_entry_and_nothing_starts(app, ai, world):
    cid, got = _propose(app, ai)
    assert _result(got["script"])["proposed"] is True
    (card,) = got["turn"]["proposals"]
    assert card["kind"] == "session" and card["state"] == "pending"
    assert card["title"] == "api" and card["agent"] == "Claude Code" and card["text"] == BRIEF
    assert card["can_start"] and card["can_dismiss"] and card["bypass_default"] is False
    (row,) = ask_store.read_proposals(cid)
    assert row["id"] == card["action_id"] and row["state"] == "pending"
    assert row["engine"] == "claude" and row["project_id"] == "p1"
    assert row["cwd"] == str(world["proj"]) and row["brief"] == BRIEF
    assert world["launches"] == []
    # The model never saw a path: list_projects and list_agents answer names by index.
    listed = [json.loads(m["content"]) for m in got["script"].tool_results(1)]
    assert listed[0] == {"projects": [{"index": 0, "name": "api"}]}
    assert listed[1] == {"agents": [{"index": 0, "name": "Claude Code", "can_start": True}]}


def test_the_starting_bypass_value_is_the_new_session_default_not_the_unattended_grant(
    app, ai, world, monkeypatch
):
    prefs.set_agent_defaults({"bypass": True})
    # The unattended grant says no; the card's starting value is the attended default regardless.
    monkeypatch.setattr(prefs, "unattended_bypass_granted", lambda path=None: False)
    _cid, got = _propose(app, ai)
    assert got["turn"]["proposals"][0]["bypass_default"] is True


@pytest.mark.parametrize(
    "args, code",
    [
        ({"project_index": 3, "engine_index": 0, "brief": BRIEF}, "bad_index"),
        ({"project_index": 0, "engine_index": 7, "brief": BRIEF}, "bad_index"),
        ({"project_index": "0", "engine_index": 0, "brief": BRIEF}, "bad_index"),
        ({"project_index": False, "engine_index": 0, "brief": BRIEF}, "bad_index"),
    ],
    ids=["project", "agent", "string-index", "bool-index"],
)
def test_a_forged_index_is_refused_and_nothing_is_written(app, ai, world, args, code):
    script = ai(
        calls(("list_projects", {}), ("list_agents", {})),
        call("propose_session", args, cid="s"),
        answer("no"),
    )
    cid = app.new()
    turn = app.ask(cid, "start one").json()["turn"]
    assert "must be an index" in _result(script)["error"]
    assert turn["proposals"] == [] and ask_store.read_proposals(cid) == []


def test_an_index_from_a_list_this_turn_never_fetched_is_refused(app, ai, world):
    script = ai(
        call(
            "propose_session",
            {"project_index": 0, "engine_index": 0, "brief": BRIEF},
            cid="s",
        ),
        answer("no"),
    )
    cid = app.new()
    app.ask(cid, "start one")
    assert "call list_projects in this turn first" in _result(script, 1)["error"]
    assert ask_store.read_proposals(cid) == []


@pytest.mark.parametrize(
    "setup, needle",
    [
        (lambda w: prefs.set_orchestrator({"enabled": False}), "session assistance"),
        (
            lambda w: prefs.set_orchestrator({"enabled": True, "autonomy": "off"}),
            "session assistance",
        ),
        (
            lambda w: w.update(engine=(False, "it cannot be started unattended")),
            "cannot be started",
        ),
        (lambda w: prefs.set_folder_exclusions([str(w["proj"])]), "outside your project folders"),
    ],
    ids=["disabled", "observe-only", "agent", "excluded-folder"],
)
def test_a_refused_proposal_writes_nothing(app, ai, world, setup, needle):
    setup(world)
    script = _session_turn(ai)
    cid = app.new()
    turn = app.ask(cid, "start one").json()["turn"]
    assert needle in _result(script)["error"]
    assert turn["proposals"] == [] and ask_store.read_proposals(cid) == []


def test_a_brief_carrying_a_secret_or_a_credential_is_refused(app, ai, world):
    template_secrets.register_typed(["hunter2-PLANTED-template-value-9f3k"])
    for brief in (f"use {CRED} to deploy", "log in with hunter2-PLANTED-template-value-9f3k"):
        script = _session_turn(ai, brief=brief)
        cid = app.new()
        turn = app.ask(cid, "start one").json()["turn"]
        assert "secret or looks like a credential" in _result(script)["error"]
        assert turn["proposals"] == [] and ask_store.read_proposals(cid) == []


def test_the_shown_brief_is_the_typed_brief(live, ai, world):
    cid, _ = _propose(live, ai, brief="Rebase\x1b[201~ then test\x07  ")
    card = live.card(cid)
    assert card["text"] == "Rebase[201~ then test"
    assert live.decide(card["action_id"]).status_code == 202
    live.settled(cid)
    assert world["launches"][0]["brief"] == card["text"]


# ---- deciding ----------------------------------------------------------------------------------


def test_start_launches_once_with_the_value_the_tap_carries(live, ai, world):
    cid, _ = _propose(live, ai)
    pid = live.card(cid)["action_id"]
    r = live.decide(pid, {"decision": "start", "bypass": True})
    assert r.status_code == 202 and r.json()["proposal"]["state"] == "deciding"
    card = live.settled(cid)
    assert card["state"] == "started" and card["session_key"].startswith("claude:")
    (launch,) = world["launches"]
    assert launch == {
        "engine": "claude",
        "cwd": os.path.realpath(world["proj"]),
        "brief": BRIEF,
        "bypass": True,
    }
    assert card["can_start"] is False and card["can_dismiss"] is False


def test_an_absent_bypass_launches_without_it_whatever_the_default(live, ai, world):
    prefs.set_agent_defaults({"bypass": True})
    cid, _ = _propose(live, ai)
    assert live.decide(live.card(cid)["action_id"], {"decision": "start"}).status_code == 202
    live.settled(cid)
    assert world["launches"][0]["bypass"] is False


@pytest.mark.parametrize("bypass", ["true", 1, None])
def test_a_bypass_that_is_not_a_boolean_is_refused(live, ai, world, bypass):
    cid, _ = _propose(live, ai)
    r = live.decide(live.card(cid)["action_id"], {"decision": "start", "bypass": bypass})
    assert r.status_code == 422 and world["launches"] == []


def test_a_double_tap_is_one_launch(live, ai, world):
    cid, _ = _propose(live, ai)
    pid = live.card(cid)["action_id"]
    gate = asyncio.Event()
    world["gate"] = gate
    first = live.decide(pid)
    second = live.decide(pid)
    assert first.status_code == 202
    assert second.status_code == 200 and second.json()["proposal"]["state"] == "deciding"
    live.c.portal.call(gate.set)
    assert live.settled(cid)["state"] == "started"
    third = live.decide(pid)
    assert third.status_code == 200 and third.json()["proposal"]["state"] == "started"
    assert len(world["launches"]) == 1


def test_dismiss_starts_nothing_and_settles(live, ai, world):
    cid, _ = _propose(live, ai)
    pid = live.card(cid)["action_id"]
    r = live.decide(pid, {"decision": "dismiss"})
    assert r.status_code == 200 and r.json()["proposal"]["state"] == "dismissed"
    assert live.decide(pid).json()["proposal"]["state"] == "dismissed"
    assert world["launches"] == []


def test_the_client_names_the_proposal_never_what_it_starts(live, ai, world):
    cid, _ = _propose(live, ai)
    pid = live.card(cid)["action_id"]
    r = live.decide(
        pid, {"decision": "start", "engine": "codex", "cwd": "/etc", "brief": "rm -rf ~"}
    )
    assert r.status_code == 202
    live.settled(cid)
    (launch,) = world["launches"]
    assert launch["engine"] == "claude" and launch["brief"] == BRIEF
    assert launch["cwd"] == os.path.realpath(world["proj"])


def test_unknown_and_malformed_proposals_are_404(live, ai, world):
    assert live.decide("0" * 32).status_code == 404
    assert live.decide("not-an-id").status_code == 404


def test_the_decide_route_needs_csrf(live, ai, world):
    cid, _ = _propose(live, ai)
    r = live.c.post(
        f"/api/ask/proposals/{live.card(cid)['action_id']}/decide",
        json={"decision": "start"},
        headers={"Origin": live.cfg.origin},
    )
    assert r.status_code == 403 and world["launches"] == []


# ---- the attended bypass rule ------------------------------------------------------------------


def test_a_changed_default_before_the_tap_asks_again_and_spawns_nothing(live, ai, world):
    cid, _ = _propose(live, ai)
    pid = live.card(cid)["action_id"]
    prefs.set_agent_defaults({"bypass": True})
    r = live.decide(pid, {"decision": "start", "bypass": False})
    assert r.status_code == 409
    card = r.json()["proposal"]
    assert card["state"] == "pending" and card["notice"] == "default_changed"
    assert card["bypass_default"] is True and world["launches"] == []
    # Re-confirmed against the new starting value, it starts.
    assert live.decide(pid, {"decision": "start", "bypass": True}).status_code == 202
    assert live.settled(cid)["state"] == "started"
    assert world["launches"][0]["bypass"] is True


def test_a_default_changed_inside_the_fence_asks_again_and_spawns_nothing(live, ai, world):
    """The change commits under the launch fence (`set_agent_defaults` → policy_transaction), so
    it lands before `authorize` reads it — simulated here as the last thing before that read."""
    cid, _ = _propose(live, ai)
    pid = live.card(cid)["action_id"]
    real = ask_proposals.bypass_default
    flips = {"n": 0}

    def flipped():
        flips["n"] += 1
        return real() if flips["n"] == 1 else not real()

    ask_proposals.bypass_default, saved = flipped, real
    try:
        assert live.decide(pid).status_code == 202
        card = live.settled(cid)
    finally:
        ask_proposals.bypass_default = saved
    assert card["state"] == "pending" and card["notice"] == "default_changed"
    assert world["spawned"] == [] and card["session_key"] == ""


def test_a_concurrent_default_change_is_ordered_by_the_real_fence(live, ai, world):
    """A real `set_agent_defaults` racing the launch: it either commits first (the card asks
    again, nothing spawned) or waits for the fence (the launch used the shown value)."""
    cid, _ = _propose(live, ai)
    pid = live.card(cid)["action_id"]
    import threading

    done = threading.Event()

    def change():
        prefs.set_agent_defaults({"bypass": True})
        done.set()

    world["on_fence"] = lambda: threading.Thread(target=change).start()
    assert live.decide(pid).status_code == 202
    card = live.settled(cid)
    assert done.wait(5)
    # The change waited for the fence the launch held: the launch went first, with the shown value.
    assert card["state"] == "started" and world["launches"][0]["bypass"] is False


# ---- stale -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setup, needle",
    [
        (lambda w: prefs.set_orchestrator({"enabled": False}), "session assistance"),
        (lambda w: w["projects"].clear(), "no longer exists"),
        (lambda w: w["projects"][0].update(cwd="/elsewhere"), "folder changed"),
        (lambda w: w.update(engine=(False, "claude is not installed")), "not installed"),
        (lambda w: prefs.set_folder_exclusions([str(w["proj"])]), "outside your project folders"),
    ],
    ids=["scope-off", "project-gone", "project-moved", "agent-gone", "folder-excluded"],
)
def test_a_stale_proposal_is_out_of_date_and_starts_nothing(live, ai, world, setup, needle):
    cid, _ = _propose(live, ai)
    pid = live.card(cid)["action_id"]
    setup(world)
    r = live.decide(pid)
    assert r.status_code == 200
    card = r.json()["proposal"]
    assert card["state"] == "stale" and needle in card["reason"]
    assert card["can_start"] is False and world["launches"] == []
    assert live.decide(pid).json()["proposal"]["state"] == "stale"


def test_scope_withdrawn_inside_the_fence_is_stale_and_spawns_nothing(live, ai, world, monkeypatch):
    """A withdrawal commits under the same fence (`policy_transaction`); simulated as landing
    just before `authorize` reads the policy."""
    cid, _ = _propose(live, ai)
    world["on_fence"] = lambda: monkeypatch.setattr(
        prefs, "get_automation_policy", lambda scope, path=None: {"enabled": False}
    )
    assert live.decide(live.card(cid)["action_id"]).status_code == 202
    card = live.settled(cid)
    assert card["state"] == "stale" and "session assistance" in card["reason"]
    assert world["spawned"] == []


def test_a_symlinked_folder_repointed_after_the_tap_is_refused(live, ai, world, tmp_path):
    link = world["proj"].parent / "link"
    link.symlink_to(world["proj"])
    world["projects"][0]["cwd"] = str(link)
    cid, _ = _propose(live, ai)
    other = world["proj"].parent / "other"
    other.mkdir()

    def repoint():
        link.unlink()
        link.symlink_to(other)

    world["on_fence"] = repoint
    assert live.decide(live.card(cid)["action_id"]).status_code == 202
    card = live.settled(cid)
    assert card["state"] == "stale" and "moved" in card["reason"]
    assert world["spawned"] == []


# ---- outcomes ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "result, state, key",
    [
        (_ok, "started", True),
        (_unbound, "unconfirmed", True),
        (_leaked, "unconfirmed", True),
        (_leaked_before_launched, "unconfirmed", True),
        (_spared, "unconfirmed", True),
        (_stopped, "failed", False),
    ],
    ids=["started", "not-bound", "leaked", "leaked-unlaunched", "spared", "stopped"],
)
def test_outcomes_say_only_what_the_dispatch_proved(live, ai, world, result, state, key):
    world["result"] = result
    cid, _ = _propose(live, ai)
    pid = live.card(cid)["action_id"]
    live.decide(pid)
    card = live.settled(cid)
    assert card["state"] == state
    assert bool(card["session_key"]) is key
    if state != "started":
        assert card["reason"]
    # Never retried: the same tap returns the outcome.
    assert live.decide(pid).json()["proposal"]["state"] == state
    assert len(world["launches"]) == 1


def test_a_dispatch_that_could_not_be_attempted_is_failed_with_its_reason(
    live, ai, world, monkeypatch
):
    async def refuse(**kw):
        raise headless_dispatch.DispatchError("claude cannot be dispatched unattended")

    monkeypatch.setattr(headless_dispatch, "dispatch", refuse)
    cid, _ = _propose(live, ai)
    live.decide(live.card(cid)["action_id"])
    card = live.settled(cid)
    assert card["state"] == "failed" and "cannot be dispatched" in card["reason"]
    assert "nothing was started" in card["reason"]


def test_the_session_key_is_recorded_before_the_spawn(live, ai, world):
    cid, _ = _propose(live, ai)
    pid = live.card(cid)["action_id"]
    gate = asyncio.Event()
    world["gate"] = gate
    live.decide(pid)
    (row,) = ask_store.read_proposals(cid)
    assert row["state"] == "deciding" and row["session_key"].startswith("claude:")
    assert world["spawned"] == []
    live.c.portal.call(gate.set)
    live.settled(cid)


# ---- crash and retention -----------------------------------------------------------------------


def test_a_crash_while_deciding_reads_interrupted_and_is_never_replayed(app, ai, world):
    cid, got = _propose(app, ai)
    pid = got["turn"]["proposals"][0]["action_id"]
    # The server died mid-launch: `deciding` on disk, nobody holding the decide lock.
    with ask_store.proposals_lock(cid):
        rows = ask_store.read_proposals(cid)
        rows[0].update(state="deciding", bypass=False, session_key="claude:" + "1" * 36)
        ask_store.write_proposals(cid, rows)
    (card,) = [p for t in app.get(cid)["turns"] for p in t["proposals"]]
    assert card["state"] == "interrupted" and "not retried" in card["reason"]
    assert card["session_key"] == "" and card["can_start"] is False
    assert world["launches"] == []
    status, view = asyncio.run(ask_proposals.decide(pid, "start", bypass=False, registry=object()))
    assert status == 200 and view["state"] == "interrupted" and world["launches"] == []


def test_a_launch_in_flight_is_not_mistaken_for_a_crash(live, ai, world):
    cid, _ = _propose(live, ai)
    gate = asyncio.Event()
    world["gate"] = gate
    live.decide(live.card(cid)["action_id"])
    assert live.card(cid)["state"] == "deciding"  # the read did not settle it as interrupted
    live.c.portal.call(gate.set)
    assert live.settled(cid)["state"] == "started"


def test_a_launch_pins_its_conversation_past_retention(live, ai, world):
    cid, _ = _propose(live, ai)
    gate = asyncio.Event()
    world["gate"] = gate
    live.decide(live.card(cid)["action_id"])
    t = time.time() - (ask_store.RETENTION_DAYS + 1) * 86400
    os.utime(ask_store.root() / f"{cid}.jsonl", (t, t))
    assert ask_store.sweep() == []
    live.c.portal.call(gate.set)
    assert live.settled(cid)["state"] == "started"
    os.utime(ask_store.root() / f"{cid}.jsonl", (t, t))
    assert ask_store.sweep() == [cid]
    assert not ask_store._proposals_path(cid).exists()


def test_a_tap_that_starts_deciding_while_retention_waits_is_seen(app, ai, world, monkeypatch):
    """Retention's last look happens under the sidecar lock: a tap that wrote `deciding` while the
    sweep waited for the turn lock keeps the conversation."""
    cid, _ = _propose(app, ai)
    t = time.time() - (ask_store.RETENTION_DAYS + 1) * 86400
    os.utime(ask_store.root() / f"{cid}.jsonl", (t, t))
    real = ask_store.turn_lock

    import contextlib

    @contextlib.contextmanager
    def a_tap_lands_first(c):
        with ask_store.proposals_lock(c):
            rows = ask_store.read_proposals(c)
            rows[0]["state"] = "deciding"
            ask_store.write_proposals(c, rows)
        with real(c) as free:
            yield free

    monkeypatch.setattr(ask_store, "turn_lock", a_tap_lands_first)
    assert ask_store.sweep() == []
    assert ask_store._proposals_path(cid).exists()


def test_a_secret_registered_after_the_card_is_shown_refuses_the_start(live, ai, world):
    """Hermes on #1491, 1(a): registered before the tap — the start is refused, never launched
    with a scrubbed brief the operator did not approve, and the value is never written."""
    secret = "purple-monkey-dishwasher-tag"
    cid, _ = _propose(live, ai, brief=f"Deploy with {secret} as the tag")
    template_secrets.register_typed([secret])
    r = live.decide(live.card(cid)["action_id"])
    card = r.json()["proposal"]
    assert card["state"] == "stale" and "secret" in card["reason"]
    assert world["launches"] == []
    assert secret not in ask_store._proposals_path(cid).read_text()


def test_a_secret_registered_after_deciding_is_written_refuses_the_spawn(live, ai, world):
    """1(b): registered after `deciding` landed and before the spawn — checked inside the launch
    fence against the brief about to be typed: nothing spawns, and the card is out of date."""
    secret = "purple-monkey-dishwasher-tag"
    cid, _ = _propose(live, ai, brief=f"Deploy with {secret} as the tag")
    world["on_fence"] = lambda: template_secrets.register_typed([secret])
    assert live.decide(live.card(cid)["action_id"]).status_code == 202
    card = live.settled(cid)
    assert card["state"] == "stale" and "secret" in card["reason"]
    assert world["spawned"] == []


def test_a_project_archived_during_the_launch_refuses_the_spawn(live, ai, world):
    """Hermes on #1491, 2: the project is read again inside the launch fence."""
    cid, _ = _propose(live, ai)
    world["on_fence"] = lambda: world["projects"].clear()
    assert live.decide(live.card(cid)["action_id"]).status_code == 202
    card = live.settled(cid)
    assert card["state"] == "stale" and "no longer exists" in card["reason"]
    assert world["spawned"] == []


def test_a_project_folder_changed_during_the_launch_refuses_the_spawn(live, ai, world):
    cid, _ = _propose(live, ai)
    other = world["proj"].parent / "elsewhere"
    other.mkdir()
    world["on_fence"] = lambda: world["projects"][0].update(cwd=str(other))
    assert live.decide(live.card(cid)["action_id"]).status_code == 202
    card = live.settled(cid)
    assert card["state"] == "stale" and "folder changed" in card["reason"]
    assert world["spawned"] == []


def test_a_symlink_repointed_before_the_tap_is_refused(live, ai, world):
    """Hermes on #1491, 4: the folder's realpath is stored when the card is shown; a link
    repointed before the tap is refused, never taken as the new baseline."""
    link = world["proj"].parent / "link"
    link.symlink_to(world["proj"])
    world["projects"][0]["cwd"] = str(link)
    cid, _ = _propose(live, ai)
    other = world["proj"].parent / "other"
    other.mkdir()
    link.unlink()
    link.symlink_to(other)
    r = live.decide(live.card(cid)["action_id"])
    card = r.json()["proposal"]
    assert card["state"] == "stale" and "moved" in card["reason"]
    assert world["launches"] == []


def test_the_sidecar_is_private(app, ai, world):
    import stat

    cid, _ = _propose(app, ai)
    path = ask_store._proposals_path(cid)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(path.parent).st_mode) == 0o700


def test_an_unreadable_sidecar_reads_unknown_and_decides_nothing(live, ai, world):
    cid, _ = _propose(live, ai)
    pid = live.card(cid)["action_id"]
    ask_store._proposals_path(cid).write_text("{not json")
    card = live.card(cid)
    assert card["state"] == "unknown" and card["can_start"] is False
    assert live.decide(pid).status_code == 404
    assert world["launches"] == []


def test_a_card_whose_entry_never_landed_offers_nothing(app, ai, world, monkeypatch):
    def broken(cid, entry):
        raise ask_store.StoreRefused("disk full")

    monkeypatch.setattr(ask_proposals, "add", broken)
    script = _session_turn(ai)
    cid = app.new()
    turn = app.ask(cid, "start one").json()["turn"]
    assert "could not be recorded" in _result(script)["error"]
    (card,) = turn["proposals"]
    assert card["state"] == "gone" and card["can_start"] is False


def test_a_secret_registered_before_the_entry_is_written_refuses_it(app, ai, world, monkeypatch):
    """The sidecar write is its own store boundary: a secret registered after the tool's check and
    before the entry lands refuses the entry rather than storing an altered brief to launch."""
    secret = "purple-monkey-dishwasher-tag"
    real = ask_proposals.add

    def late(cid, entry):
        template_secrets.register_typed([secret])
        return real(cid, entry)

    monkeypatch.setattr(ask_proposals, "add", late)
    script = _session_turn(ai, brief=f"Deploy with {secret} as the tag")
    cid = app.new()
    turn = app.ask(cid, "start one").json()["turn"]
    assert "secret or looks like a credential" in _result(script)["error"]
    assert ask_store.read_proposals(cid) == []
    (card,) = turn["proposals"]
    assert card["state"] == "gone" and card["can_start"] is False


# ---- Hermes round 2 on #1491: the check holds until the process exists -------------------------


def test_the_launch_asks_for_the_brief_to_be_checked_again_as_it_is_typed(live, ai, world):
    cid, _ = _propose(live, ai)
    live.decide(live.card(cid)["action_id"])
    live.settled(cid)
    assert world["kwargs"] == [{"guard_secrets": True}]


def test_a_secret_registered_just_after_the_check_waits_for_the_spawn(live, ai, world):
    """Registered between `authorize` and the spawn: the registration fence is still held, so it
    lands only once the process exists — the spawn decision saw the full secret set."""
    secret = "purple-monkey-dishwasher-tag"
    cid, _ = _propose(live, ai, brief=f"Deploy with {secret} as the tag")
    seen: dict = {}

    def register_meanwhile():
        t = threading.Thread(target=template_secrets.register_typed, args=([secret],))
        t.start()
        t.join(0.3)
        seen["registered_before_spawn"] = not t.is_alive()
        seen["thread"] = t

    world["after_check"] = register_meanwhile
    assert live.decide(live.card(cid)["action_id"]).status_code == 202
    live.settled(cid)
    seen["thread"].join(5)
    assert seen["registered_before_spawn"] is False
    assert not seen["thread"].is_alive()  # …and it landed once the fence was released


def _real_project(monkeypatch, world) -> str:
    from agent_sessions import projects

    monkeypatch.setattr(mission_plan, "resolve_chosen_project", REAL_RESOLVE)
    monkeypatch.setattr(mission_plan, "project_options", REAL_OPTIONS)
    p = projects.create("api", folders=[str(world["proj"])], default_folder=str(world["proj"]))
    return p.id


def test_a_project_archived_just_after_the_check_waits_for_the_spawn(live, ai, world, monkeypatch):
    """Archived between `authorize` and the spawn: the project store's flock is held across both,
    so the archive commits only after the process exists — never under a check it already passed."""
    from agent_sessions import projects

    pid = _real_project(monkeypatch, world)
    cid, _ = _propose(live, ai)
    seen: dict = {}

    def archive_meanwhile():
        t = threading.Thread(target=lambda: projects.update(pid, archived=True))
        t.start()
        t.join(0.3)
        seen["archived_before_spawn"] = not t.is_alive()
        seen["thread"] = t

    world["after_check"] = archive_meanwhile
    assert live.decide(live.card(cid)["action_id"]).status_code == 202
    card = live.settled(cid)
    seen["thread"].join(5)
    assert seen["archived_before_spawn"] is False and card["state"] == "started"
    assert projects.load()[pid].archived is True


def test_a_project_store_held_elsewhere_refuses_the_spawn_and_can_be_tapped_again(
    live, ai, world, monkeypatch
):
    """The flock is TRIED inside the launch fence, never waited on (it is ordered outside it): held
    past the bound, the start refuses with nothing spawned, and the card can be tapped again."""
    from agent_sessions import projects

    _real_project(monkeypatch, world)
    monkeypatch.setattr(ask_proposals, "PROJECT_LOCK_WAIT_S", 0.1)
    cid, _ = _propose(live, ai)
    pid = live.card(cid)["action_id"]
    hold, held = threading.Event(), threading.Event()

    def holder():
        with projects.locked_index():
            held.set()
            hold.wait(10)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    try:
        assert live.decide(pid).status_code == 202
        card = live.settled(cid)
    finally:
        hold.set()
        t.join(5)
    assert card["state"] == "pending" and card["notice"] == "busy" and card["can_start"]
    assert world["spawned"] == []
    assert live.decide(pid).status_code == 202
    assert live.settled(cid)["state"] == "started"


def test_a_preflight_probe_reads_the_project_without_the_spawns_hold(live, ai, world):
    """`authorize` also gates a late-id engine's preflight probe, outside `spawn_fence`: there it
    reads the project fresh rather than calling the store busy."""
    world["probe"] = True
    cid, _ = _propose(live, ai)
    live.decide(live.card(cid)["action_id"])
    assert live.settled(cid)["state"] == "started"


def test_a_tap_waits_for_a_launch_that_is_just_letting_its_lock_go(live, ai, world):
    """A launch writes its card's last state (`pending` again) a moment before it releases the
    decide lock: a tap in that gap waits for the lock rather than answering "still starting"."""
    cid, _ = _propose(live, ai)
    pid = live.card(cid)["action_id"]
    fd = ask_store.try_decide_lock(cid)  # the card reads pending; the lock is still held…
    threading.Timer(0.3, os.close, args=(fd,)).start()  # …and goes a moment later
    assert live.decide(pid).status_code == 202
    assert live.settled(cid)["state"] == "started"
