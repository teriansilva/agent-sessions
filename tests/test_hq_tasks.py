"""HQ's running questions are tasks (#2103): queued instead of refused, visible, told when done.

What is pinned here (``docs/invariants/hq-tasks.md``, ``ask-orchestrator.md``):

* **the queue** — a message sent while a turn runs is admitted as ``queued`` (at most
  ``QUEUE_MAX``, in order) and starts when the running turn settles; admission takes no turn
  lock; the retry contract holds for a queued turn; Withdraw and the start of the next turn
  meet under one lock; Stop leaves the queue and Stop all clears it, with no successor; a
  queued turn does not survive a restart;
* **no fence moves** — a queued turn is a turn like any other: the same tools, kinds and
  budgets, and nothing in this change acts without the operator;
* **being told** — one bell row per settled turn, only while its SETTLEMENT is unseen, written
  after a grace, retracted when seen, reconciled once after a crash, with a fixed title and no
  model text, opening the conversation;
* **the list** — only stored first lines.
"""

# ruff: noqa: F811 — imported isolated pytest fixtures

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import time
import uuid
from pathlib import Path

import httpx
import pytest

from agent_sessions import (
    ask_follow,
    ask_orchestrator,
    ask_store,
    hq_tasks,
    notifications,
)
from test_ask_draft_turns import held, run  # noqa: F401 — fixtures are used by name
from test_ask_orchestrator import CRED, ai, answer, app, call  # noqa: F401

Q = ask_orchestrator.QUEUE_MAX


async def send(cid: str, text: str, turn_id: str | None = None, **kw):
    tid = turn_id or str(uuid.uuid4())
    task, view = await ask_orchestrator.begin(cid, tid, text, working_keys=set(), **kw)
    return tid, task, view


async def started(cid: str, tid: str) -> asyncio.Task | None:
    fut = ask_orchestrator.when_started(cid, tid)
    assert fut is not None, "the turn is not waiting here"
    return await asyncio.wait_for(fut, 15)


def statuses(cid: str, tid: str) -> list[str]:
    return [
        r["status"] for r in ask_store.read(cid) if r["type"] == "status" and r["turn_id"] == tid
    ]


def view_of(cid: str, tid: str) -> dict:
    views = ask_orchestrator.turn_views(ask_store.read(cid))
    return next(v for v in views if v["turn_id"] == tid)


async def idle(cid: str) -> None:
    async with asyncio.timeout(20):
        while cid in ask_orchestrator._RUNNING or ask_orchestrator._QUEUE.get(cid):
            await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _clean():
    # A test that ends with a turn still held leaves its entry behind (its loop is gone).
    ask_orchestrator._RUNNING.clear()
    yield
    ask_orchestrator._RUNNING.clear()
    ask_orchestrator._QUEUE.clear()
    ask_orchestrator._NO_SUCCESSOR.clear()
    ask_orchestrator._STARTS.clear()


# ---- the queue ---------------------------------------------------------------------------------


def test_a_second_and_third_message_queue_and_run_in_order_after_the_first(held):
    async def scenario():
        script = held(answer("one"), answer("two"), answer("three"))
        cid = ask_store.create()
        t1, task1, _ = await send(cid, "first")
        await asyncio.wait_for(script.reached.wait(), 15)
        # Admitted while the running turn HOLDS its cross-process lock: admission takes none.
        assert ask_store.try_turn_lock(cid) is None
        t2, task2, v2 = await send(cid, "second")
        t3, task3, v3 = await send(cid, "third")
        assert task2 is None and task3 is None
        assert (v2["status"], v3["status"]) == ("queued", "queued")
        assert ask_orchestrator.queued(cid) == [t2, t3]
        assert script.bodies == []  # the endpoint holds the first; the others were not asked
        w2, w3 = ask_orchestrator.when_started(cid, t2), ask_orchestrator.when_started(cid, t3)
        script.go.set()
        assert (await task1)["answer"] == "one"
        assert (await (await asyncio.wait_for(w2, 15)))["answer"] == "two"
        assert (await (await asyncio.wait_for(w3, 15)))["answer"] == "three"
        await idle(cid)
        return cid, script, (t1, t2, t3)

    cid, script, (t1, t2, t3) = run(scenario())
    assert statuses(cid, t2) == ["queued", "pending", "done"]
    assert statuses(cid, t3) == ["queued", "pending", "done"]
    # Each queued turn replays what settled before it STARTED, in order, and nothing that was
    # still waiting behind it.
    said = [[m["content"] for m in b["messages"] if m["role"] != "system"] for b in script.bodies]
    assert said[1][-3:] == ["first", "one", "second"]
    assert said[2][-5:] == ["first", "one", "second", "two", "third"]
    v = view_of(cid, t3)
    assert v["asked_at"] < v["started_at"] <= v["settled_at"]
    assert v["elapsed_ms"] == int((v["settled_at"] - v["started_at"]) * 1000)
    assert ask_store.try_turn_lock(cid) is not None  # the handed-over lock was released


def test_the_queue_cap_refuses_the_next_with_a_reason_and_stores_nothing(held):
    async def scenario():
        held(answer("never"))
        cid = ask_store.create()
        await send(cid, "running")
        for i in range(Q):
            _, _, v = await send(cid, f"queued {i}")
            assert v["status"] == "queued"
        before = ask_store.read(cid)
        with pytest.raises(ask_orchestrator.TurnError) as e:
            await send(cid, "one too many")
        assert e.value.status == 409 and e.value.detail == ask_orchestrator.QUEUE_FULL
        assert e.value.extra == {"queue_full": True}
        assert ask_store.read(cid) == before and len(ask_orchestrator.queued(cid)) == Q

    run(scenario())


def test_two_messages_racing_for_the_last_slot_one_queues_one_is_refused(held):
    async def scenario():
        held(answer("never"))
        cid = ask_store.create()
        await send(cid, "running")
        for i in range(Q - 1):
            await send(cid, f"queued {i}")
        got = await asyncio.gather(
            send(cid, "racer a"), send(cid, "racer b"), return_exceptions=True
        )
        refused = [g for g in got if isinstance(g, ask_orchestrator.TurnError)]
        admitted = [g for g in got if not isinstance(g, BaseException)]
        assert len(refused) == 1 and refused[0].detail == ask_orchestrator.QUEUE_FULL
        assert len(admitted) == 1 and admitted[0][2]["status"] == "queued"
        views = ask_orchestrator.turn_views(ask_store.read(cid))
        assert [v["status"] for v in views] == ["pending"] + ["queued"] * Q
        assert len({v["turn_id"] for v in views}) == Q + 1  # nothing duplicated
        return cid

    run(scenario())


