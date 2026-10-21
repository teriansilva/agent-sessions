"""Running missions every 30 s, a resting session within seconds, and spend on a WALL CLOCK (#1214).

The supervisor used to look at a mission every five minutes; an opencode permission prompt sat for
3.5 minutes before it was read. It now looks at running missions every 30 s and a prompt watch wakes
a mission as soon as one of its sessions comes to rest. These tests pin the three things that make
that safe:

* **Spend is per unit of time, not per pass** (`mission_pace.Pace`): an idle mission costs zero
  model calls however often it is passed; a working session is read on the old five-minute interval;
  a session that keeps stopping is capped per window; a reading the cap refused, or whose call
  failed, stays PENDING rather than being lost or retried every 30 s.
* **Rest is observed cheaply and once** (`mission_now.rest_episode`, `session_rest`, `watch`): a
  settled prompt or a quiet screen is one episode; the same episode never wakes twice; ordinary
  output while producing never wakes; an unobserved session falls back to its store.
* **The cadence** — a fast ring over running missions, a slow ring over review missions, and a watch
  serviced between a sweep's passes, so a slow pass delays a wake by one pass, not one batch.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import mission_now, mission_pace, missions, scrollback
from agent_sessions import mission_supervisor as sup
from agent_sessions import mission_supervisor_loop as loop

SESSION = "claude:11111111-1111-1111-1111-111111111111"
READ = mission_pace.READ_INTERVAL_S
WIN = mission_pace.WINDOW_S
CAP = mission_pace.MODEL_CALLS_PER_WINDOW


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def pace(clock):
    return mission_pace.Pace(clock)


# ---- Pace, on its own --------------------------------------------------------------------------


def test_a_never_read_session_is_due_and_a_read_one_waits_the_interval(pace, clock):
    assert pace.read_due("m", "s", None) == (True, "")
    pace.note_read("m", "s", None)
    clock.t += READ - 1
    assert pace.read_due("m", "s", None)[0] is False
    clock.t += 1
    assert pace.read_due("m", "s", None)[0] is True


def test_a_NEW_rest_episode_is_due_at_once_and_the_same_one_never_again(pace, clock):
    pace.note_read("m", "s", None)
    clock.t += 5
    assert pace.read_due("m", "s", "screen:1") == (True, "")
    pace.note_read("m", "s", "screen:1")
    clock.t += 30
    assert pace.read_due("m", "s", "screen:1")[0] is False, "the same rest was read twice"
    assert pace.read_due("m", "s", "screen:2")[0] is True
    assert pace.read_due("m", "s", None)[0] is False, "a working session read inside the interval"


def test_the_model_cap_is_per_SLIDING_window(pace, clock):
    for _ in range(CAP):
        assert pace.take_model_call("m", "s")[0] is True
        clock.t += 10
    allowed, why = pace.take_model_call("m", "s")
    assert allowed is False and "budget" in why
    clock.t = 1000.0 + WIN  # the first call leaves the window
    assert pace.take_model_call("m", "s")[0] is True
    assert pace.take_model_call("m", "s")[0] is False


def test_the_cap_is_per_session_and_mission(pace):
    for _ in range(CAP):
        pace.take_model_call("m", "s")
    assert pace.take_model_call("m", "other")[0] is True
    assert pace.take_model_call("other", "s")[0] is True


def test_an_EXHAUSTED_budget_is_not_read_and_its_trigger_stays_pending(pace, clock):
    for _ in range(CAP):
        pace.take_model_call("m", "s")
    due, why = pace.read_due("m", "s", "screen:4")
    assert due is False and "pending" in why
    # Nothing was recorded, so the SAME episode is due the moment a call frees.
    clock.t += WIN
    assert pace.read_due("m", "s", "screen:4") == (True, "")


def test_restore_read_un_consumes(pace, clock):
    prev = pace.note_read("m", "s", "screen:1")
    assert prev is None
    clock.t += 30
    prev = pace.note_read("m", "s", "screen:2")
    pace.restore_read("m", "s", prev)
    assert pace.read_due("m", "s", "screen:2")[0] is True
    assert pace.read_due("m", "s", "screen:1")[0] is False


def test_asks_and_probes_keep_the_old_five_minute_spacing(pace, clock):
    assert pace.take_ask("m", "k") is True
    assert pace.take_ask("m", "k") is False
    assert pace.take_ask("m", "other") is True
    assert pace.take_probes("m") is True
    clock.t += mission_pace.PROBE_INTERVAL_S - 1
    assert pace.take_probes("m") is False
    assert pace.take_ask("m", "k") is False
    clock.t += 1
    assert pace.take_probes("m") is True
    assert pace.take_ask("m", "k") is True


def test_store_rest_is_one_episode_per_resting_mark(pace, clock):
    assert pace.store_rest("s", 5, 20.0) is None  # first sighting starts the clock
    clock.t += 19
    assert pace.store_rest("s", 5, 20.0) is None
    clock.t += 1
    assert pace.store_rest("s", 5, 20.0) == "store:5"
    clock.t += 100
    assert pace.store_rest("s", 5, 20.0) == "store:5", "the same rest must keep its id"
    assert pace.store_rest("s", 6, 20.0) is None, "growth ends the episode"
    clock.t += 20
    assert pace.store_rest("s", 6, 20.0) == "store:6"


# ---- rest, observed on the screen --------------------------------------------------------------


@pytest.fixture
def screen(monkeypatch):
    """A fake ring: its visible-output clock and its screen text."""
    state = {"last": None, "text": ""}
    monkeypatch.setattr(scrollback, "get_last_visible_output_at", lambda k: state["last"])
    monkeypatch.setattr(scrollback, "live_tail_text", lambda k, n=4000: state["text"])
    mission_now.reset_cache_for_test()
    yield state
    mission_now.reset_cache_for_test()


def test_no_clock_is_UNOBSERVED_not_quiet(screen):
    assert mission_now.rest_episode("k", now=100.0) == (False, None)


def test_a_SETTLED_permission_prompt_rests_after_3s(screen):
    screen["last"] = 100.0
    screen["text"] = "Allow this command?\n  git log --oneline\nDo you want to proceed? (y/n)"
    assert mission_now.rest_episode("k", now=101.0) == (True, None), "not yet settled"
    mission_now.reset_cache_for_test()
    observed, ep = mission_now.rest_episode("k", now=100.0 + mission_now.PROMPT_SETTLE_S)
    assert observed and ep == "screen:100.0"


def test_ordinary_NUMBERED_output_is_not_a_rest_until_the_screen_goes_quiet(screen):
    """Negative control: `_prompt_class` reads `1.` in ordinary output as a choice. A session that
    printed a numbered plan and is still producing is not resting; one that stopped is — which is
    right, and the model (not the classifier) decides what that stop means."""
    screen["last"] = 100.0
    screen["text"] = "Plan:\n1. read the code\n2. write the test\nWorking…"
    assert mission_now.rest_episode("k", now=101.0) == (True, None)
    observed, ep = mission_now.rest_episode("k", now=100.0 + mission_now.QUIET_AFTER_S)
    assert ep == "screen:100.0"


def test_an_UNRECOGNISED_dialog_rests_once_the_screen_is_quiet(screen):
    screen["last"] = 100.0
    screen["text"] = (
        "△ Permission required\n  Grep '(?i)mission'\n  Allow once   Allow always   Reject"
    )
    assert mission_now.rest_episode("k", now=110.0) == (True, None), "open class, not yet quiet"
    ep_quiet = mission_now.rest_episode("k", now=100.0 + mission_now.QUIET_AFTER_S)[1]
    assert ep_quiet == "screen:100.0"


def test_a_prompt_and_its_later_quiet_are_ONE_episode_and_new_output_is_another(screen):
    screen["last"] = 100.0
    screen["text"] = "Continue? (y/n)"
    a = mission_now.rest_episode("k", now=104.0)[1]
    b = mission_now.rest_episode("k", now=200.0)[1]
    assert a == b is not None
    screen["last"] = 210.0
    assert mission_now.rest_episode("k", now=211.0)[1] is None
    assert mission_now.rest_episode("k", now=240.0)[1] not in (None, a)


def test_session_rest_falls_back_to_the_STORE_when_unobserved(screen, pace, clock, monkeypatch):
    from agent_sessions import mission_fence, transcript

    monkeypatch.setattr(mission_fence, "physical_of", lambda k, path=None: k)
    marks = {"n": 7}
    monkeypatch.setattr(transcript, "growth_mark", lambda e, n, h: marks["n"])
    assert sup.session_rest(SESSION, pace) is None
    clock.t += mission_now.QUIET_AFTER_S
    assert sup.session_rest(SESSION, pace) == "store:7"
    marks["n"] = 8
    assert sup.session_rest(SESSION, pace) is None
    # …and an OBSERVED session is judged by its screen, never its store.
    screen["last"] = 0.0
    assert sup.session_rest(SESSION, pace) is not None
    assert sup.session_rest(SESSION, pace).startswith("screen:")


# ---- the paced pass: model calls on a wall clock -----------------------------------------------


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "m.db"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    missions.reset_schema_cache_for_test()
    return tmp_path


def _running_mission(session: str = SESSION, title: str = "ship it") -> str:
    mid = missions.create_mission(title, cwd="/tmp")["id"]
    missions.set_state(mid, "draft", "planned")
    missions.set_state(mid, "planned", "dispatching")
    missions.set_state(mid, "dispatching", "running")
    missions.adopt(mid, session)
    missions.instantiate_objectives(
        mid,
        [{"key": "k", "title": "k", "probe": "forge_pr", "gate": True, "source": "playbook"}],
    )
    return mid


@pytest.fixture
def world(store, monkeypatch):
    """A running mission whose session input, rest and model are all under the test's control."""
    from agent_sessions import mission_probes, review

    state = {"fp": "fp0", "rest": None, "calls": 0, "fail": False, "probes": 0}
    monkeypatch.setattr(review, "_require_config", lambda: {"base_url": "x", "api_key": "y"})
    monkeypatch.setattr(review, "gather_input", lambda *a, **k: ("transcript", state["fp"]))

    async def complete(messages, **kw):
        state["calls"] += 1
        if state["fail"]:
            raise review.ReviewError("endpoint did not answer")
        return {"recap": "working", "assessment": "on_track"}

    monkeypatch.setattr(review, "complete_json", complete)
    monkeypatch.setattr(sup, "session_rest", lambda sk, p, path=None: state["rest"])
    monkeypatch.setattr(sup, "session_is_stalled", lambda *a, **k: (False, "", None))

    def probes(mid, path=None):
        state["probes"] += 1
        return {}

    monkeypatch.setattr(mission_probes, "run_for_mission", probes)
    state["mid"] = _running_mission()
    return state


