"""HQ works until it is done or stopped (#2076).

What is pinned here:

* **storage is bounded, the work is not** — a turn stores its first ``STEP_RECORDS_MAX`` no-effect
  steps and EVERY step with an effect; the rest are counted in one ``steps_more`` record. A
  running turn's totals are the server's; a turn that ended without its summary says its count
  is incomplete;
* **the request budget** — old results are dropped first, then whole rounds (a call and its
  result always together), never the question or the newest round; what still does not fit and
  is refused fails as ``context_full``;
* **Stop is a settlement fence** — a flag read at safe points, never a cancellation of the
  turn: the model request and a read are abandoned, an effect runs to its recorded outcome, the
  turn settles ``stopped`` with what it did, and whichever of Stop and the answer reaches the
  settle first wins. Stop names one turn, and the route is guarded like every state change.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures

from __future__ import annotations

import asyncio
import json
import threading
import uuid

import httpx
import pytest

from agent_sessions import (
    actuator,
    ask_orchestrator,
    ask_store,
    memory_capture,
    review,
    session_input,
)
from test_ask_draft_turns import held, run  # noqa: F401 — fixtures are used by name
from test_ask_nudge import target, writer  # noqa: F401
from test_ask_orchestrator import (  # noqa: F401
    CRED,
    QD,
    S1,
    Script,
    ai,
    answer,
    app,
    call,
    calls,
    found,
)

MAX = ask_orchestrator.STEP_RECORDS_MAX


class Looks:
    """A read tool. Its ``hold_at``-th call waits for ``go`` (and says it was ``reached``)."""

    def __init__(self, hold_at: int | None = None):
        self.ran = 0
        self.finished = 0
        self.hold_at = hold_at
        self.reached = asyncio.Event()
        self.go = asyncio.Event()

    async def run(self, turn, args):
        self.ran += 1
        if self.ran == self.hold_at:
            self.reached.set()
            await self.go.wait()
        self.finished += 1
        return {"ok": True}, {"count": 1}


def tool(monkeypatch, name: str, kind: str, run_) -> None:
    spec = ask_orchestrator._spec(name, "a test tool", {"refuse": {"type": "boolean"}}, [])
    monkeypatch.setitem(ask_orchestrator.TOOLS, name, ask_orchestrator.Tool(name, kind, spec, run_))


@pytest.fixture
def looks(monkeypatch):
    def install(hold_at: int | None = None) -> Looks:
        reads = Looks(hold_at)
        tool(monkeypatch, "look", "read", reads.run)
        return reads

    return install


async def begin(cid: str, text: str = "what is going on", **kw) -> tuple[str, asyncio.Task]:
    tid = str(uuid.uuid4())
    task, view = await ask_orchestrator.begin(cid, tid, text, working_keys=set(), **kw)
    assert task is not None and view is None
    return tid, task


async def view_of(cid: str, tid: str) -> dict:
    records = await ask_orchestrator.read_conversation(cid)
    views = ask_orchestrator.with_running(ask_orchestrator.turn_views(records), cid)
    return next(t for t in views if t["turn_id"] == tid)


def records_of(cid: str, kind: str) -> list[dict]:
    return [r for r in ask_store.read(cid) if r["type"] == kind]


# ---- storage: bounded detail, unbounded work ---------------------------------------------------


def test_a_turn_past_the_stored_steps_settles_with_a_summary_and_reloads(app, ai, looks):
    reads = looks()
    ai(calls(*[("look", {})] * (MAX + 60)), answer("done"))
    cid = app.new()
    turn = app.ask(cid, "look at everything").json()["turn"]
    assert turn["status"] == "done" and reads.ran == MAX + 60  # every step ran
    assert len(turn["tools"]) == MAX
    more = turn["steps"]["more"]
    assert turn["steps"]["count"] == MAX + 60 and turn["steps"]["incomplete"] is False
    assert (more["count"], more["tools"], more["refused"]) == (60, {"look": 60}, {})
    assert isinstance(more["ms"], int)
    assert len(records_of(cid, "tool")) == MAX and len(records_of(cid, "steps_more")) == 1
    # Each stored step carries its ordinal and its duration: numbers.
    assert [t["n"] for t in turn["tools"]] == list(range(1, MAX + 1))
    assert all(isinstance(t["ms"], int) for t in turn["tools"])
    reloaded = app.get(cid)["turns"][0]
    assert reloaded["steps"] == turn["steps"] and len(reloaded["tools"]) == MAX
    assert isinstance(reloaded["elapsed_ms"], int)


def test_every_step_reaches_the_model_even_past_the_stored_ones(app, ai, looks):
    looks()
    script = ai(calls(*[("look", {})] * (MAX + 5)), answer("done"))
    app.ask(app.new(), "look")
    assert len(script.tool_results(1)) == MAX + 5


def test_a_running_turn_shows_the_servers_count_and_can_be_stopped_past_the_stored_steps(ai, looks):
    async def scenario():
        reads = looks(hold_at=MAX + 30)
        ai(calls(*[("look", {})] * (MAX + 60)), answer("never"))
        cid = ask_store.create()
        tid, task = await begin(cid)
        await asyncio.wait_for(reads.reached.wait(), 30)
        # A second reader (a reload, another device): the live totals, not the store's 200.
        live = await view_of(cid, tid)
        assert live["status"] == "pending" and len(live["tools"]) == MAX
        assert live["steps"]["count"] == MAX + 29 and live["steps"]["more"]["count"] == 29
        assert live["steps"]["live"]["n"] == MAX + 30 and live["steps"]["live"]["tool"] == "look"
        assert live["steps"]["incomplete"] is False and live["stopping"] is False
        assert isinstance(live["elapsed_ms"], int)
        await ask_orchestrator.stop(cid, tid)
        stopped = await task
        assert stopped["status"] == "stopped" and stopped["answer"] is None
        assert stopped["steps"]["count"] == MAX + 29  # the abandoned read is no step
        assert stopped["steps"]["more"]["tools"] == {"look": 29}
        assert await view_of(cid, tid) == stopped  # …and it reloads the same
        return cid

    cid = run(scenario())
    assert len(records_of(cid, "tool")) == MAX
    assert [r["status"] for r in records_of(cid, "status")] == ["pending", "stopped"]


def test_a_turn_that_ended_without_its_summary_says_its_count_is_incomplete():
    """A crash past the stored steps: the next read settles it `interrupted`, and with no
    `steps_more` record its count is "200+, incomplete" — never "200 steps"."""

    def crashed(steps: int) -> dict:
        cid, tid = ask_store.create(), str(uuid.uuid4())
        ask_store.append(
            cid,
            {"type": "user", "turn_id": tid, "text": "q", "qd": QD},
            {"type": "status", "turn_id": tid, "status": "pending"},
            *(
                {
                    "type": "tool",
                    "turn_id": tid,
                    "call_id": f"c{i}",
                    "name": "list_agents",
                    "args": {},
                    "meta": {"n": i + 1, "ms": 3},
                }
                for i in range(steps)
            ),
        )
        records = asyncio.run(ask_orchestrator.read_conversation(cid))
        (turn,) = ask_orchestrator.with_running(ask_orchestrator.turn_views(records), cid)
        assert turn["status"] == "failed" and turn["code"] == "interrupted"
        return turn["steps"]

    assert crashed(MAX) == {"count": MAX, "more": None, "incomplete": True}
    assert crashed(MAX - 1) == {"count": MAX - 1, "more": None, "incomplete": False}


@pytest.mark.parametrize("outcome", ["delivered", "refused", "indeterminate"])
def test_a_send_after_the_stored_steps_keeps_its_record_through_a_stop(
    app, ai, found, target, writer, looks, monkeypatch, outcome
):
    """What a stopped turn says about sending is read from its stored send records — so a send
    keeps its record whatever its position, and a refused one (nothing written) is only counted."""
    text = CRED if outcome == "refused" else "Rebase."
    if outcome == "indeterminate":

        async def deliver(action_id, **kw):
            return {"id": action_id, "state": "indeterminate"}

        monkeypatch.setattr(actuator, "deliver", deliver)

    async def scenario():
        reads = looks(hold_at=MAX + 1)
        ai(
            calls(*[("look", {})] * MAX),
            calls(
                ("find_sessions", {"query": "rebase"}),
                ("send_nudge", {"key": S1, "text": text}),
                ("look", {}),
            ),
            answer("never"),
        )
        cid = app.new()
        tid, task = await begin(cid, "nudge the rebase session", app=app.app)
        await asyncio.wait_for(reads.reached.wait(), 30)
        await ask_orchestrator.stop(cid, tid)
        return cid, await task

    cid, turn = run(scenario())
    assert turn["status"] == "stopped"
    reloaded = app.get(cid)["turns"][0]
    sends = [t for t in reloaded["tools"] if t["name"] == "send_nudge"]
    more = reloaded["steps"]["more"]
    if outcome == "refused":
        # Nothing was written: a no-effect step past the stored ones, counted apart.
        assert sends == [] and more["refused"] == {"send_nudge": 1}
        assert writer == [] and not any(t.get("sent") for t in reloaded["tools"])
    else:
        (send,) = sends
        assert send["effect"] is True and send["n"] == MAX + 2
        assert send["sent"] == {"delivered": "yes", "indeterminate": "unknown"}[outcome]
        assert more["refused"] == {} and more["tools"] == {"find_sessions": 1}
        assert len(writer) == (1 if outcome == "delivered" else 0)
    assert reloaded["steps"]["count"] == MAX + 2 and len(reloaded["tools"]) == MAX + len(sends)


def test_five_thousand_refused_sends_are_counted_and_the_store_stays_bounded(
    ai, looks, monkeypatch
):
    async def refuse(turn, args):
        raise ask_orchestrator._ToolRefused("refused: no", "not_actuable")

    tool(monkeypatch, "send_thing", "send", refuse)

    async def scenario():
        reads = looks(hold_at=MAX + 1)
        ai(
            calls(*[("look", {})] * MAX),
            calls(*[("send_thing", {})] * 5000, ("look", {})),
            answer("never"),
        )
        cid = ask_store.create()
        tid, task = await begin(cid)
        await asyncio.wait_for(reads.reached.wait(), 60)
        await ask_orchestrator.stop(cid, tid)
        return cid, tid, await task

    cid, tid, turn = run(scenario(), timeout=120)
    assert turn["status"] == "stopped"
    reloaded = asyncio.run(view_of(cid, tid))
    assert reloaded["steps"]["count"] == MAX + 5000
    assert reloaded["steps"]["more"]["refused"] == {"send_thing": 5000}
    assert reloaded["steps"]["more"]["tools"] == {}
    assert len(records_of(cid, "tool")) == MAX
    # The whole conversation is far inside the worst case the read cap is measured against.
    from test_ask_orchestrator import worst_case_bytes

    size = (ask_store.root() / f"{cid}.jsonl").stat().st_size
    assert size < worst_case_bytes() / ask_store.MAX_TURNS < ask_store.READ_MAX_BYTES


def test_the_summary_can_name_every_tool():
    """`steps_more` counts per tool name: the store must admit the whole tool list (and
    "unknown"), or a long turn could not settle."""
    assert len(ask_orchestrator.TOOLS) + 1 <= ask_store.STEPS_MORE_NAMES_MAX
    names = [*ask_orchestrator.TOOLS, "unknown"]
    rec = {
        "type": "steps_more",
        "turn_id": str(uuid.uuid4()),
        "count": 2 * len(names),
        "ms": 5,
        "tools": {n: 1 for n in names},
        "refused": {n: 1 for n in names},
    }
    assert ask_store._admit(rec, [])["tools"] == rec["tools"]
    for bad in ({"count": -1}, {"tools": {"Not A Tool": 1}}, {"refused": {"look": "many"}}):
        with pytest.raises(ValueError):
            ask_store._admit({**rec, **bad}, [])


# ---- the request budget ------------------------------------------------------------------------


def _round(i: int, n_calls: int = 1, args: str = "{}", result: str = '{"ok": true}') -> list[dict]:
    ids = [f"r{i}c{j}" for j in range(n_calls)]
    return [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": c, "type": "function", "function": {"name": "look", "arguments": args}}
                for c in ids
            ],
        },
        *({"role": "tool", "tool_call_id": c, "content": result} for c in ids),
    ]


def _convo(rounds: list[list[dict]]) -> tuple[list[dict], int]:
    head = [{"role": "system", "content": "prompt"}, {"role": "user", "content": "the question"}]
    return [*head, *(m for r in rounds for m in r)], 1


def _paired(convo: list[dict], question_at: int) -> None:
    """A call is never left without its result, nor a result without its call."""
    i = question_at + 1
    while i < len(convo):
        m = convo[i]
        assert m["role"] == "assistant" and m["tool_calls"], "a round opens with its calls"
        ids = [c["id"] for c in m["tool_calls"]]
        got = [x["tool_call_id"] for x in convo[i + 1 : i + 1 + len(ids)]]
        assert got == ids and all(x["role"] == "tool" for x in convo[i + 1 : i + 1 + len(ids)])
        i += 1 + len(ids)


def test_many_tiny_results_make_room_by_whole_rounds(monkeypatch):
    convo, at = _convo([_round(i) for i in range(400)])
    newest = [dict(m) for m in convo[-2:]]
    monkeypatch.setattr(ask_orchestrator, "REQUEST_BUDGET_CHARS", 6_000)
    state = ask_orchestrator._Fit()
    assert ask_orchestrator._fit(convo, at, [], state) is True
    assert ask_orchestrator._request_chars(convo, []) <= 6_000
    _paired(convo, at)
    assert convo[at] == {"role": "user", "content": "the question"}
    kept = [m for m in convo[at + 1 :] if m["role"] == "assistant"]
    assert state.dropped == 400 - len(kept) > 0
    # One fixed note, on the oldest round left; the newest round is as it was.
    assert kept[0]["content"] == f"[{state.dropped} earlier lookups were dropped to make room]"
    assert all(m["content"] is None for m in kept[1:-1])
    assert convo[-1] == newest[-1] and convo[-2]["tool_calls"] == newest[-2]["tool_calls"]
    # Asked again later, the count goes on and the note moves to the oldest round still there.
    convo += [m for i in range(400, 500) for m in _round(i)]
    assert ask_orchestrator._fit(convo, at, [], state) is True
    _paired(convo, at)
    notes = [m["content"] for m in convo if m["role"] == "assistant" and m["content"]]
    assert notes == [f"[{state.dropped} earlier lookups were dropped to make room]"]


def test_old_results_are_dropped_before_any_round(monkeypatch):
    convo, at = _convo([_round(i, result="x" * 2_000) for i in range(10)])
    monkeypatch.setattr(ask_orchestrator, "REQUEST_BUDGET_CHARS", 12_000)
    state = ask_orchestrator._Fit()
    assert ask_orchestrator._fit(convo, at, [], state) is True
    _paired(convo, at)
    results = [m["content"] for m in convo if m["role"] == "tool"]
    assert len(results) == 10 and state.dropped == 0  # every call still has its result
    dropped = results.count(ask_orchestrator.DROPPED_RESULT)
    assert 0 < dropped < 10 and results[:dropped] == [ask_orchestrator.DROPPED_RESULT] * dropped
    assert results[-1] == "x" * 2_000  # oldest first; the newest round is never touched


def test_large_tool_call_arguments_are_dropped_with_their_round(monkeypatch):
    """The arguments count too: placeholders for the results are not enough, so the old rounds
    go whole — each assistant message with ALL of its results."""
    big = json.dumps({"q": "y" * 3_000})
    convo, at = _convo([_round(i, n_calls=3, args=big) for i in range(12)])
    monkeypatch.setattr(ask_orchestrator, "REQUEST_BUDGET_CHARS", 30_000)
    state = ask_orchestrator._Fit()
    assert ask_orchestrator._fit(convo, at, [], state) is True
    assert ask_orchestrator._request_chars(convo, []) <= 30_000
    _paired(convo, at)
    assert state.dropped % 3 == 0 and state.dropped >= 3
    assert convo[at + 1]["content"].startswith(f"[{state.dropped} earlier lookups were dropped")


def test_an_oversized_newest_round_is_sent_as_it_is(monkeypatch):
    convo, at = _convo([_round(0), _round(1, result="z" * 50_000)])
    monkeypatch.setattr(ask_orchestrator, "REQUEST_BUDGET_CHARS", 10_000)
    state = ask_orchestrator._Fit()
    assert ask_orchestrator._fit(convo, at, [], state) is False
    _paired(convo, at)
    assert convo[-1]["content"] == "z" * 50_000 and convo[at]["content"] == "the question"
    assert state.dropped == 1 and len(convo) == at + 3  # only the newest round is left


def test_the_tool_schemas_and_images_count(monkeypatch):
    tools = ask_orchestrator.tool_specs()
    convo, at = _convo([])
    assert ask_orchestrator._request_chars(convo, tools) > len(json.dumps(tools)) > 10_000
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 500_000}}
    convo[at] = {"role": "user", "content": [{"type": "text", "text": "look"}, image]}
    assert ask_orchestrator._request_chars(convo, []) < 2 * ask_orchestrator.IMAGE_PART_CHARS


def test_the_real_request_fits_the_budget_with_room_for_work():
    """The fixed part of every request (the tool schemas, the prompts) leaves most of the budget
    to the turn's own lookups."""
    assert len(json.dumps(ask_orchestrator.tool_specs("call"))) < (
        ask_orchestrator.REQUEST_BUDGET_CHARS / 3
    )
    assert ask_orchestrator.TOOL_RESULT_MAX * 4 < ask_orchestrator.REQUEST_BUDGET_CHARS