def test_the_same_slot_is_reserved_under_the_store_lock_also_across_threads():
    """`append_if` decides and writes under the file's own lock: two admissions that both
    looked before either wrote cannot both take the last slot."""
    cid = ask_store.create()
    seen: list[int] = []

    def decide(records):
        n = sum(1 for r in records if r["type"] == "status")
        seen.append(n)
        if n >= 1:
            raise ask_orchestrator.TurnError(409, ask_orchestrator.QUEUE_FULL)
        time.sleep(0.05)  # a slow decider: the other one must wait for the write
        return [{"type": "status", "turn_id": str(uuid.uuid4()), "status": "queued"}]

    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        futs = [pool.submit(ask_store.append_if, cid, decide) for _ in range(2)]
        done = [f.exception() for f in futs]
    assert sorted(seen) == [0, 1]
    assert sum(1 for d in done if isinstance(d, ask_orchestrator.TurnError)) == 1
    assert sum(1 for r in ask_store.read(cid) if r["type"] == "status") == 1


def test_a_cut_stream_retried_with_the_same_id_returns_the_queued_turn(held):
    async def scenario():
        script = held(answer("one"), answer("two"))
        cid = ask_store.create()
        _, task1, _ = await send(cid, "first")
        await asyncio.wait_for(script.reached.wait(), 15)
        t2, _, first = await send(cid, "second")
        before = ask_store.read(cid)
        _, task, again = await send(cid, "second", turn_id=t2)
        assert task is None and again["status"] == "queued" and again["turn_id"] == t2
        assert ask_store.read(cid) == before and ask_orchestrator.queued(cid) == [t2]
        with pytest.raises(ask_orchestrator.TurnError) as e:
            await send(cid, "something else", turn_id=t2)
        assert e.value.status == 409 and "different question" in e.value.detail
        # Once it runs, the same id names the running turn; once settled, its answer.
        w2 = ask_orchestrator.when_started(cid, t2)
        script.go.set()
        await task1
        running = await asyncio.wait_for(w2, 15)
        _, task, _ = await send(cid, "second", turn_id=t2)
        assert task is None or task is running
        assert (await running)["answer"] == "two"
        _, task, settled = await send(cid, "second", turn_id=t2)
        assert task is None and settled["answer"] == "two"
        assert len(script.bodies) == 2  # a repeat never runs the model again

    run(scenario())


def test_withdraw_takes_a_queued_question_back_and_it_never_runs(held):
    async def scenario():
        script = held(answer("one"), answer("three"))
        cid = ask_store.create()
        _, task1, _ = await send(cid, "first")
        await asyncio.wait_for(script.reached.wait(), 15)
        t2, _, _ = await send(cid, "second")
        t3, _, _ = await send(cid, "third")
        w2, w3 = ask_orchestrator.when_started(cid, t2), ask_orchestrator.when_started(cid, t3)
        assert await ask_orchestrator.withdraw(cid, t2) is True
        assert await ask_orchestrator.withdraw(cid, t2) is False  # once
        assert await ask_orchestrator.withdraw(cid, str(uuid.uuid4())) is False
        assert await w2 is None
        script.go.set()
        await task1
        assert (await (await asyncio.wait_for(w3, 15)))["answer"] == "three"
        await idle(cid)
        return cid, script, t2

    cid, script, t2 = run(scenario())
    assert statuses(cid, t2) == ["queued", "withdrawn"]
    # The withdrawn question was never put to HQ, not even as history.
    # (the conversation's own messages: a system prompt may use the word)
    said = [m for b in script.bodies for m in b["messages"] if m["role"] != "system"]
    assert said and all("second" not in json.dumps(m) for m in said)


@pytest.mark.parametrize("first", ["withdraw", "settle", "together"])
def test_withdraw_racing_the_start_has_one_outcome_never_both(held, first):
    async def scenario():
        script = held(answer("one"), answer("two"))
        cid = ask_store.create()
        _, task1, _ = await send(cid, "first")
        await asyncio.wait_for(script.reached.wait(), 15)
        t2, _, _ = await send(cid, "second")
        w2 = ask_orchestrator.when_started(cid, t2)
        if first == "withdraw":
            took = await ask_orchestrator.withdraw(cid, t2)
            script.go.set()
            await task1
        elif first == "settle":
            script.go.set()
            await task1
            took = await ask_orchestrator.withdraw(cid, t2)
        else:
            script.go.set()
            took, _ = await asyncio.gather(ask_orchestrator.withdraw(cid, t2), task1)
        task2 = await asyncio.wait_for(w2, 15)
        if task2 is not None:
            await task2
        await idle(cid)
        return cid, t2, took, task2, script

    cid, t2, took, task2, script = run(scenario())
    got = statuses(cid, t2)
    if took:
        assert task2 is None and got == ["queued", "withdrawn"] and len(script.bodies) == 1
    else:
        assert task2 is not None and got == ["queued", "pending", "done"]
    assert ("withdrawn" in got) != ("pending" in got)
    assert took == (first == "withdraw") or first == "together"


def test_stop_stops_the_running_turn_and_leaves_the_queue(held):
    async def scenario():
        script = held(answer("never"), answer("two"))
        cid = ask_store.create()
        t1, task1, _ = await send(cid, "first")
        await asyncio.wait_for(script.reached.wait(), 15)
        t2, _, _ = await send(cid, "second")
        w2 = ask_orchestrator.when_started(cid, t2)
        assert await ask_orchestrator.stop(cid, t1) is True
        assert (await task1)["status"] == "stopped"
        task2 = await asyncio.wait_for(w2, 15)
        script.go.set()
        assert (await task2)["status"] == "done"
        return cid, t1, t2

    cid, t1, t2 = run(scenario())
    assert statuses(cid, t1) == ["pending", "stopped"]
    assert statuses(cid, t2) == ["queued", "pending", "done"]