async def _passes(world, pace, clock, n: int, *, step: float = 30.0, each=None) -> None:
    for i in range(n):
        if each:
            each(i)
        try:
            await sup.run_pass(world["mid"], pace=pace)
        except Exception:  # noqa: BLE001 — a failing reading raises out of the pass; the loop logs it
            pass
        clock.t += step


@pytest.mark.anyio
async def test_an_IDLE_mission_costs_ZERO_model_calls_across_many_passes(world, pace, clock):
    """Warmed up (read once), then an hour of 30 s passes with resting episodes coming and going on
    an unchanged input: not one more model call."""
    await _passes(world, pace, clock, 1)
    assert world["calls"] == 1, "the first reading of a never-read session"

    def rest(i):
        world["rest"] = f"screen:{i // 4}"  # a new rest now and then, same input throughout

    await _passes(world, pace, clock, 120, each=rest)
    assert world["calls"] == 1


@pytest.mark.anyio
async def test_a_WORKING_session_is_read_on_the_old_interval_not_every_pass(world, pace, clock):
    """Changing output every 30 s, never at rest: one model call per `READ_INTERVAL_S`, exactly
    what the five-minute sweep spent."""

    def moving(i):
        world["fp"] = f"fp{i}"

    await _passes(world, pace, clock, 20, each=moving)  # 0..570 s
    assert world["calls"] == 2  # t=0 and t=300