def test_a_request_that_cannot_fit_and_is_refused_fails_as_context_full(app, ai, monkeypatch):
    async def huge(turn, args):
        return {"text": "w" * 9_000}, {"length": 9_000}

    tool(monkeypatch, "look", "read", huge)
    first = len(json.dumps(ask_orchestrator.tool_specs())) + 4_000
    monkeypatch.setattr(ask_orchestrator, "REQUEST_BUDGET_CHARS", first)
    refusal = httpx.Response(400, json={"error": {"message": "bad request"}})
    script = ai(call("look"), refusal)
    cid = app.new()
    turn = app.ask(cid, "read the big one").json()["turn"]
    assert (turn["status"], turn["code"]) == ("failed", "context_full")
    assert "more than the model can hold" in turn["detail"]
    # Sent as it was: the newest round whole, its result not replaced.
    assert script.tool_results(1)[-1]["content"].count("w") == 9_000
    assert len(turn["tools"]) == 1  # what it did is kept


def test_a_request_within_budget_goes_on_and_an_endpoints_own_context_refusal_is_named(app, ai):
    words = "This model's maximum context length is 8192 tokens. Reduce the length."
    ai(call("list_agents"), httpx.Response(400, json={"error": {"message": words}}))
    turn = app.ask(app.new(), "agents").json()["turn"]
    assert (turn["status"], turn["code"]) == ("failed", "context_full")
    # …while any other refusal keeps its own reason.
    ai(call("list_agents"), httpx.Response(429, json={"error": {"message": "slow down"}}))
    turn = app.ask(app.new(), "agents").json()["turn"]
    assert turn["code"] is None and "rate limited" in turn["detail"]