def test_stop_all_stops_every_running_turn_and_withdraws_every_queue(held):
    async def scenario():
        script = held(answer("never"))
        a, b = ask_store.create(), ask_store.create()
        ta, task_a, _ = await send(a, "in a")
        tb, task_b, _ = await send(b, "in b")
        qa, _, _ = await send(a, "behind a")
        qb, _, _ = await send(b, "behind b")
        async with asyncio.timeout(15):
            while not all(ask_orchestrator._RUNNING[c].turn.inflight for c in (a, b)):
                await asyncio.sleep(0.01)
        done = await ask_orchestrator.stop_all()
        assert done == {"stopped": 2, "withdrawn": 2}
        assert (await task_a)["status"] == "stopped" and (await task_b)["status"] == "stopped"
        await idle(a)
        await idle(b)
        assert not ask_orchestrator._NO_SUCCESSOR
        # The next message is a new request: it runs.
        script.go.set()
        _, task, _ = await send(a, "afterwards")
        assert (await task)["status"] == "done"
        return (a, ta, qa), (b, tb, qb), script

    (a, ta, qa), (b, tb, qb), script = run(scenario())
    for cid, running, waiting in ((a, ta, qa), (b, tb, qb)):
        assert statuses(cid, running) == ["pending", "stopped"]
        assert statuses(cid, waiting) == ["queued", "withdrawn"]
    assert len(script.bodies) == 1  # only "afterwards" was ever answered


def test_stop_all_racing_a_settle_starts_no_successor(held):
    """The mark is set SYNCHRONOUSLY, before the stop flag: a turn that settles from that
    moment on starts nothing behind it, even though Stop all has awaited nothing yet."""

    async def scenario():
        script = held(answer("never"))
        cid = ask_store.create()
        t1, task1, _ = await send(cid, "first")
        await asyncio.wait_for(script.reached.wait(), 15)
        t2, _, _ = await send(cid, "second")
        assert ask_orchestrator.request_stop_all() == [cid]  # no await: the fence is up
        await task1
        await idle(cid)
        return cid, t1, t2, script

    cid, t1, t2, script = run(scenario())
    assert statuses(cid, t1) == ["pending", "stopped"]
    assert statuses(cid, t2) == ["queued", "withdrawn"]
    assert script.bodies == []


def test_stop_all_during_the_start_of_the_next_turn_lets_it_ask_nothing(held, monkeypatch):
    """Stop all landing while the successor's start is being written: it starts already
    stopped, makes no model request and settles `stopped`."""

    async def scenario():
        script = held(answer("one"), answer("never asked"))
        cid = ask_store.create()
        _, task1, _ = await send(cid, "first")
        await asyncio.wait_for(script.reached.wait(), 15)
        t2, _, _ = await send(cid, "second")
        w2 = ask_orchestrator.when_started(cid, t2)
        real = ask_orchestrator._new_turn

        async def new_turn(*a, **kw):
            turn = await real(*a, **kw)
            if turn.turn_id == t2:
                ask_orchestrator.request_stop_all()
            return turn

        monkeypatch.setattr(ask_orchestrator, "_new_turn", new_turn)
        script.go.set()
        assert (await task1)["answer"] == "one"
        assert (await (await asyncio.wait_for(w2, 15)))["status"] == "stopped"
        await idle(cid)
        return cid, t2, script

    cid, t2, script = run(scenario())
    assert statuses(cid, t2) == ["queued", "pending", "stopped"]
    assert len(script.bodies) == 1


def test_a_queued_turn_after_a_restart_is_not_started_and_never_runs(ai):
    async def scenario():
        script = ai(answer("fresh"))
        cid = ask_store.create()
        t1, t2 = str(uuid.uuid4()), str(uuid.uuid4())
        qd = ask_store.question_digest("x")
        # What the last process left: a turn that was running and one queued behind it.
        ask_store.append(
            cid,
            {"type": "user", "turn_id": t1, "text": "was running", "qd": qd},
            {"type": "status", "turn_id": t1, "status": "pending"},
            {"type": "user", "turn_id": t2, "text": "was queued", "qd": qd},
            {"type": "status", "turn_id": t2, "status": "queued"},
        )
        await ask_orchestrator.read_conversation(cid)
        assert statuses(cid, t1) == ["pending", "failed"]
        assert statuses(cid, t2) == ["queued", "not_started"]
        assert ask_orchestrator.when_started(cid, t2) is None
        assert await ask_orchestrator.withdraw(cid, t2) is False
        # The same id again returns it as it is; nothing starts it.
        _, task, view = await send(cid, "x", turn_id=t2)
        assert task is None and view["status"] == "not_started"
        # A re-send is a new turn, and the unstarted question is not replayed to HQ.
        _, task, _ = await send(cid, "was queued")
        assert (await task)["answer"] == "fresh"
        return script

    script = run(scenario())
    assert len(script.bodies) == 1
    said = [m["content"] for m in script.bodies[0]["messages"] if m["role"] != "system"]
    assert said.count("was queued") == 1 and said[-1] == "was queued"


def test_only_an_operators_request_queues_a_continuation_and_a_draft_never_do(held):
    async def scenario():
        held(answer("never"))
        cid = ask_store.create()
        await send(cid, "running")
        for kw in ({"via": "card"}, {"via": "call", "draft": True}):
            with pytest.raises(ask_orchestrator.TurnError) as e:
                await send(cid, "not the operator's typed or spoken request", **kw)
            assert e.value.status == 409 and "already running" in e.value.detail
        assert ask_orchestrator.queued(cid) == []
        # A spoken question (not a draft) is the operator's: it queues like a typed one.
        _, task, view = await send(cid, "spoken", via="call")
        assert task is None and (view["status"], view["via"]) == ("queued", "call")

    run(scenario())


def test_a_queued_turn_keeps_its_requests_identity_in_memory_only(held):
    marker = object()

    async def scenario():
        script = held(answer("one"), answer("two"))
        cid = ask_store.create()
        _, task1, _ = await send(cid, "first")
        await asyncio.wait_for(script.reached.wait(), 15)
        t2, _, _ = await send(cid, "second", operator=marker)
        w2 = ask_orchestrator.when_started(cid, t2)
        assert ask_orchestrator._QUEUE[cid][0].operator is marker
        script.go.set()
        await task1
        task2 = await asyncio.wait_for(w2, 15)
        assert ask_orchestrator._RUNNING[cid].turn.operator is marker
        await task2
        return cid

    cid = run(scenario())
    stored = (ask_store.root() / f"{cid}.jsonl").read_text()
    assert "operator" not in stored and "cookie" not in stored.lower()