@pytest.mark.anyio
async def test_a_session_that_KEEPS_STOPPING_is_capped_per_window(world, pace, clock):
    def stops(i):
        world["fp"] = f"fp{i}"
        world["rest"] = f"screen:{i}"

    await _passes(world, pace, clock, 10, each=stops)  # 0..270 s: ten distinct stops
    assert world["calls"] == CAP
    await _passes(world, pace, clock, 1, each=stops)  # t=300: the first call left the window
    assert world["calls"] == CAP + 1


@pytest.mark.anyio
async def test_EXHAUSTION_then_refill_reads_the_pending_stop_ONCE(world, pace, clock):
    """The fourth stop inside the window waits; with no further output it is read exactly once
    when the window frees a call — not dropped, and not read on every pass after."""

    def stops(i):
        world["fp"] = f"fp{i}"
        world["rest"] = f"screen:{i}"

    await _passes(world, pace, clock, CAP + 1, each=stops)  # t=0,30,60,90 — the 4th is refused
    assert world["calls"] == CAP
    # No further output: same input, same rest, pass after pass across the window boundary.
    await _passes(world, pace, clock, 15)
    assert world["calls"] == CAP + 1, "the pending stop was dropped or read more than once"


@pytest.mark.anyio
async def test_a_FAILED_reading_stays_pending_and_is_charged(world, pace, clock):
    world["fail"] = True
    world["fp"], world["rest"] = "fpX", "screen:x"
    await _passes(world, pace, clock, 10)  # one stop, a failing endpoint, ten passes
    assert world["calls"] == CAP, "a failing endpoint was retried every pass, or not at all"
    world["fail"] = False
    clock.t = 1000.0 + WIN
    await _passes(world, pace, clock, 3)
    assert world["calls"] == CAP + 1, "the stop the failures left pending was never read"


