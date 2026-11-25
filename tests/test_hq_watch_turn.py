"""The turn watcher inside HQ's turn (#2136): a scripted endpoint for HQ, a scripted judge.

Pinned here:
- a `replan` reaches HQ as DATA on the newest tool result (`watcher_note`), never as a system
  message, and the screen hears "replan"; on a call it is said once as an aside;
- never an end (the operator's direction, 2026-11-18): the #2116 guard's wrap re-steers with the
  tools open, with or without a watcher verdict; the turn ends when HQ answers;
- notes: the `note` tool keeps HQ's own line, every next request carries them on its NEWEST
  result only, and a note is never progress for the loop guard;
- Stop during a watcher judgement stops the turn and cancels the judgement.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures
from __future__ import annotations

import asyncio
import json

import pytest

from agent_sessions import ask_orchestrator, ask_store, hq_watch
from test_ask_orchestrator import ai, answer, call  # noqa: F401 — fixtures are used by name


class Judge:
    def __init__(self, *verdicts, gate: asyncio.Event | None = None):
        self.verdicts = list(verdicts) or [{"verdict": "fine"}]
        self.digests: list[dict] = []
        self.gate = gate
        self.cancelled = False

    async def __call__(self, digest: str):
        self.digests.append(json.loads(digest))
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        return self.verdicts[min(len(self.digests) - 1, len(self.verdicts) - 1)]


@pytest.fixture
def judge(monkeypatch):
    def install(*verdicts, gate=None) -> Judge:
        j = Judge(*verdicts, gate=gate)
        monkeypatch.setattr(hq_watch, "judge_model", j)
        return j

    # Look after every round, so a test needs few rounds.
    monkeypatch.setattr(hq_watch, "EVERY_ROUNDS", 1)
    return install


def _turn(via: str | None = None, question: str = "archive every session that is done"):
    events: list[dict] = []
    turn = ask_orchestrator._Turn(
        cid=ask_store.create(),
        turn_id="11111111-1111-4111-8111-111111111111",
        question=question,
        working_keys=set(),
        progress=events.append,
    )
    turn.via = via
    return turn, events


def _run(turn):
    async def go():
        return await ask_orchestrator.run_turn(turn)

    return asyncio.run(go())


def _projects(cid="p") -> dict:
    return call("list_projects", {}, cid=cid)


def _tool_payloads(body: dict) -> list[dict]:
    out = []
    for m in body["messages"]:
        if m.get("role") == "tool":
            try:
                out.append(json.loads(m["content"]))
            except ValueError:
                out.append({})
    return out


def test_a_replan_reaches_hq_as_data_on_the_newest_result_never_as_system_text(ai, judge):
    j = judge({"verdict": "replan", "strategy": "decide from the list you already have"})
    script = ai(_projects("a"), _projects("b"), _projects("c"), answer("Done."))
    turn, events = _turn()
    records = _run(turn)
    assert records[-1]["status"] == "done"
    assert j.digests  # the watcher looked
    noted = [b for b in script.bodies if any("watcher_note" in t for t in _tool_payloads(b))]
    assert noted, "a request carried the watcher's strategy"
    first = noted[0]
    assert _tool_payloads(first)[-1]["watcher_note"] == "decide from the list you already have"
    assert not any(
        "decide from the list" in str(m.get("content")) for m in first["messages"]
        if m.get("role") == "system"
    )  # fmt: skip
    assert {"type": "progress", "step": "replan",
            "text": "decide from the list you already have"} in events  # fmt: skip
    assert turn.watch is not None and turn.watch.strategies


def test_a_replan_on_a_call_is_said_once_as_an_aside(ai, judge):
    judge({"verdict": "replan", "strategy": "decide from the list"}, {"verdict": "fine"})
    ai(_projects("a"), _projects("b"), _projects("c"), answer("Done."))
    turn, events = _turn(via="call")
    _run(turn)
    asides = [e["text"] for e in events if e.get("type") == "aside"]
    assert asides.count("Changing approach: decide from the list") == 1


def test_the_guards_wrap_re_steers_with_the_tools_open_and_the_turn_goes_on(ai, monkeypatch):
    # conftest's default judge gives no verdict: only the #2116 guard speaks, every round.
    monkeypatch.setattr(ask_orchestrator._LoopGuard, "end_round", lambda self: "wrap")
    script = ai(_projects("a"), _projects("b"), _projects("c"), answer("Here is what I have."))
    turn, _ = _turn()
    records = _run(turn)
    assert records[0]["text"] == "Here is what I have." and records[-1]["status"] == "done"
    assert "code" not in records[-1]
    assert all("tool_choice" not in b for b in script.bodies)  # the tools never closed
    for body in script.bodies[1:]:
        assert "Do not repeat a call" in _tool_payloads(body)[-1]["hq_note"]


def test_notes_ride_on_the_newest_result_only_and_are_never_progress(ai, judge):
    judge({"verdict": "fine"})
    script = ai(
        call("note", {"key": "s1", "text": "finished, nothing pending"}, cid="n"),
        _projects("a"),
        answer("Done."),
    )
    turn, _ = _turn()
    _run(turn)
    second, third = script.bodies[1], script.bodies[2]
    assert _tool_payloads(second)[-1]["turn_notes"] == [
        {"key": "s1", "note": "finished, nothing pending"}
    ]
    payloads = _tool_payloads(third)
    assert "turn_notes" not in payloads[0] and payloads[-1]["turn_notes"][0]["key"] == "s1"
    # A note never resets the loop guard's streak: it was not observed as a step.
    assert turn.loop.calls and all(not k.startswith("note\0") for k in turn.loop.calls)


def test_stop_while_a_judgement_runs_stops_the_turn_and_cancels_it(ai, judge):
    gate = asyncio.Event()
    j = judge({"verdict": "fine"}, gate=gate)
    # HQ repeats the same call forever: nothing ends this turn but the Commander's Stop.
    ai(_projects("a"))
    turn, _ = _turn()

    async def go():
        task = asyncio.ensure_future(ask_orchestrator.run_turn(turn))
        while not j.digests:
            await asyncio.sleep(0.01)
        turn.stop.set()
        if turn.inflight is not None:
            turn.inflight.cancel()
        with pytest.raises(ask_orchestrator._Stopped):
            await task

    asyncio.run(go())
    assert j.cancelled


def test_results_about_to_be_dropped_become_notes_on_that_same_request(ai, monkeypatch):
    """Over budget (#2136): the oldest results are summarised into the turn's notes BEFORE `_fit`
    drops them, and the request that drops them already carries the notes."""
    from test_ask_orchestrator import _fake_tool

    seen: list[list[dict]] = []

    async def summarise(request, notes, items):
        seen.append(items)
        return [(f"batch{len(seen)}", f"remembered {len(items)} results")]

    async def read_big(turn, args):
        return {"text": "B" * 6_000, "n": len(seen)}, {}

    monkeypatch.setattr(hq_watch, "summarise", summarise)
    _fake_tool(monkeypatch, "read", read_big, "read_big")
    base = ask_orchestrator._request_chars([], ask_orchestrator.tool_specs(None))
    monkeypatch.setattr(ask_orchestrator, "REQUEST_BUDGET_CHARS", base + 20_000)
    script = ai(*[call("read_big", cid=f"b{i}") for i in range(8)], answer("Done."))
    turn, _ = _turn()
    records = _run(turn)
    assert records[0]["text"] == "Done."
    assert seen, "the request went over budget and results were remembered first"
    dropped = [
        b for b in script.bodies
        if any(m.get("content") == ask_orchestrator.DROPPED_RESULT for m in b["messages"])
    ]  # fmt: skip
    assert dropped
    assert _tool_payloads(dropped[0])[-1]["turn_notes"][0]["key"] == "batch1"


def test_remembering_takes_only_what_must_go_and_never_the_history_or_newest_round(monkeypatch):
    seen: list[list[dict]] = []

    async def summarise(request, notes, items):
        seen.append(items)
        return [("s1", "finished")]

    monkeypatch.setattr(hq_watch, "summarise", summarise)
    turn, _ = _turn()
    big = "R" * 1_000
    convo = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "an earlier question in this conversation"},
        {"role": "assistant", "content": "HISTORY ANSWER"},
        {"role": "user", "content": "archive the done ones"},  # the question
    ]
    question_at = 3
    for r in range(4):
        fn = {"name": "session_brief", "arguments": json.dumps({"key": f"s{r}"})}
        tool_call = {"id": f"c{r}", "type": "function", "function": fn}
        convo.append({"role": "assistant", "content": None, "tool_calls": [tool_call]})
        convo.append({"role": "tool", "tool_call_id": f"c{r}", "content": f"{r}:{big}"})
    budget = ask_orchestrator._request_chars(convo, []) - 1_500  # two results must go
    monkeypatch.setattr(ask_orchestrator, "REQUEST_BUDGET_CHARS", budget)

    asyncio.run(ask_orchestrator._remember_before_dropping(turn, convo, question_at, []))
    # the notes ride on the newest result of THIS request
    assert "turn_notes" in json.loads(convo[-1]["content"])
    assert turn.notes.render() == [{"key": "s1", "note": "finished"}]
    (items,) = seen
    assert [it["result"][:2] for it in items] == ["0:", "1:"]  # the oldest, only as many as needed
    assert all("HISTORY" not in it["result"] for it in items)
    # Each result is summarised once: asked again, nothing new is sent.
    asyncio.run(ask_orchestrator._remember_before_dropping(turn, convo, question_at, []))
    assert len(seen) == 2 and [it["result"][:2] for it in seen[1]] == ["2:"]


def test_every_result_fit_drops_was_summarised_first_even_past_one_batch(monkeypatch):
    """Hermes 8438: sixteen full-sized results, fourteen of which must go. Every result `_fit`
    then drops was in a summarising request — in batches within the summariser's limits — and
    none was marked handled without being sent."""
    batches: list[list[dict]] = []

    async def summarise(request, notes, items):
        batches.append(items)
        return [(f"batch{len(batches)}", f"{len(items)} results remembered")]

    monkeypatch.setattr(hq_watch, "summarise", summarise)
    turn, _ = _turn()
    convo = [{"role": "system", "content": "sys"}, {"role": "user", "content": "the question"}]
    for r in range(16):
        fn = {"name": "session_brief", "arguments": json.dumps({"key": f"s{r}"})}
        tool_call = {"id": f"c{r}", "type": "function", "function": fn}
        convo.append({"role": "assistant", "content": None, "tool_calls": [tool_call]})
        convo.append(
            {"role": "tool", "tool_call_id": f"c{r}", "content": f"R{r:02d}" + "x" * 5_000}
        )
    original = {m["tool_call_id"]: m["content"] for m in convo if m.get("role") == "tool"}
    budget = ask_orchestrator._request_chars(convo, []) - 14 * 5_000
    monkeypatch.setattr(ask_orchestrator, "REQUEST_BUDGET_CHARS", budget)

    asyncio.run(ask_orchestrator._remember_before_dropping(turn, convo, 1, []))
    ask_orchestrator._fit(convo, 1, [], ask_orchestrator._Fit())

    sent = {it["result"][:3] for batch in batches for it in batch}
    dropped = [
        m["tool_call_id"]
        for m in convo
        if m.get("role") == "tool" and m.get("content") == ask_orchestrator.DROPPED_RESULT
    ]
    assert len(dropped) >= 14 and len(batches) >= 2
    assert all(original[cid][:3] in sent for cid in dropped)  # nothing dropped unseen
    assert all(len(b) <= hq_watch.SUMMARISE_ITEMS_MAX for b in batches)
    assert all(
        sum(min(len(it["result"]), hq_watch.SUMMARISE_RESULT_MAX) for it in b)
        <= hq_watch.SUMMARISE_CHARS_MAX
        for b in batches
    )
    assert turn.summarised <= {f"c{r}" for r in range(15)}  # never the newest round
    assert "turn_notes" in json.loads(convo[-1]["content"])


def _notes_in(convo: list[dict]) -> list[list[dict]]:
    """Every `turn_notes` copy anywhere in the request, in order."""
    out = []
    for m in convo:
        if m.get("role") == "tool":
            try:
                value = json.loads(m["content"])
            except (ValueError, TypeError):
                continue
            if isinstance(value, dict) and "turn_notes" in value:
                out.append(value["turn_notes"])
    return out


def test_exactly_the_current_notes_are_sent_after_fit_deletes_whole_rounds(monkeypatch):
    """Hermes 8440: `_fit` deletes whole rounds in place (its second branch: large call
    arguments, so dropping results alone is not enough), which moves the message carrying the
    notes. After that, an updated and a deleted note must leave exactly ONE copy of exactly the
    current notes in the request — no stale version anywhere."""
    turn, _ = _turn()
    convo = [{"role": "system", "content": "sys"}, {"role": "user", "content": "the question"}]

    def add_round(r: int) -> None:
        args = json.dumps({"key": f"s{r}", "pad": "A" * 4_000})
        tool_call = {"id": f"c{r}", "type": "function",
                     "function": {"name": "session_brief", "arguments": args}}  # fmt: skip
        convo.append({"role": "assistant", "content": None, "tool_calls": [tool_call]})
        convo.append({"role": "tool", "tool_call_id": f"c{r}", "content": json.dumps({"r": r})})

    for r in range(3):
        add_round(r)
    turn.notes.set("progress", "checked s0")
    turn.notes.set("s0", "finished")
    ask_orchestrator._carry_notes(turn, convo)
    before = len(convo)
    budget = ask_orchestrator._request_chars(convo, []) - 6_000  # results alone can't make room
    monkeypatch.setattr(ask_orchestrator, "REQUEST_BUDGET_CHARS", budget)
    ask_orchestrator._fit(convo, 1, [], ask_orchestrator._Fit())
    assert len(convo) < before  # the whole-round branch ran: the carrier moved

    add_round(3)
    turn.notes.set("progress", "checked s0, s1, s2, s3")  # updated
    turn.notes.set("s0", "")  # deleted
    ask_orchestrator._carry_notes(turn, convo)

    assert _notes_in(convo) == [[{"key": "progress", "note": "checked s0, s1, s2, s3"}]]
    assert "turn_notes" in json.loads(convo[-1]["content"])


def test_deleting_the_last_note_leaves_no_copy():
    turn, _ = _turn()
    convo = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": []},
        {"role": "tool", "tool_call_id": "c0", "content": "{}"},
    ]
    turn.notes.set("progress", "half done")
    ask_orchestrator._carry_notes(turn, convo)
    turn.notes.set("progress", "")
    ask_orchestrator._carry_notes(turn, convo)
    assert _notes_in(convo) == []