def test_a_queued_turn_can_be_followed_without_a_gap_from_admission_to_settle(held):
    """`follow` (review 8407 on #2125): a turn that will run, runs or ran is never reported as
    one that never started — while it waits, while its start is being written (it has left the
    queue and is not registered as running yet), while it runs, and after it settled."""

    async def scenario():
        script = held(answer("one"), answer("two"))
        cid = ask_store.create()
        _, task1, _ = await send(cid, "first")
        await asyncio.wait_for(script.reached.wait(), 15)
        t2, _, _ = await send(cid, "second")
        handle = ask_orchestrator.when_started(cid, t2)
        assert handle is not None and not handle.done()
        seen: list = []
        real = ask_orchestrator._new_turn

        async def new_turn(*a, **kw):
            # INSIDE the start: out of the queue, not yet the running turn.
            assert ask_orchestrator.queued(cid) == [] and cid not in ask_orchestrator._RUNNING
            seen.append(ask_orchestrator.when_started(cid, t2))
            return await real(*a, **kw)

        ask_orchestrator._new_turn = new_turn
        try:
            script.go.set()
            await task1
            task2, view = await ask_orchestrator.follow(cid, t2)
        finally:
            ask_orchestrator._new_turn = real
        assert seen == [handle]  # the same handle, still resolvable mid-start
        assert view is None and task2 is ask_orchestrator._RUNNING[cid].task
        assert (await task2)["answer"] == "two"
        await idle(cid)
        # Settled: the registry has let go, the handle still holds the task, the store answers.
        assert ask_orchestrator.when_started(cid, t2) is None
        assert (await ask_orchestrator.follow(cid, t2, handle))[0] is task2
        late_task, late = await ask_orchestrator.follow(cid, t2)
        assert late_task is None and (late["status"], late["answer"]) == ("done", "two")
        # Withdrawn: followed to its stored outcome, never a task.
        script.go.clear()
        _, task3, _ = await send(cid, "third")
        t4, _, _ = await send(cid, "fourth")
        h4 = ask_orchestrator.when_started(cid, t4)
        await ask_orchestrator.withdraw(cid, t4)
        gone_task, gone = await ask_orchestrator.follow(cid, t4, h4)
        assert gone_task is None and gone["status"] == "withdrawn"
        assert await ask_orchestrator.follow(cid, str(uuid.uuid4())) == (None, None)
        script.go.set()
        await task3
        assert not ask_orchestrator._STARTS

    run(scenario())


# ---- what a queued turn replays (#2103 on #2137's history) --------------------------------------


class Gated:
    """The endpoint: request ``i`` is answered with ``replies[i]`` once ``gates[i]`` is set."""

    def __init__(self, *replies: str):
        self.replies = replies
        self.gates = [asyncio.Event() for _ in replies]
        self.arrived = [asyncio.Event() for _ in replies]
        self.bodies: list[dict] = []

    async def handler(self, request: httpx.Request) -> httpx.Response:
        i = len(self.bodies)
        self.bodies.append(json.loads(request.content))
        self.arrived[i].set()
        await self.gates[i].wait()
        msg = {"role": "assistant", "content": self.replies[i]}
        return httpx.Response(200, json={"choices": [{"message": msg, "finish_reason": "stop"}]})

    def asked(self, i: int) -> list[str]:
        """Request ``i``'s conversation: every user and assistant message, in order."""
        return [str(m["content"]) for m in self.bodies[i]["messages"] if m["role"] != "system"]


@pytest.fixture
def gated(ai, monkeypatch):
    from agent_sessions import review

    def install(*replies: str) -> Gated:
        ai()
        script = Gated(*replies)
        monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(script.handler))
        return script

    return install


def test_a_queued_turn_replays_the_turn_it_waited_behind_question_and_answer(gated):
    """A user → B user (queued) → A's answer: B's history must not stop at B's own question,
    which is stored BEFORE A's answer. B is asked with A's question AND answer, then its own
    question once. (The real `_run_turn`; the endpoint records the request.)"""

    async def scenario():
        script = gated("alpha-answer", "beta-answer")
        cid = ask_store.create()
        _, task_a, _ = await send(cid, "alpha-question")
        await asyncio.wait_for(script.arrived[0].wait(), 15)
        tb, _, _ = await send(cid, "beta-question")
        wb = ask_orchestrator.when_started(cid, tb)
        # The order the queue leaves in the file: B's question before A's answer.
        script.gates[0].set()
        await task_a
        kinds = [(r["type"], r.get("text")) for r in ask_store.read(cid) if r["type"] != "status"]
        assert [k for k in kinds if k[0] in ("user", "assistant")] == [
            ("user", "alpha-question"),
            ("user", "beta-question"),
            ("assistant", "alpha-answer"),
        ]
        task_b = await asyncio.wait_for(wb, 15)
        await asyncio.wait_for(script.arrived[1].wait(), 15)
        script.gates[1].set()
        await task_b
        return script

    script = run(scenario())
    assert script.asked(1) == ["alpha-question", "alpha-answer", "beta-question"]


def test_three_deep_the_last_queued_turn_replays_both_before_it(gated):
    async def scenario():
        script = gated("alpha-answer", "beta-answer", "gamma-answer")
        cid = ask_store.create()
        _, task_a, _ = await send(cid, "alpha-question")
        await asyncio.wait_for(script.arrived[0].wait(), 15)
        tb, _, _ = await send(cid, "beta-question")
        tc, _, _ = await send(cid, "gamma-question")
        wc = ask_orchestrator.when_started(cid, tc)
        for gate in script.gates:
            gate.set()
        await task_a
        await (await asyncio.wait_for(wc, 15))
        return script

    script = run(scenario())
    assert script.asked(1) == ["alpha-question", "alpha-answer", "beta-question"]
    assert script.asked(2) == [
        "alpha-question", "alpha-answer", "beta-question", "beta-answer", "gamma-question",
    ]  # fmt: skip


def test_a_withdrawn_question_is_not_replayed_to_the_turn_behind_it(gated):
    async def scenario():
        script = gated("alpha-answer", "gamma-answer")
        cid = ask_store.create()
        _, task_a, _ = await send(cid, "alpha-question")
        await asyncio.wait_for(script.arrived[0].wait(), 15)
        tb, _, _ = await send(cid, "beta-question")
        tc, _, _ = await send(cid, "gamma-question")
        wc = ask_orchestrator.when_started(cid, tc)
        assert await ask_orchestrator.withdraw(cid, tb)
        for gate in script.gates:
            gate.set()
        await task_a
        await (await asyncio.wait_for(wc, 15))
        return script

    script = run(scenario())
    assert script.asked(1) == ["alpha-question", "alpha-answer", "gamma-question"]