@pytest.mark.anyio
@pytest.mark.parametrize("what", ["capture", "gather", "gather-raises", "checkpoint", "facts"])
async def test_a_read_that_FAILED_BEFORE_the_model_leaves_the_stop_pending(
    world, pace, clock, monkeypatch, what
):
    """#1214 review, finding 2 (rounds 1 and 2). A failed authority capture, input read, checkpoint
    or objective lookup never completed the read, so the stop that woke the mission is retried on
    the next pass, not after `READ_INTERVAL_S` — and nothing is charged for it."""
    from agent_sessions import automation, review

    await _passes(world, pace, clock, 1)  # warmed up: one reading of fp0
    assert world["calls"] == 1
    failing = {"on": True}
    if what == "capture":
        real = automation.capture

        def capture(*a, **k):
            if failing["on"]:
                raise RuntimeError("store busy")
            return real(*a, **k)

        monkeypatch.setattr(automation, "capture", capture)
    elif what == "checkpoint":
        real_cp = missions.supervisor_checkpoint

        def checkpoint(*a, **k):
            if failing["on"]:
                raise OSError("database is locked")
            return real_cp(*a, **k)

        monkeypatch.setattr(missions, "supervisor_checkpoint", checkpoint)
    elif what == "facts":
        real_facts = sup._objective_facts

        def facts(*a, **k):
            if failing["on"]:
                raise OSError("database is locked")
            return real_facts(*a, **k)

        monkeypatch.setattr(sup, "_objective_facts", facts)
    else:
        err = review.ReviewError("nothing yet") if what == "gather" else OSError("disk")

        def gather(*a, **k):
            if failing["on"]:
                raise err
            return ("transcript", world["fp"])

        monkeypatch.setattr(review, "gather_input", gather)

    world["fp"], world["rest"] = "fp-prompt", "screen:prompt"
    await _passes(world, pace, clock, 1)
    assert world["calls"] == 1 and pace.model_calls_in_window(world["mid"], SESSION) == 1
    failing["on"] = False
    await _passes(world, pace, clock, 1)  # 30 s later, the same stop
    assert world["calls"] == 2, "the failed read consumed the stop"


@pytest.mark.anyio
async def test_the_probes_keep_their_old_cadence_under_a_30s_pass(world, pace, clock):
    await _passes(world, pace, clock, 20)  # 0..570 s
    assert world["probes"] == 2