def test_a_long_turn_drops_old_results_on_the_wire_and_still_answers(app, ai, monkeypatch):
    parts = iter(range(1_000))

    async def big(turn, args):
        # Each result says something new (a distinct part), so the loop guard (#2113) stays out.
        return {"text": "w" * 3_000, "part": next(parts)}, {"length": 3_000}

    tool(monkeypatch, "look", "read", big)
    # Room for the system prompt, the question and the newest whole result (15,000 since #1938's
    # specialists paragraph and #2251's steps paragraph grew the prompt).
    budget = len(json.dumps(ask_orchestrator.tool_specs())) + 15_000
    monkeypatch.setattr(ask_orchestrator, "REQUEST_BUDGET_CHARS", budget)
    script = ai(*[call("look", cid=f"r{i}") for i in range(30)], answer("all read"))
    turn = app.ask(app.new(), "read them all").json()["turn"]
    assert turn["status"] == "done" and turn["steps"]["count"] == 30
    last = script.bodies[-1]["messages"]
    assert len(json.dumps(last)) + len(json.dumps(script.bodies[-1]["tools"])) <= budget + 200
    _paired(last, next(i for i, m in enumerate(last) if m["role"] == "user"))
    assert last[-1]["content"].count("w") == 3_000  # the newest result is whole