def test_a_question_that_never_started_and_a_sibling_still_running_are_not_replayed(gated):
    """After a restart: a `not_started` question, and a `pending` one another process may still
    be running, sit in the file around settled turns. Neither has an outcome: neither is
    replayed; the settled turn after them is."""

    async def scenario():
        script = gated("delta-answer")
        cid = ask_store.create()
        qd = ask_store.question_digest("x")
        ids = [str(uuid.uuid4()) for _ in range(4)]

        def user(i: int, text: str) -> dict:
            return {"type": "user", "turn_id": ids[i], "text": text, "qd": qd}

        def status(i: int, value: str) -> dict:
            return {"type": "status", "turn_id": ids[i], "status": value}

        ask_store.append(
            cid,
            user(0, "alpha-question"),
            status(0, "pending"),
            user(1, "never-started-question"),
            status(1, "queued"),
            user(2, "sibling-question"),  # still `pending`: no outcome on record
            {"type": "assistant", "turn_id": ids[0], "text": "alpha-answer"},
            status(0, "done"),
            status(1, "not_started"),
            status(2, "pending"),
        )
        records = ask_store.read(cid)
        summary, pairs, _ = ask_orchestrator._exchanges(records, str(uuid.uuid4()))
        assert summary is None and [(q, a) for _t, q, a in pairs] == [
            ("alpha-question", "alpha-answer")
        ]
        # …and through a real turn: the interrupted sibling is settled `failed` at the read
        # (its fixed note is replayed, as before #2103); the unstarted question never is.
        _, task, _ = await send(cid, "delta-question")
        await asyncio.wait_for(script.arrived[0].wait(), 15)
        script.gates[0].set()
        await task
        return script

    script = run(scenario())
    asked = script.asked(0)
    assert "never-started-question" not in asked
    assert asked[:2] == ["alpha-question", "alpha-answer"] and asked[-1] == "delta-question"
    assert asked[2:-1] == ["sibling-question", ask_orchestrator.FAILED_TURN_NOTE]


def test_a_queued_turn_started_after_a_compaction_sees_the_summary_and_is_asked_once(gated):
    """A summary's prefix may include a queued turn's question (it is stored while the turn
    ahead of it runs). That turn, started later, opens with the summary, replays the exchanges
    after it, and is asked its own question exactly once."""

    async def scenario():
        script = gated("alpha-answer", "beta-answer", "gamma-answer", "delta-answer")
        cid = ask_store.create()
        _, task_a, _ = await send(cid, "alpha-question")
        await asyncio.wait_for(script.arrived[0].wait(), 15)
        script.gates[0].set()
        await task_a
        tb, task_b, _ = await send(cid, "beta-question")
        await asyncio.wait_for(script.arrived[1].wait(), 15)
        tc, _, _ = await send(cid, "gamma-question")
        td, _, _ = await send(cid, "delta-question")
        wc = ask_orchestrator.when_started(cid, tc)
        wd = ask_orchestrator.when_started(cid, td)
        script.gates[1].set()
        await task_b
        task_c = await asyncio.wait_for(wc, 15)
        await asyncio.wait_for(script.arrived[2].wait(), 15)
        # C runs. A summary of everything up to and including B is published now: its bound
        # prefix ends at B's last record, AFTER the questions of C and D (both stored earlier).
        records = ask_store.read(cid)
        n = max(i for i, r in enumerate(records) if r.get("turn_id") == tb) + 1
        inside = [r.get("text") for r in records[:n] if r["type"] == "user"]
        assert "gamma-question" in inside and "delta-question" in inside
        written = ask_store.append_summary(
            cid,
            {
                "upto": tb,
                "n": n,
                "digest": ask_store.records_digest(records[:n]),
                "text": "SUMMARY-OF-ALPHA-AND-BETA",
            },
        )
        assert written is not None
        script.gates[2].set()
        await task_c
        task_d = await asyncio.wait_for(wd, 15)
        await asyncio.wait_for(script.arrived[3].wait(), 15)
        script.gates[3].set()
        await task_d
        return script

    script = run(scenario())
    asked = script.asked(3)  # D: queued before the compaction, started after it
    assert "SUMMARY-OF-ALPHA-AND-BETA" in asked[0]
    # (the summary is a user message; a fixed acknowledgement keeps the roles alternating)
    assert asked[-3:] == ["gamma-question", "gamma-answer", "delta-question"] and len(asked) == 5
    joined = "\n".join(asked)
    assert joined.count("delta-question") == 1
    assert "alpha-answer" not in joined and "beta-answer" not in joined  # folded into the summary
    # C had started before the summary existed: it replayed the exchanges themselves.
    assert script.asked(2) == [
        "alpha-question", "alpha-answer", "beta-question", "beta-answer", "gamma-question",
    ]  # fmt: skip


def test_the_webs_queue_size_is_the_servers():
    src = (Path(__file__).parents[1] / "web/src/components/ask/askQueue.ts").read_text()
    assert f"export const ASK_QUEUE_MAX = {Q};" in src


# ---- no fence moves ----------------------------------------------------------------------------


def test_a_queued_turn_has_the_running_turns_tools_kinds_and_budgets(held, monkeypatch):
    """This slice adds no authority: the turn a queue starts is `_Turn` + `_settle`, the same
    as any turn, so its tool list, each tool's kind and every budget are the running turn's."""
    budgets = dict(ask_orchestrator._BUDGETS)
    kinds = {n: t.kind for n, t in ask_orchestrator.TOOLS.items()}

    async def scenario():
        script = held(answer("one"), answer("two"))
        cid = ask_store.create()
        _, task1, _ = await send(cid, "first")
        await asyncio.wait_for(script.reached.wait(), 15)
        t2, _, _ = await send(cid, "second")
        w2 = ask_orchestrator.when_started(cid, t2)
        script.go.set()
        await task1
        await (await asyncio.wait_for(w2, 15))
        return script

    script = run(scenario())
    assert script.bodies[0]["tools"] == script.bodies[1]["tools"]
    assert ask_orchestrator._BUDGETS == budgets
    assert {n: t.kind for n, t in ask_orchestrator.TOOLS.items()} == kinds