@pytest.mark.anyio
async def test_an_UNPACED_pass_is_unthrottled_as_before(world):
    for i in range(3):
        world["fp"] = f"fp{i}"
        await sup.run_pass(world["mid"])
    assert world["calls"] == 3 and world["probes"] == 3


@pytest.mark.anyio
async def test_an_owed_question_is_retried_on_the_old_spacing(world, pace, clock, monkeypatch):
    """A question that does not land is retried by the next pass — 30 s away now — so the retry is
    paced: one attempt per objective per `ASK_RETRY_S`."""
    from agent_sessions import mission_questions

    assert missions.escalate_once(
        world["mid"], session_key=SESSION, objective_key="k", episode=1, reason="stuck"
    )
    asks: list[str] = []

    async def ask(mid, key, *, context="", path=None):
        asks.append(key)
        return None  # never lands

    monkeypatch.setattr(mission_questions, "ask", ask)
    await _passes(world, pace, clock, 12)  # 0..330 s
    assert asks == ["k", "k"], asks


# ---- the watch -----------------------------------------------------------------------------------


@pytest.fixture
def watched(monkeypatch):
    """Held sessions and their rest episodes, under the test's control."""
    state = {"held": {}, "rest": {}, "on": True}
    monkeypatch.setattr(loop, "_running_held_sessions", lambda: dict(state["held"]))
    monkeypatch.setattr(sup, "session_rest", lambda sk, p, path=None: state["rest"].get(sk))
    monkeypatch.setattr(loop, "_enabled", lambda: state["on"])
    monkeypatch.setattr(loop, "_watch_seen", {})
    return state


@pytest.mark.anyio
async def test_a_resting_session_wakes_its_mission_ONCE_per_episode(watched):
    watched["held"] = {"a:1": "m1", "a:2": "m1", "b:1": "m2"}
    assert await loop.watch() == []
    watched["rest"] = {"a:1": "screen:1", "a:2": "screen:9"}
    assert await loop.watch() == ["m1"], "two resting sessions of one mission are one wake"
    assert await loop.watch() == [], "the same episode woke twice"
    watched["rest"]["a:1"] = None  # output again…
    assert await loop.watch() == []
    watched["rest"]["a:1"] = "screen:2"  # …and a new stop
    watched["rest"]["b:1"] = "store:4"
    assert await loop.watch() == ["m1", "m2"]


@pytest.mark.anyio
async def test_a_DISABLED_supervisor_watches_nothing_and_forgets(watched):
    watched["held"] = {"a:1": "m1"}
    watched["rest"] = {"a:1": "screen:1"}
    watched["on"] = False
    assert await loop.watch() == []
    watched["on"] = True
    assert await loop.watch() == ["m1"]


@pytest.mark.anyio
async def test_a_session_no_running_mission_holds_is_forgotten(watched):
    watched["held"] = {"a:1": "m1"}
    watched["rest"] = {"a:1": "screen:1"}
    assert await loop.watch() == ["m1"]
    watched["held"] = {}
    assert await loop.watch() == [] and loop._watch_seen == {}


@pytest.mark.anyio
async def test_a_woken_mission_gets_the_ORDINARY_paced_pass_and_no_judge_call(watched, monkeypatch):
    passed: list = []

    async def run_pass(mid, registry=None, **kw):
        passed.append((mid, kw.get("pace")))
        return {}

    judged: list = []

    async def judge_batch(ids, budget):
        judged.append(ids)
        return {}

    monkeypatch.setattr(loop.mission_supervisor, "run_pass", run_pass)
    monkeypatch.setattr(loop.mission_judge, "judge_batch", judge_batch)
    monkeypatch.setattr(loop, "_next_watch", 0.0)
    watched["held"] = {"a:1": "m1", "b:1": "m2"}
    watched["rest"] = {"a:1": "screen:1", "b:1": "screen:1"}
    report = await loop.service_watch(now=10.0)
    assert report == {"m1": "passed", "m2": "passed"}
    assert [p[0] for p in passed] == ["m1", "m2"] and all(p[1] is loop._pace for p in passed)
    assert judged == []
    assert await loop.service_watch(now=11.0) == {}, "the watch ran again inside its interval"