# ---- Stop --------------------------------------------------------------------------------------


def test_stop_during_the_model_wait_settles_stopped_and_the_next_turn_can_continue(held, ai):
    async def scenario():
        script = held(answer("never said"))
        cid = ask_store.create()
        tid, task = await begin(cid, "how far is the epic")
        await asyncio.wait_for(script.reached.wait(), 15)
        await ask_orchestrator.stop(cid, tid)
        turn = await asyncio.wait_for(task, 5)  # settled without the endpoint ever answering
        assert not ask_orchestrator._RUNNING and not script.go.is_set()
        assert (turn["status"], turn["answer"], turn["detail"]) == ("stopped", None, None)
        # The slot and the lock are free: the next message runs, and replays the stopped
        # question with its fixed note, so "continue" has something to continue.
        after = ai(answer("carrying on"))
        _, nxt = await begin(cid, "continue")
        assert (await nxt)["answer"] == "carrying on"
        return cid, after

    cid, after = run(scenario())
    kinds = [r["type"] for r in ask_store.read(cid)]
    assert kinds[:4] == ["conversation", "user", "status", "status"]
    assert records_of(cid, "assistant")[0]["text"] == "carrying on"
    msgs = after.bodies[0]["messages"]
    at = next(i for i, m in enumerate(msgs) if m["content"] == "how far is the epic")
    assert msgs[at + 1] == {"role": "assistant", "content": ask_orchestrator.STOPPED_TURN_NOTE}
    assert msgs[at + 2]["content"] == "continue"