def test_a_queued_turn_stages_a_card_and_starts_nothing(held, monkeypatch):
    """A proposal a queued turn makes is a card like any other: recorded, and nothing runs
    until the operator decides it."""
    ran: list[str] = []

    async def propose(turn, args):
        ran.append("staged")
        await ask_orchestrator._turn_append(
            turn,
            {
                "type": "proposal",
                "turn_id": turn.turn_id,
                "kind": "session",
                "action_id": uuid.uuid4().hex,
                "title": "p",
                "agent": "a",
                "text": "brief",
            },
        )
        return {"staged": True}, {"count": 1}

    spec = ask_orchestrator._spec("stage_test", "a test card", {}, [])
    monkeypatch.setitem(
        ask_orchestrator.TOOLS,
        "stage_test",
        ask_orchestrator.Tool("stage_test", "proposal", spec, propose),
    )

    async def scenario():
        script = held(answer("one"), call("stage_test"), answer("staged it"))
        cid = ask_store.create()
        _, task1, _ = await send(cid, "first")
        await asyncio.wait_for(script.reached.wait(), 15)
        t2, _, _ = await send(cid, "start a session")
        w2 = ask_orchestrator.when_started(cid, t2)
        script.go.set()
        await task1
        view = await (await asyncio.wait_for(w2, 15))
        return cid, view

    cid, view = run(scenario())
    assert ran == ["staged"] and len(view["proposals"]) == 1
    # The card's entry never landed in the decide sidecar: there is nothing a tap could start,
    # and nothing started on its own.
    assert view["proposals"][0]["state"] == "gone" and not view["proposals"][0]["can_start"]


def test_nothing_in_this_change_can_act_or_grant():
    """No authority setting, no D13 grant, no act-without-a-tap path (#2070 keeps those for
    their own children): the tasks module reaches no store but the conversations and the bell,
    and the queue's own code names no delivery, launch, decide or grant path."""
    src = Path(inspect.getsourcefile(hq_tasks)).read_text()
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.level:
            imported |= {a.name for a in node.names}
    assert imported == {"ask_orchestrator", "ask_store", "notifications"}
    queue_code = "".join(
        inspect.getsource(f)
        for f in (
            ask_orchestrator._Queued,
            ask_orchestrator._enqueue,
            ask_orchestrator._start_next,
            ask_orchestrator._withdraw_locked,
            ask_orchestrator._new_turn,
            ask_orchestrator._launch,
            ask_orchestrator.withdraw,
            ask_orchestrator.request_stop_all,
            ask_orchestrator.stop_all,
        )
    )
    for word in (
        "ask_proposals",
        "asgi_call",
        "deliver",
        "grant",
        "authority",
        "prefs.",
        "bypass",
        ".decide(",
        "dispatch",
    ):
        assert word not in queue_code, word
    from agent_sessions import prefs

    names = [k.lower() for k in vars(prefs)]
    assert not [k for k in names if "hq_authority" in k or "hq_task" in k or "d13" in k]


# ---- being told --------------------------------------------------------------------------------