@pytest.mark.anyio
async def test_a_stop_during_a_SLOW_sweep_is_served_between_its_passes(store, watched, monkeypatch):
    """The sweep holds the single-flight for its whole batch. A stop that appears while its first
    pass is in flight is served right after that pass — not after the batch."""
    order: list[str] = []

    async def run_pass(mid, registry=None, **kw):
        order.append(mid)
        if mid == "m_first":
            # The slow pass in flight; meanwhile two sessions of two other missions stop.
            await asyncio.sleep(0.01)
            watched["held"] = {"x:1": "m_woken_a", "y:1": "m_woken_b"}
            watched["rest"] = {"x:1": "screen:1", "y:1": "screen:1"}
        return {}

    monkeypatch.setattr(loop.mission_supervisor, "run_pass", run_pass)
    monkeypatch.setattr(loop, "_early", {})
    monkeypatch.setattr(loop, "_next_watch", 0.0)
    monkeypatch.setattr(loop, "_wake", asyncio.Event())
    out = {"swept": 0, "nudged": 0, "escalated": 0}
    await loop._sweep_batch(["m_first", "m_second"], out, None, paced=True)
    assert order == ["m_first", "m_woken_a", "m_woken_b", "m_second"], order
    assert out["woken"] == 2
    # …and serving the wakes did not cost the ring its progress: the cursor still reached the end.
    assert missions.get_supervisor_state(loop._CURSOR_KEY) == "m_second"
    assert out["swept"] == 2


@pytest.mark.anyio
async def test_wakes_that_KEEP_re_arming_cannot_stall_the_ring(store, watched, monkeypatch):
    """A session that stops again after every pass is served at most once per watch interval, so
    a batch still visits every mission in order and advances its cursor."""
    order: list[str] = []
    n = {"ep": 0}

    async def run_pass(mid, registry=None, **kw):
        order.append(mid)
        n["ep"] += 1
        watched["rest"] = {"x:1": f"screen:{n['ep']}"}  # a fresh stop after every pass
        return {}

    monkeypatch.setattr(loop.mission_supervisor, "run_pass", run_pass)
    monkeypatch.setattr(loop, "_early", {})
    monkeypatch.setattr(loop, "_next_watch", 0.0)
    monkeypatch.setattr(loop, "_wake", asyncio.Event())
    watched["held"] = {"x:1": "m_noisy"}
    batch = [f"m{i}" for i in range(6)]
    out = {"swept": 0, "nudged": 0, "escalated": 0}
    await loop._sweep_batch(batch, out, None, paced=True)
    assert [m for m in order if m != "m_noisy"] == batch
    assert order.count("m_noisy") == 1, "the watch ran more than once inside its interval"
    assert missions.get_supervisor_state(loop._CURSOR_KEY) == "m5"


@pytest.mark.anyio
async def test_the_fast_sweep_leaves_a_mission_to_its_PENDING_early_reading(store, monkeypatch):
    passed: list[str] = []

    async def run_pass(mid, registry=None, **kw):
        passed.append(mid)
        return {}

    monkeypatch.setattr(loop.mission_supervisor, "run_pass", run_pass)
    monkeypatch.setattr(loop, "_early", {"m_new": (99.0, 0)})
    monkeypatch.setattr(loop, "_wake", None)
    out = {"swept": 0, "nudged": 0, "escalated": 0}
    await loop._sweep_batch(["m_new", "m_old"], out, None, paced=True)
    assert passed == ["m_old"] and out["deferred_to_early"] == 1 and out["swept"] == 1