def test_stop_between_tools_lets_the_effect_finish_and_runs_nothing_after_it(
    ai, looks, monkeypatch
):
    async def scenario():
        reached, go, staged = asyncio.Event(), asyncio.Event(), []

        async def stage(turn, args):
            reached.set()
            await go.wait()
            staged.append("x")
            return {"proposed": True}, {"count": 1}

        tool(monkeypatch, "propose_thing", "proposal", stage)
        reads = looks()
        script = ai(calls(("propose_thing", {}), ("look", {})), answer("never"))
        cid = ask_store.create()
        tid, task = await begin(cid)
        queue = ask_orchestrator.listen(cid, task)
        await asyncio.wait_for(reached.wait(), 15)
        while not queue.empty():
            queue.get_nowait()
        stopping = asyncio.create_task(ask_orchestrator.stop(cid, tid))
        await asyncio.sleep(0.05)
        # Stop is a flag, not a cancellation: the tool with an effect is still running.
        assert not task.done() and staged == []
        assert (await view_of(cid, tid))["stopping"] is True
        # From the Stop on, the turn publishes nothing more: not a late callback, and not the
        # end of the step that is still finishing.
        ask_orchestrator._RUNNING[cid].turn.progress({"type": "progress", "step": "catalog"})
        go.set()
        await stopping
        turn = await task
        assert queue.empty()
        assert staged == ["x"] and reads.ran == 0 and len(script.bodies) == 1
        assert turn["status"] == "stopped"
        assert [(t["name"], t.get("effect")) for t in turn["tools"]] == [("propose_thing", True)]

    run(scenario())


def test_stop_during_send_nudge_sends_once_then_stops(app, ai, found, target, looks, monkeypatch):
    in_send, release, sent = threading.Event(), threading.Event(), []

    def send_input(phys, payload, **kw):
        in_send.set()
        assert release.wait(20)
        sent.append(payload)
        return session_input.Outcome("delivered", "")

    monkeypatch.setattr(session_input, "send_input", send_input)

    async def scenario():
        reads = looks()
        script = ai(
            call("find_sessions", {"query": "rebase"}, cid="f"),
            calls(("send_nudge", {"key": S1, "text": "Rebase."}), ("look", {})),
            answer("never"),
        )
        cid = app.new()
        tid, task = await begin(cid, "nudge it", app=app.app)
        assert await asyncio.to_thread(in_send.wait, 20)
        stopping = asyncio.create_task(ask_orchestrator.stop(cid, tid))
        await asyncio.sleep(0.05)
        assert not task.done()  # the claim, the delivery and the outcome record come first
        release.set()
        await stopping
        turn = await task
        assert turn["status"] == "stopped" and len(sent) == 1
        assert reads.ran == 0 and len(script.bodies) == 2
        send = turn["tools"][-1]
        assert (send["name"], send["sent"], send["effect"]) == ("send_nudge", "yes", True)
        return cid

    cid = run(scenario())
    assert len(sent) == 1  # …and never a second time
    assert [p["kind"] for p in app.get(cid)["turns"][0]["proposals"]] == ["nudge"]


def test_a_stop_that_reaches_the_settle_first_leaves_no_answer(ai, monkeypatch):
    """The model's answer is in hand, and Stop lands before it is written: Stop wins."""

    def stop_now(question, *, expected=None):
        (r,) = ask_orchestrator._RUNNING.values()
        r.turn.stop.set()  # what `stop()` does, between the answer and the settle
        return None

    monkeypatch.setattr(memory_capture, "intent", stop_now)

    async def scenario():
        ai(answer("the answer that lost"))
        cid = ask_store.create()
        tid, task = await begin(cid)
        turn = await task
        assert (turn["status"], turn["answer"]) == ("stopped", None)
        return cid

    cid = run(scenario())
    assert records_of(cid, "assistant") == []
    assert "the answer that lost" not in json.dumps(ask_store.read(cid))


def test_a_stop_after_the_answer_is_written_changes_nothing(app, ai):
    ai(answer("the answer"))
    cid, tid = app.new(), str(uuid.uuid4())
    app.ask(cid, "q", tid)
    r = app.c.post(f"/api/ask/conversations/{cid}/turns/{tid}/stop", headers=app.hdr)
    assert r.status_code == 200 and r.json()["running"] is False
    assert (r.json()["turn"]["status"], r.json()["turn"]["answer"]) == ("done", "the answer")
    assert [s["status"] for s in records_of(cid, "status")] == ["pending", "done"]


def test_a_stop_racing_the_settle_under_the_lock_is_a_no_op(ai, monkeypatch):
    """Stop arrives while the answer is being written: it waits for the conversation lock, finds
    the turn settled, and changes nothing."""
    real = ask_orchestrator._append

    async def scenario():
        ai(answer("the answer"))
        cid = ask_store.create()
        racing: list[asyncio.Task] = []
        tid = None

        async def slow_append(c, *records):
            if any(r.get("type") == "assistant" for r in records):
                racing.append(asyncio.create_task(ask_orchestrator.stop(cid, tid)))
                await asyncio.sleep(0.05)  # Stop is now waiting for the lock the settle holds
            return await real(c, *records)

        monkeypatch.setattr(ask_orchestrator, "_append", slow_append)
        tid, task = await begin(cid)
        turn = await task
        await asyncio.gather(*racing)
        assert racing and (turn["status"], turn["answer"]) == ("done", "the answer")
        assert (await view_of(cid, tid))["status"] == "done"

    run(scenario())


def test_an_abandoned_reads_late_completion_records_and_publishes_nothing(ai, monkeypatch):
    in_read, release, late = threading.Event(), threading.Event(), []

    async def slow_read(turn, args):
        def work():
            in_read.set()
            assert release.wait(20)
            turn.progress({"type": "progress", "step": "catalog", "sessions": 9})  # too late
            late.append("finished")
            return {"ok": True}

        return await asyncio.to_thread(work), {"count": 1}

    tool(monkeypatch, "look", "read", slow_read)

    async def scenario():
        script = ai(call("look"), answer("never"))
        cid = ask_store.create()
        tid, task = await begin(cid)
        queue = ask_orchestrator.listen(cid, task)
        assert await asyncio.to_thread(in_read.wait, 20)
        await ask_orchestrator.stop(cid, tid)
        turn = await asyncio.wait_for(task, 5)  # settled while the worker thread still runs
        assert late == [] and turn["status"] == "stopped" and turn["tools"] == []
        seen = []
        while not queue.empty():
            seen.append(queue.get_nowait())
        release.set()
        for _ in range(200):
            if late:
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        assert late == ["finished"] and queue.empty()  # it finished; nothing was published
        assert [e["step"] for e in seen] == ["thinking", "tool"]
        assert seen[1]["n"] == 1 and isinstance(seen[1]["at"], float)
        assert (await view_of(cid, tid))["tools"] == [] and len(script.bodies) == 1
        return cid

    cid = run(scenario())
    assert records_of(cid, "tool") == []
    assert [s["status"] for s in records_of(cid, "status")] == ["pending", "stopped"]