@pytest.fixture
def notices(monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_HQ_TASK_NOTICES", "1")
    monkeypatch.setattr(hq_tasks, "SETTLE_NOTIFY_GRACE_S", 0.05)


def bell() -> list[dict]:
    return notifications.listing()["notifications"]


async def settle_notices() -> None:
    async with asyncio.timeout(10):
        while hq_tasks._NOTICES:
            await asyncio.sleep(0.01)


CANARY = "the-answer-canary-7Q"


def test_an_unseen_settle_is_announced_once_with_a_fixed_title_and_no_model_text(ai, notices):
    async def scenario():
        ai(answer(f"{CANARY} is what the model wrote"))
        cid = ask_store.create()
        tid, task, _ = await send(cid, "Where is the release?\nand more detail")
        await task
        assert bell() == []  # not before the grace
        await settle_notices()
        return cid, tid

    cid, tid = run(scenario())
    rows = bell()
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "HQ answered"
    assert row["reason"] == "Where is the release? and more detail"  # the conversation's name
    assert row["action_id"] == f"hq-turn:{cid}:{tid}" and row["hq_conversation"] == cid
    assert (row["session_id"], row["project"], row["engine"]) == ("", "", "hq")
    assert CANARY not in notifications._notifications_path().read_text()
    # The bell row's link and the push URL open the conversation.
    assert notifications._link(row) == f"/hq/c/{cid}"
    assert notifications._link(row, "https://box.example") == f"https://box.example/hq/c/{cid}"
    # Asked again (a second settle hook, a reconcile): never a second row.
    assert hq_tasks.notify_unseen(cid, tid) == [] and hq_tasks.notify_unseen(cid) == []
    assert len(bell()) == 1


@pytest.mark.parametrize(
    ("reply", "stop", "title"),
    [
        (answer("done"), False, "HQ answered"),
        (call("ask_operator", {"question": "Which one?", "options": ["a", "b"]}), False,
         "HQ has a question"),
        (answer("never"), True, "HQ stopped"),
        (httpx.Response(500, text="boom"), False, "HQ could not answer"),
    ],
)  # fmt: skip
def test_each_outcome_has_its_fixed_title(held, ai, notices, reply, stop, title):
    async def scenario():
        cid = ask_store.create()
        if stop:
            script = held(reply)
            tid, task, _ = await send(cid, "a question")
            await asyncio.wait_for(script.reached.wait(), 15)
            await ask_orchestrator.stop(cid, tid)
        else:
            ai(reply)
            tid, task, _ = await send(cid, "a question")
        await task
        await settle_notices()

    run(scenario())
    assert [r["title"] for r in bell()] == [title]


def test_the_push_carries_only_the_fixed_title_and_the_conversations_url(ai, notices, monkeypatch):
    """The third-party boundary (#726): the push is the fixed title and the link. The
    conversation's name is the Commander's own question, so it stays in the bell row and never
    leaves in a push; neither does anything HQ answered. The REAL `fanout` builds the payload;
    only the send to the push service is replaced."""
    from agent_sessions import webpush

    payloads: list[bytes] = []
    monkeypatch.setattr(notifications, "list_subscriptions", lambda: [{"id": "d"}])
    real_read = notifications._read_strict
    subs = notifications._subs_path()
    monkeypatch.setattr(
        notifications,
        "_read_strict",
        lambda path: [{"endpoint": "e"}] if path == subs else real_read(path),
    )
    monkeypatch.setattr(webpush, "send", lambda sub, payload, **k: payloads.append(payload))
    asked = "Where is the marmalade release?"  # `marmalade`: the question's canary

    async def scenario():
        ai(answer(f"{CANARY}"))
        cid = ask_store.create()
        _, task, _ = await send(cid, f"{asked}\nand a second line of the question")
        await task
        await settle_notices()
        return cid

    cid = run(scenario())
    assert len(payloads) == 1
    assert json.loads(payloads[0]) == {"title": "HQ answered", "body": "", "url": f"/hq/c/{cid}"}
    raw = payloads[0].decode()
    for leaked in ("marmalade", "Where is the", "second line", CANARY):
        assert leaked not in raw
    # The bell row, in the app and behind the login, keeps the name.
    assert [r["reason"] for r in bell()] == [f"{asked} and a second line of the question"]


def test_a_settle_seen_within_the_grace_writes_no_row(ai, notices, monkeypatch):
    monkeypatch.setattr(hq_tasks, "SETTLE_NOTIFY_GRACE_S", 0.3)

    async def scenario():
        ai(answer("done"))
        cid = ask_store.create()
        tid, task, _ = await send(cid, "a question")
        view = await task
        # The browser showing the conversation marks the SETTLEMENT seen, as the console does.
        ask_follow.mark_seen(cid, view["settled_at"])
        await settle_notices()
        return cid

    cid = run(scenario())
    assert bell() == []
    assert hq_tasks.notify_unseen(cid) == []  # and a later reconcile writes none either


def test_seeing_a_question_while_it_was_queued_or_running_acknowledges_nothing(held, notices):
    async def scenario():
        script = held(answer("one"), answer("two"))
        cid = ask_store.create()
        _, task1, _ = await send(cid, "first")
        await asyncio.wait_for(script.reached.wait(), 15)
        t2, _, _ = await send(cid, "second")
        w2 = ask_orchestrator.when_started(cid, t2)
        # The conversation is on screen NOW, showing the first turn running and the second
        # queued: everything displayed so far is seen.
        ask_follow.mark_seen(cid, time.time())
        await asyncio.sleep(0.02)
        script.go.set()
        await task1
        await (await asyncio.wait_for(w2, 15))
        await settle_notices()
        return cid, t2

    cid, t2 = run(scenario())
    assert sorted(r["action_id"].rsplit(":", 1)[1] for r in bell()).count(t2) == 1
    assert len(bell()) == 2  # both answers landed after the last look


def test_the_row_is_retracted_when_the_settlement_is_seen(app, ai, notices):
    async def scenario():
        ai(answer("done"))
        cid = app.new()
        tid, task, _ = await send(cid, "a question")
        view = await task
        await settle_notices()
        return cid, view

    cid, view = run(scenario())
    assert len(bell()) == 1
    # Seen only up to BEFORE the settlement: the row stays.
    r = app.c.post(
        f"/api/ask/conversations/{cid}/seen",
        json={"through": view["settled_at"] - 0.001},
        headers=app.hdr,
    )
    assert r.status_code == 200 and len(bell()) == 1
    r = app.c.post(
        f"/api/ask/conversations/{cid}/seen",
        json={"through": view["settled_at"]},
        headers=app.hdr,
    )
    assert r.status_code == 200 and bell() == []
    assert app.get(cid)["seen_at"] == view["settled_at"]
    # Retracted, and never announced again.
    assert hq_tasks.notify_unseen(cid) == [] and bell() == []


def test_a_crash_between_the_settle_and_the_row_is_reconciled_once(ai, monkeypatch):
    """With the notice switched off the settle writes no row: exactly what a process that
    stopped inside the grace leaves behind."""

    async def scenario():
        ai(answer("done"))
        cid, old = ask_store.create(), ask_store.create()
        _, task, _ = await send(cid, "unseen and recent")
        await task
        # Settled long ago: outside the window, never announced.
        tid = str(uuid.uuid4())
        long_ago = time.time() - hq_tasks.RECONCILE_WINDOW_S - 60
        ask_store.append(
            old,
            {"type": "user", "turn_id": tid, "text": "old", "qd": "0" * 64, "ts": long_ago},
            {"type": "status", "turn_id": tid, "status": "pending", "ts": long_ago},
            {"type": "status", "turn_id": tid, "status": "done", "ts": long_ago},
        )
        assert bell() == []
        monkeypatch.setenv("AGENT_SESSIONS_HQ_TASK_NOTICES", "1")
        first = await hq_tasks.reconcile()
        second = await hq_tasks.reconcile()
        return cid, first, second

    cid, first, second = run(scenario())
    assert (first, second) == (1, 0)
    assert [r["hq_conversation"] for r in bell()] == [cid]
    # Dismissed by the Commander: a later reconcile does not bring it back.
    notifications.dismiss([r["id"] for r in bell()])
    assert run(hq_tasks.reconcile()) == 0 and bell() == []


def test_a_withdrawn_or_unstarted_turn_is_never_announced(held, notices):
    async def scenario():
        script = held(answer("never"))
        cid = ask_store.create()
        t1, task1, _ = await send(cid, "first")
        await asyncio.wait_for(script.reached.wait(), 15)
        await send(cid, "second")
        await ask_orchestrator.stop_all()
        await task1
        await settle_notices()
        return cid, t1

    cid, t1 = run(scenario())
    assert [r["action_id"] for r in bell()] == [f"hq-turn:{cid}:{t1}"]
    assert bell()[0]["title"] == "HQ stopped"


# ---- the list and its routes -------------------------------------------------------------------


def test_the_list_shows_running_queued_parked_and_recent_tasks(held, ai):
    async def scenario():
        cid_q = ask_store.create()
        ai(call("ask_operator", {"question": "Which one?", "options": ["a", "b"]}))
        _, task, _ = await send(cid_q, "Archive old docs sessions")
        await task
        script = held(answer("one"))
        cid = ask_store.create()
        t1, task1, _ = await send(cid, "Where is #1613?\nsecond line")
        await asyncio.wait_for(script.reached.wait(), 15)
        t2, _, _ = await send(cid, "And which sessions are blocked?")
        t3, _, _ = await send(cid, "to be withdrawn")
        await ask_orchestrator.withdraw(cid, t3)
        got = await asyncio.to_thread(hq_tasks.listing)
        script.go.set()
        await task1
        await idle(cid)
        after = await asyncio.to_thread(hq_tasks.listing)
        return cid, cid_q, (t1, t2, t3), got, after

    cid, cid_q, (t1, t2, t3), got, after = run(scenario())
    assert got["counts"] == {"running": 1, "queued": 1, "question": 1}
    rows = {r["turn_id"]: r for r in got["tasks"]}
    assert [r["state"] for r in got["tasks"]] == ["running", "queued", "question"]
    assert rows[t1]["question"] == "Where is #1613?"  # the first line only
    assert rows[t1]["conversation"] == "Where is #1613? second line"
    assert rows[t2]["state"] == "queued" and rows[t2]["cid"] == cid
    parked = got["tasks"][2]
    assert (parked["cid"], parked["question"]) == (cid_q, "Archive old docs sessions")
    assert [(r["turn_id"], r["state"]) for r in got["recent"]] == [(t3, "withdrawn")]
    assert after["counts"] == {"running": 0, "queued": 0, "question": 1}
    assert [r["state"] for r in after["recent"]] == ["answered", "answered", "withdrawn"]
    assert set(got["tasks"][0]) == {
        "cid", "turn_id", "state", "question", "via", "conversation", "steps", "step",
        "stopping", "asked_at", "started_at", "ended_at", "elapsed_ms",
    }  # fmt: skip


def test_the_tasks_route_returns_only_stored_first_lines_redacted_as_stored(app, held):
    async def scenario():
        script = held(answer("one"))
        cid = app.new()
        _, task1, _ = await send(cid, "first question")
        await asyncio.wait_for(script.reached.wait(), 15)
        await send(cid, f"deploy with {CRED}\nand a second line")
        transport = httpx.ASGITransport(app=app.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver", cookies=dict(app.c.cookies)
        ) as c:
            async with httpx.AsyncClient(
                transport=transport, base_url="https://testserver"
            ) as anon:
                assert (await anon.get("/api/hq/tasks")).status_code == 401
            r = await c.get("/api/hq/tasks")
        script.go.set()
        await task1
        await idle(cid)
        return r

    r = run(scenario())
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["queue_max"] == Q and body["counts"]["queued"] == 1
    assert CRED not in r.text and "second line" not in r.text
    assert [t["question"] for t in body["tasks"]] == ["first question", ask_store.WITHHELD]
    assert "answer" not in r.text


def test_the_stop_all_and_withdraw_routes_are_guarded_and_do_what_they_say(app, held):
    async def scenario():
        script = held(answer("never"))
        cid = app.new()
        t1, task1, _ = await send(cid, "first")
        await asyncio.wait_for(script.reached.wait(), 15)
        t2, _, _ = await send(cid, "second")
        t3, _, _ = await send(cid, "third")
        transport = httpx.ASGITransport(app=app.app)
        base = f"/api/ask/conversations/{cid}/turns"
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver", cookies=dict(app.c.cookies)
        ) as c:
            async with httpx.AsyncClient(
                transport=transport, base_url="https://testserver"
            ) as anon:
                for path in ("/api/hq/tasks/stop-all", f"{base}/{t2}/withdraw"):
                    assert (await anon.post(path, headers=app.hdr)).status_code == 401
            no_csrf = {"Origin": app.hdr["Origin"]}
            foreign = {**app.hdr, "Origin": "https://evil.example"}
            for path in ("/api/hq/tasks/stop-all", f"{base}/{t2}/withdraw"):
                assert (await c.post(path, headers=no_csrf)).status_code == 403
                assert (await c.post(path, headers=foreign)).status_code == 403
            assert ask_orchestrator.queued(cid) == [t2, t3] and not task1.done()
            assert (await c.post(f"{base}/nope/withdraw", headers=app.hdr)).status_code == 404
            unknown = await c.post(f"{base}/{uuid.uuid4()}/withdraw", headers=app.hdr)
            assert unknown.status_code == 404
            r = await c.post(f"{base}/{t2}/withdraw", headers=app.hdr)
            assert r.status_code == 200
            assert (r.json()["withdrawn"], r.json()["turn"]["status"]) == (True, "withdrawn")
            again = await c.post(f"{base}/{t2}/withdraw", headers=app.hdr)
            assert (again.json()["withdrawn"], again.json()["turn"]["status"]) == (
                False,
                "withdrawn",
            )
            # The running turn is not a queued one: Withdraw leaves it alone.
            r = await c.post(f"{base}/{t1}/withdraw", headers=app.hdr)
            assert r.json()["withdrawn"] is False and not task1.done()
            r = await c.post("/api/hq/tasks/stop-all", headers=app.hdr)
            assert r.status_code == 200
            body = r.json()
            assert (body["stopped"], body["withdrawn"]) == (1, 1)
            assert body["counts"] == {"running": 0, "queued": 0, "question": 0}
            # Idempotent: nothing left to stop.
            r = await c.post("/api/hq/tasks/stop-all", headers=app.hdr)
            assert (r.json()["stopped"], r.json()["withdrawn"]) == (0, 0)
        assert (await task1)["status"] == "stopped"
        return cid, t3

    cid, t3 = run(scenario())
    assert statuses(cid, t3) == ["queued", "withdrawn"]