# ---- the rings -----------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_MORE_than_one_batch_of_running_missions_is_covered_and_review_is_not_in_it(
    store, monkeypatch
):
    running = {_running_mission(f"claude:{i:08d}-1111-1111-1111-111111111111") for i in range(30)}
    review_mid = _running_mission("claude:99999999-1111-1111-1111-111111111111", "in review")
    missions.set_state(review_mid, "running", "review")
    visited: list[str] = []

    async def run_pass(mid, registry=None, **kw):
        visited.append(mid)
        return {}

    async def no_judge(ids, budget):
        return {}

    async def anoop():
        return None

    monkeypatch.setattr(loop.mission_supervisor, "run_pass", run_pass)
    monkeypatch.setattr(loop.mission_judge, "judge_batch", no_judge)
    monkeypatch.setattr(loop, "_enabled", lambda: True)
    monkeypatch.setattr(loop, "_reconcile_delivered", anoop)
    monkeypatch.setattr(loop, "_early", {})

    fast = {
        "states": loop.FAST_STATES,
        "cursor_key": loop._CURSOR_KEY,
        "reconcile": False,
        "paced": True,
    }
    first = await loop.sweep(**fast)
    assert first["swept"] == loop.MISSIONS_PER_SWEEP == 25
    await loop.sweep(**fast)
    assert set(visited) == running and len(visited) == 30, "the ring missed or repeated a mission"
    assert review_mid not in visited

    visited.clear()
    slow = await loop.sweep(states=loop.SLOW_STATES, cursor_key=loop.SLOW_CURSOR_KEY)
    assert visited == [review_mid] and slow["swept"] == 1
    # …and the slow ring kept its own cursor: the fast ring's is untouched by it.
    assert missions.get_supervisor_state(loop._CURSOR_KEY) in running


@pytest.mark.anyio
async def test_the_loop_runs_the_FAST_ring_on_its_interval_and_the_slow_one_on_its_own(
    monkeypatch,
):
    monkeypatch.setattr(loop, "FAST_INTERVAL_S", 0.02)
    monkeypatch.setattr(loop, "MIN_SWEEP_GAP_S", 0.0)
    monkeypatch.setattr(loop, "INTERVAL_S", 3600.0)
    monkeypatch.setattr(loop, "WATCH_INTERVAL_S", 3600.0)
    monkeypatch.setattr(loop, "_early", {})

    async def anoop():
        return None

    monkeypatch.setattr(loop, "_reconcile_delivered", anoop)
    swept: list[tuple] = []

    async def sweep(registry=None, **kw):
        swept.append(kw["states"])
        return {}

    monkeypatch.setattr(loop, "sweep", sweep)

    async def drive():
        task = asyncio.create_task(loop.run())
        for _ in range(200):
            if len(swept) >= 3:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    await drive()
    assert len(swept) >= 3 and set(swept) == {loop.FAST_STATES}, swept


@pytest.fixture
def paced_env(store, monkeypatch):
    async def anoop():
        return None

    async def run_pass(mid, registry=None, **kw):
        return {}

    monkeypatch.setattr(loop, "_enabled", lambda: True)
    monkeypatch.setattr(loop, "_reconcile_delivered", anoop)
    monkeypatch.setattr(loop.mission_supervisor, "run_pass", run_pass)
    monkeypatch.setattr(loop, "_early", {})
    monkeypatch.setattr(loop, "_wake", None)
    monkeypatch.setattr(loop, "_window_at", None)
    monkeypatch.setattr(loop, "_early_window_at", None)
    monkeypatch.setattr(loop, "_judge_due", set())
    return monkeypatch