def test_progress_carries_each_steps_ordinal_start_and_stored_record(app, ai, looks):
    looks()
    ai(calls(("look", {}), ("list_agents", {})), answer("ok"))
    cid = app.new()
    r = app.c.post(
        f"/api/ask/conversations/{cid}/message/stream",
        json={"turn_id": str(uuid.uuid4()), "text": "look"},
        headers=app.hdr,
    )
    lines = [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
    began = [e for e in lines if e.get("step") == "tool"]
    done = [e for e in lines if e.get("step") == "tool_done"]
    assert [(e["n"], e["tool"]) for e in began] == [(1, "look"), (2, "list_agents")]
    assert all(isinstance(e["at"], float) for e in began)
    assert [(e["n"], e["steps"], e["more"], e["outcome"]) for e in done] == [
        (1, 1, 0, "ok"),
        (2, 2, 0, "ok"),
    ]
    assert done[0]["record"] == lines[-1]["turn"]["tools"][0]
    assert all(isinstance(e["ms"], int) for e in done)
    assert [e["steps"] for e in lines if e.get("step") == "thinking"] == [0, 2]


def test_stop_twice_and_a_stale_id_never_stop_the_turn_that_replaced_it(held, ai):
    async def scenario():
        script = held(answer("never"))
        cid = ask_store.create()
        first, task = await begin(cid, "one")
        await asyncio.wait_for(script.reached.wait(), 15)
        await asyncio.gather(ask_orchestrator.stop(cid, first), ask_orchestrator.stop(cid, first))
        assert (await task)["status"] == "stopped"
        await ask_orchestrator.stop(cid, first)  # again, long after
        # The turn that replaced it is running now: the old id, and an unknown one, change nothing.
        script.reached.clear()
        second, task2 = await begin(cid, "two")
        await asyncio.wait_for(script.reached.wait(), 15)
        await ask_orchestrator.stop(cid, first)
        await ask_orchestrator.stop(cid, str(uuid.uuid4()))
        turn = ask_orchestrator._RUNNING[cid].turn
        assert not turn.stop.is_set() and not task2.done()
        assert (await view_of(cid, second))["status"] == "pending"
        script.go.set()
        assert (await task2)["answer"] == "never"
        return cid, first

    cid, first = run(scenario())
    statuses = [s["status"] for s in records_of(cid, "status") if s["turn_id"] == first]
    assert statuses == ["pending", "stopped"]  # one outcome, however often it was stopped


def test_an_open_draft_is_not_stopped_it_keeps_discard(held):
    async def scenario():
        script = held(answer("never"))
        cid = ask_store.create()
        tid = str(uuid.uuid4())
        task, _ = await ask_orchestrator.begin(
            cid, tid, "what is running", working_keys=set(), via="call", draft=True
        )
        await asyncio.wait_for(script.reached.wait(), 15)
        await ask_orchestrator.stop(cid, tid)
        assert not task.done() and not ask_orchestrator._RUNNING[cid].turn.stop.is_set()
        assert ask_orchestrator.running_turn(cid) is None
        assert await ask_orchestrator.discard(cid, tid) is True

    run(scenario())


def test_the_stop_route_stops_the_named_turn_and_answers_with_it(app, held):
    async def scenario():
        script = held(answer("never"))
        cid = app.new()
        tid, task = await begin(cid)
        await asyncio.wait_for(script.reached.wait(), 15)
        transport = httpx.ASGITransport(app=app.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver", cookies=dict(app.c.cookies)
        ) as c:
            base = f"/api/ask/conversations/{cid}/turns"
            # Refused without the session, without the CSRF token, and from another origin —
            # and none of those stopped anything.
            async with httpx.AsyncClient(
                transport=transport, base_url="https://testserver"
            ) as anon:
                assert (await anon.post(f"{base}/{tid}/stop", headers=app.hdr)).status_code == 401
            no_csrf = {"Origin": app.hdr["Origin"]}
            assert (await c.post(f"{base}/{tid}/stop", headers=no_csrf)).status_code == 403
            foreign = {**app.hdr, "Origin": "https://evil.example"}
            assert (await c.post(f"{base}/{tid}/stop", headers=foreign)).status_code == 403
            assert not task.done() and not ask_orchestrator._RUNNING[cid].turn.stop.is_set()
            # An unknown turn or conversation is a 404, and stops nothing either.
            other = await c.post(f"{base}/{uuid.uuid4()}/stop", headers=app.hdr)
            assert other.status_code == 404 and not task.done()
            gone = f"/api/ask/conversations/ask_{'0' * 32}/turns/{tid}/stop"
            assert (await c.post(gone, headers=app.hdr)).status_code == 404
            assert (await c.post(f"{base}/not-a-turn/stop", headers=app.hdr)).status_code == 404
            r = await c.post(f"{base}/{tid}/stop", headers=app.hdr)
            assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
            body = r.json()
            assert body["running"] is False and body["turn"]["status"] == "stopped"
            assert body["turn"]["turn_id"] == tid and body["turn"]["answer"] is None
            again = await c.post(f"{base}/{tid}/stop", headers=app.hdr)
            assert again.status_code == 200 and again.json()["turn"] == body["turn"]
        assert (await task)["status"] == "stopped"

    run(scenario())


def test_a_stopped_turn_keeps_what_it_did_and_its_question_stays_open(app, ai, looks):
    """The question, every step so far and any card already staged are kept; there is no answer,
    and a stopped answer to an `ask_operator` question leaves that question open."""

    async def scenario():
        reads = looks(hold_at=3)
        ai(calls(("look", {}), ("look", {}), ("look", {})), answer("never"))
        cid = app.new()
        tid, task = await begin(cid, "look three times")
        await asyncio.wait_for(reads.reached.wait(), 15)
        await ask_orchestrator.stop(cid, tid)
        return cid, tid, await task

    cid, tid, turn = run(scenario())
    assert [r["type"] for r in ask_store.read(cid)] == [
        "conversation", "user", "status", "tool", "tool", "status",
    ]  # fmt: skip
    reloaded = app.get(cid)
    assert reloaded["running"] is False and reloaded["turns"] == [turn]
    assert turn["question"] == "look three times" and turn["status"] == "stopped"
    assert [t["n"] for t in turn["tools"]] == [1, 2] and turn["steps"]["count"] == 2
    assert "stopping" not in turn and isinstance(turn["elapsed_ms"], int)
    # The same id again is a replay of the stopped turn, never a second run.
    replay = app.ask(cid, "look three times", tid).json()["turn"]
    assert replay["status"] == "stopped"


# ---- the web's label table ---------------------------------------------------------------------


def test_every_hq_tool_has_a_label_in_the_webs_one_table():
    """The process row names each step from ONE table (`web/src/components/ask/askStep.ts`): it
    must cover every tool the server offers, and name none that does not exist."""
    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "web" / "src" / "components" / "ask" / "askStep.ts"
    ).read_text(encoding="utf-8")
    table = source[source.index("export const TOOL_LABELS") :]
    table = table[: table.index("\n};")]
    labelled = set(re.findall(r"^  ([a-z_]+): \{ label: \"", table, flags=re.M))
    assert labelled == set(ask_orchestrator.TOOLS)