def test_the_message_route_answers_a_queued_turn_and_names_a_full_queue(app, held):
    async def scenario():
        script = held(answer("never"))
        cid = app.new()
        _, task1, _ = await send(cid, "first")
        await asyncio.wait_for(script.reached.wait(), 15)
        transport = httpx.ASGITransport(app=app.app)
        async with httpx.AsyncClient(
            transport=transport, base_url="https://testserver", cookies=dict(app.c.cookies)
        ) as c:
            url = f"/api/ask/conversations/{cid}/message/stream"
            for i in range(Q):
                r = await c.post(
                    url, json={"turn_id": str(uuid.uuid4()), "text": f"q{i}"}, headers=app.hdr
                )
                lines = [json.loads(x) for x in r.text.splitlines()]
                assert r.status_code == 200 and len(lines) == 1
                assert lines[0]["type"] == "turn" and lines[0]["turn"]["status"] == "queued"
            r = await c.post(
                url, json={"turn_id": str(uuid.uuid4()), "text": "too many"}, headers=app.hdr
            )
            assert r.status_code == 409
            assert r.json() == {"detail": ask_orchestrator.QUEUE_FULL, "queue_full": True}
            body = (await c.get(f"/api/ask/conversations/{cid}")).json()
            assert [t["status"] for t in body["turns"]] == ["pending"] + ["queued"] * Q
        await ask_orchestrator.stop_all()
        await task1

    run(scenario())