@pytest.mark.anyio
async def test_the_loop_JUDGES_ONCE_PER_WINDOW_over_every_mission_it_supervised(paced_env):
    """Sweeps are 30 s apart now; judging per sweep would be ten times the spend
    `JUDGE_CALLS_PER_SWEEP` used to mean. The loop judges once per `JUDGE_WINDOW_S`, with one fresh
    budget, over everything its sweeps supervised since the last judge phase; the early readings'
    budget carries over within the window too."""
    from agent_sessions import mission_judge as mj

    calls: list[tuple[list[str], int]] = []

    async def judge_batch(ids, budget):
        calls.append((list(ids), budget.left))
        return {}

    paced_env.setattr(loop.mission_judge, "judge_batch", judge_batch)
    paced_env.setattr(loop, "MISSIONS_PER_SWEEP", 2)
    mids = sorted(_running_mission(f"claude:{i:08d}-2222-2222-2222-222222222222") for i in range(4))
    fast = {"states": loop.FAST_STATES, "paced": True, "reconcile": False}

    await loop.sweep(now=1000.0, **fast)  # opens the first window: judges its own page
    assert calls == [(mids[:2], mj.JUDGE_CALLS_PER_SWEEP)]
    spent_early = mj.Budget(0)
    paced_env.setattr(loop, "_early_budget", spent_early)
    for t in (1030.0, 1060.0, 1000.0 + loop.JUDGE_WINDOW_S - 1):
        out = await loop.sweep(now=t, **fast)
        assert out["judge_skipped"] == "not this window"
    assert len(calls) == 1, "a sweep inside the window judged"
    assert loop._early_budget is spent_early, "the early budget refilled inside the window"

    await loop.sweep(now=1000.0 + loop.JUDGE_WINDOW_S, **fast)
    ids, left = calls[-1]
    assert ids == mids and left == mj.JUDGE_CALLS_PER_SWEEP, "not every supervised mission"
    assert loop._early_budget is not spent_early


@pytest.mark.anyio
async def test_a_SECOND_PAGE_is_judged_even_when_the_first_always_has_demand(paced_env):
    """#1214 review, finding 1. With a budget carried across the pages of a ring, the window always
    opened on the same page, that page's continuing demand spent all six calls, and the other page
    never got one — `judge_batch` only rotates within the ids it is given. Judging once per window
    over every supervised mission puts both pages in one least-recently-served order."""
    served: dict[str, float] = {}
    clock = {"t": 0.0}

    async def judge_batch(ids, budget):
        # A faithful stand-in for `rank_missions` + `judge_one`: least recently served first, every
        # mission ALWAYS has two calls of due work, charged against the budget it was given.
        for mid in sorted(ids, key=lambda m: (served.get(m, -1.0), m)):
            if budget.left < 2:
                break
            budget.take(mid)
            budget.take(mid)
            served[mid] = clock["t"]
        return {}

    paced_env.setattr(loop.mission_judge, "judge_batch", judge_batch)
    mids = {_running_mission(f"claude:{i:08d}-3333-3333-3333-333333333333") for i in range(30)}
    fast = {"states": loop.FAST_STATES, "paced": True, "reconcile": False}
    for n in range(100):  # 100 fast sweeps = ten windows over two pages (25 + 5)
        clock["t"] = 1000.0 + n * loop.FAST_INTERVAL_S
        await loop.sweep(now=clock["t"], **fast)
    assert set(served) == mids, f"never judged: {sorted(mids - set(served))}"


@pytest.mark.anyio
@pytest.mark.parametrize("with_early", [False, True])
async def test_an_EARLY_reading_at_a_window_boundary_cannot_take_the_sweeps_judge_phase(
    paced_env, with_early
):
    """#1214 review round 2, finding 1. The loop serves due early readings BEFORE sweeps. When the
    early path opened the (shared) window, the sweep right after it saw no new window and only
    queued its missions — one boundary early reading postponed every ordinary judgment by five
    minutes, and repeating it starved them. The early readings keep their own budget clock now, so
    the sweeps judge exactly as often with early readings interleaved as without."""
    phases: list[list[str]] = []

    async def judge_batch(ids, budget):
        if budget is not loop._early_budget:
            phases.append(list(ids))
        return {}

    paced_env.setattr(loop.mission_judge, "judge_batch", judge_batch)
    ordinary = _running_mission()
    fast = {"states": loop.FAST_STATES, "paced": True, "reconcile": False}
    for n in range(31):  # t = 1000 .. 1900, every 30 s
        t = 1000.0 + n * loop.FAST_INTERVAL_S
        if with_early and t in (1300.0, 1600.0, 1900.0):
            loop.request_early_pass("msn_brand_new", now=t - loop.EARLY_READING_DELAY_S)
            await loop.run_due_early(now=t)
        await loop.sweep(now=t, **fast)
    assert len(phases) == 4, phases  # 1000, 1300, 1600, 1900 — with or without early readings
    assert all(ordinary in ids for ids in phases)
    assert ordinary not in loop._judge_due