# ---- the no-tools path: a routed memory turn (review of #2080) ----------------------------------


class _Every(set):
    """Every endpoint is one that said it takes no tools."""

    def __contains__(self, key) -> bool:
        return True


def routed(action: str, **fields) -> dict:
    """The question router's reply that sends the turn to memory."""
    return answer(json.dumps({"memory": {"action": action, **fields}}))


@pytest.fixture
def no_tools(monkeypatch):
    """An endpoint known to take no tools (the turn goes straight to the no-tools path), with
    the question router on."""
    from agent_sessions import prefs

    prefs.set_hq_route({"enabled": True})
    monkeypatch.setattr(ask_orchestrator, "_TOOLS_UNSUPPORTED", _Every())


def test_a_no_tools_memory_recall_is_abandoned_by_stop(held, ai, no_tools, monkeypatch):
    """Recall only reads: its model wait is a child Stop cancels, so the turn settles `stopped`
    at once however long the endpoint holds the request — and the next question can start."""
    from agent_sessions import ask_memory

    async def found(turn, args):
        return {"notes": [{"title": "Sources", "body": "Use primary sources."}]}, {"count": 1}

    monkeypatch.setattr(ask_memory, "search", found)

    async def scenario():
        script = held(routed("recall", query="primary sources"), answer("never"), free=1)
        cid = ask_store.create()
        tid, task = await begin(cid, "what did I say about sources?")
        await asyncio.wait_for(script.reached.wait(), 15)  # the recall's own model request
        assert ask_orchestrator._RUNNING[cid].turn.inflight is not None
        await ask_orchestrator.stop(cid, tid)
        turn = await asyncio.wait_for(task, 5)  # never waited for the held endpoint
        assert (turn["status"], turn["answer"]) == ("stopped", None)
        assert not script.go.is_set() and not ask_orchestrator._RUNNING
        ai(answer("a new answer"))
        monkeypatch.setattr(ask_orchestrator, "_TOOLS_UNSUPPORTED", set())  # tools again
        _, nxt = await begin(cid, "something else")
        assert (await nxt)["answer"] == "a new answer"
        return cid, tid

    cid, tid = run(scenario())
    statuses = [s["status"] for s in records_of(cid, "status") if s["turn_id"] == tid]
    assert statuses == ["pending", "stopped"]


def test_a_no_tools_memory_recalls_search_is_abandoned_too(ai, no_tools, monkeypatch):
    from agent_sessions import ask_memory

    in_search, release = asyncio.Event(), asyncio.Event()

    async def slow_search(turn, args):
        in_search.set()
        await release.wait()
        return {"notes": []}, {"count": 0}

    monkeypatch.setattr(ask_memory, "search", slow_search)

    async def scenario():
        script = ai(routed("recall", query="sources"), answer("never"))
        cid = ask_store.create()
        tid, task = await begin(cid, "what did I say about sources?")
        await asyncio.wait_for(in_search.wait(), 15)
        await ask_orchestrator.stop(cid, tid)
        turn = await asyncio.wait_for(task, 5)
        assert turn["status"] == "stopped" and not release.is_set()
        assert len(script.bodies) == 1  # the recall's model request was never made

    run(scenario())


def test_a_no_tools_memory_write_is_a_stored_effect_step(app, ai, no_tools):
    """The routed write goes through the same step boundary as every tool: an ordinal, a
    duration and an effect record, so the process view shows what HQ changed — after a reload."""
    from agent_sessions import memory

    ai(routed("remember", title="Research protocol", quote="Use primary sources."))
    cid = app.new()
    turn = app.ask(cid, "Remember: Use primary sources.").json()["turn"]
    assert (turn["status"], turn["answer"]) == ("done", "I saved that in HQ memory.")
    assert memory.notes(memory.HQ_SCOPE)
    reloaded = app.get(cid)["turns"][0]
    (step,) = reloaded["tools"]
    assert (step["name"], step["outcome"], step["n"]) == ("remember_hq_memory", "ok", 1)
    assert step["effect"] is True and isinstance(step["ms"], int)
    assert reloaded["steps"] == {"count": 1, "more": None, "incomplete": False}
    (rec,) = records_of(cid, "tool")
    assert rec["meta"]["effect"] is True and rec["args"]["title"] == "Research protocol"


def test_a_stop_during_a_no_tools_memory_write_keeps_the_write_and_its_step(
    app, ai, no_tools, monkeypatch
):
    """The write is the one part of a routed turn Stop does not abandon: it reaches its recorded
    outcome, the turn then settles `stopped`, and the reloaded turn shows the memory step."""
    from agent_sessions import memory

    real = ask_orchestrator.TOOLS["remember_hq_memory"]

    async def scenario():
        reached, go = asyncio.Event(), asyncio.Event()

        async def held_write(turn, args):
            reached.set()
            await go.wait()
            return await real.run(turn, args)

        monkeypatch.setitem(
            ask_orchestrator.TOOLS,
            "remember_hq_memory",
            ask_orchestrator.Tool(real.name, real.kind, real.spec, held_write),
        )
        ai(routed("remember", title="Research protocol", quote="Use primary sources."))
        cid = app.new()
        tid, task = await begin(cid, "Remember: Use primary sources.", app=app.app)
        await asyncio.wait_for(reached.wait(), 15)
        stopping = asyncio.create_task(ask_orchestrator.stop(cid, tid))
        await asyncio.sleep(0.05)
        assert not task.done() and not memory.notes(memory.HQ_SCOPE)  # still writing
        go.set()
        assert await stopping is True
        return cid, await task

    cid, turn = run(scenario())
    assert (turn["status"], turn["answer"]) == ("stopped", None)
    assert memory.notes(memory.HQ_SCOPE)  # the write completed, once
    reloaded = app.get(cid)["turns"][0]
    (step,) = reloaded["tools"]
    assert (step["name"], step["effect"], step["n"]) == ("remember_hq_memory", True, 1)
    assert reloaded["steps"]["count"] == 1 and reloaded["status"] == "stopped"


def test_stop_ends_an_endless_circle_the_guard_only_re_steers(ai, monkeypatch):
    """#2113, #2136: the guard never closes the tools (the operator's direction: never stop,
    sanity-check and continue), so a circle the model never leaves runs until the Commander's
    Stop — which settles it `stopped`, with no answer."""

    async def scenario():
        reached, go = asyncio.Event(), asyncio.Event()
        script = ai(call("session_status", {"key": "claude:never-returned"}))  # refused, always
        real = script.handler

        async def handler(request):
            body = json.loads(request.content)
            assert "tool_choice" not in body  # the tools are never closed
            if "Do not repeat a call" in request.content.decode():  # re-steered, still going
                reached.set()
                await go.wait()
            return await real(request)

        monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
        cid = ask_store.create()
        tid, task = await begin(cid, "where is the epic")
        await asyncio.wait_for(reached.wait(), 30)
        assert await ask_orchestrator.stop(cid, tid) is True
        turn = await asyncio.wait_for(task, 10)
        go.set()
        return cid, turn

    cid, turn = run(scenario())
    assert (turn["status"], turn["answer"]) == ("stopped", None)
    assert not records_of(cid, "assistant")
    assert all("code" not in r or r["code"] != "no_progress" for r in records_of(cid, "status"))


def test_stop_says_whether_the_named_turn_is_being_stopped(held, ai):
    async def scenario():
        script = held(answer("never"))
        cid = ask_store.create()
        tid, task = await begin(cid)
        await asyncio.wait_for(script.reached.wait(), 15)
        assert await ask_orchestrator.stop(cid, str(uuid.uuid4())) is False
        assert await ask_orchestrator.stop(cid, tid) is True
        await task
        assert await ask_orchestrator.stop(cid, tid) is False  # settled: nothing to stop

    run(scenario())


def test_request_stop_is_synchronous_and_refuses_an_open_draft_unless_its_commit_is_the_callers(
    held,
):
    """The flag is set with no await. An open draft keeps `discard`; one whose commit the caller
    is making is flagged before `committed` is set, and then settles `stopped` once committed."""

    async def scenario():
        script = held(answer("never"))
        cid = ask_store.create()
        tid = str(uuid.uuid4())
        task, _ = await ask_orchestrator.begin(
            cid, tid, "what is running", working_keys=set(), via="call", draft=True
        )
        await asyncio.wait_for(script.reached.wait(), 15)
        turn = ask_orchestrator._RUNNING[cid].turn
        assert ask_orchestrator.request_stop(cid, str(uuid.uuid4()), committing=True) is False
        assert ask_orchestrator.request_stop(cid, tid) is False and not turn.stop.is_set()
        assert ask_orchestrator.request_stop(cid, tid, committing=True) is True
        assert turn.stop.is_set() and not turn.draft.committed.is_set()
        assert await ask_orchestrator.commit(cid, tid) is True
        stopped = await asyncio.wait_for(task, 5)
        assert stopped["status"] == "stopped" and stopped["question"] == "what is running"
        assert ask_orchestrator.request_stop(cid, tid) is False  # settled
        return cid

    cid = run(scenario())
    assert [s["status"] for s in records_of(cid, "status")] == ["pending", "stopped"]
