"""HQ's draft turns (#1845): a call's question starts before the Commander has surely finished.

A draft runs only the read tools, writes and shows nothing until it is committed, and a discard —
at any await — leaves every store as it was and frees the conversation at once. A commit stores
exactly what a normal turn would have."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import uuid
from pathlib import Path

import pytest

from agent_sessions import ask_orchestrator, ask_store, missions, template_secrets
from conftest import STORE_ENV_PATHS
from test_ask_orchestrator import Script, ai, answer, app, call, calls, found  # noqa: F401

#: Everything with an effect: each waits for the commit. Adding a tool means deciding which side
#: it is on — never by default.
GATED = {
    "ask_operator",
    "end_call",
    "propose_library",
    "propose_nudge",
    "propose_project_change",
    "propose_secret",
    "propose_session",
    "propose_session_change",
}

_VOLATILE = (
    "AGENT_SESSIONS_RUNTIME_DIR",
    "AGENT_SESSIONS_LOCK_DIR",
    "AGENT_SESSIONS_SCROLLBACK_DIR",
    # The footer's token count: a discarded draft's tokens WERE spent, so they are counted.
    "AGENT_SESSIONS_HQ_USAGE",
)


def _stores() -> dict[str, bytes]:
    """Every file of every store, by path. Turn-lock files are created empty by any turn and are
    not state; everything else must match to the byte."""
    out: dict[str, bytes] = {}
    for var in STORE_ENV_PATHS:
        if var in _VOLATILE:
            continue
        root = Path(os.environ[var])
        for p in [root] if root.is_file() else root.rglob("*") if root.is_dir() else []:
            if p.is_file() and ".locks" not in p.parts:
                out[str(p)] = p.read_bytes()
    return out


class Held(Script):
    """The endpoint answers the first ``free`` requests at once and holds the rest until ``go``."""

    def __init__(self, *replies, free: int = 0):
        super().__init__(*replies)
        self.free = free
        self.go = asyncio.Event()
        self.reached = asyncio.Event()

    async def handler(self, request):
        if len(self.bodies) >= self.free:
            self.reached.set()
            await self.go.wait()
        return await super().handler(request)


@pytest.fixture
def held(ai, monkeypatch):  # noqa: F811
    import httpx

    from agent_sessions import review

    def install(*replies, free: int = 0) -> Held:
        ai()  # the endpoint's settings
        script = Held(*replies, free=free)
        monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(script.handler))
        return script

    return install


def run(coro, timeout: float = 40.0):
    """Run one test's coroutine with a deadline. A failing assertion leaves a draft parked at a gate
    or a held request: its tasks are cancelled and the loop closed without waiting on the default
    executor, so a regression fails here instead of hanging the run."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(asyncio.wait_for(coro, timeout))
    finally:
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.wait(pending, timeout=5))
        loop.close()


async def _draft(cid: str, text: str = "what is running") -> tuple[str, asyncio.Task]:
    turn_id = str(uuid.uuid4())
    task, view = await ask_orchestrator.begin(
        cid, turn_id, text, working_keys=set(), via="call", draft=True
    )
    assert task is not None and view is None
    return turn_id, task


async def _until(pred, timeout: float = 20.0) -> None:
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.01)


def _free(cid: str) -> bool:
    fd = ask_store.try_turn_lock(cid)
    if fd is None:
        return False
    os.close(fd)
    return True


def _shape(records: list[dict]) -> list[tuple]:
    return [(r.get("type"), r.get("status"), r.get("name"), r.get("text")) for r in records]


def _changed(before: dict[str, bytes]) -> list[str]:
    after = _stores()
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def _new(app) -> str:  # noqa: F811
    cid = app.new()
    assert _free(cid)  # the lock file exists before the snapshot: a discard leaves even it alone
    template_secrets._key_for_write()  # and the redaction key, which any model call makes once
    missions.list_missions()  # and the missions DB, which its first reader creates
    return cid


# ---- the ratchet -----------------------------------------------------------------------------


def test_every_tool_is_draft_readable_or_gated():
    names = set(ask_orchestrator.TOOLS)
    assert ask_orchestrator.DRAFT_READ_TOOLS <= names
    assert names - ask_orchestrator.DRAFT_READ_TOOLS == GATED
    for name in ask_orchestrator.DRAFT_READ_TOOLS:
        assert ask_orchestrator.TOOLS[name].kind == "read", name


@pytest.mark.parametrize("name", sorted(ask_orchestrator.DRAFT_READ_TOOLS))
def test_each_draft_readable_tool_leaves_every_store_as_it_was(app, held, found, name):  # noqa: F811
    script = held(call(name, {}), answer("done"), free=1)
    cid = _new(app)
    before = _stores()

    async def go():
        turn_id, task = await _draft(cid)
        await asyncio.wait_for(script.reached.wait(), 20)  # the tool ran; the model is asked again
        assert await ask_orchestrator.discard(cid, turn_id)
        assert task.done()

    run(go())
    assert len(script.bodies) == 1 and script.tool_results(0) == []
    assert _changed(before) == []
    assert _free(cid) and not ask_orchestrator._RUNNING


# ---- discard, at each await ------------------------------------------------------------------


def test_a_discard_while_the_model_thinks_leaves_nothing(app, held):  # noqa: F811
    script = held(answer("all quiet"))
    cid = _new(app)
    before = _stores()

    async def go():
        turn_id, task = await _draft(cid)
        await asyncio.wait_for(script.reached.wait(), 20)
        assert not ask_orchestrator.running(cid)  # a draft is never shown as running
        assert await ask_orchestrator.discard(cid, turn_id)
        assert task.cancelled()
        assert not await ask_orchestrator.discard(cid, turn_id)  # once only

    run(go())
    assert _changed(before) == []
    assert _free(cid) and not ask_orchestrator._RUNNING


def test_a_discard_inside_a_read_tool_leaves_nothing(app, held, monkeypatch):  # noqa: F811
    entered, never = asyncio.Event(), asyncio.Event()
    real = ask_orchestrator.TOOLS["list_missions"]

    async def stuck(turn, args):
        entered.set()
        await never.wait()

    monkeypatch.setitem(
        ask_orchestrator.TOOLS, "list_missions", dataclasses.replace(real, run=stuck)
    )
    script = held(call("list_missions", {}), answer("none"), free=1)
    cid = _new(app)
    before = _stores()

    async def go():
        turn_id, _ = await _draft(cid)
        await asyncio.wait_for(entered.wait(), 5)
        assert await ask_orchestrator.discard(cid, turn_id)

    run(go())
    assert len(script.bodies) == 1
    assert _changed(before) == []
    assert _free(cid) and not ask_orchestrator._RUNNING


def test_a_gated_tool_waits_for_the_commit_and_a_discard_means_it_never_ran(app, held, monkeypatch):  # noqa: F811
    ran: list[str] = []
    for name in GATED:
        real = ask_orchestrator.TOOLS[name]

        async def effect(turn, args, _name=name):
            ran.append(_name)
            return {"ok": True}, {}

        monkeypatch.setitem(ask_orchestrator.TOOLS, name, dataclasses.replace(real, run=effect))
    script = held(calls(("list_missions", {}), ("propose_nudge", {})), answer("sent"), free=1)
    cid = _new(app)
    before = _stores()

    async def go():
        turn_id, task = await _draft(cid)
        await asyncio.sleep(0.2)  # the read tool ran; the proposal sits at the gate
        assert len(script.bodies) == 1 and not script.reached.is_set() and not task.done()
        assert ran == []
        assert await ask_orchestrator.discard(cid, turn_id)

    run(go())
    assert ran == []
    assert _changed(before) == []
    assert _free(cid) and not ask_orchestrator._RUNNING


# ---- commit ----------------------------------------------------------------------------------


def test_a_committed_draft_stores_what_a_normal_turn_would(app, ai, found):  # noqa: F811
    replies = (call("list_missions", {}), answer("Two missions are running."))
    ai(*replies)
    normal = app.new()

    async def plain():
        task, _ = await ask_orchestrator.begin(
            normal, str(uuid.uuid4()), "what is running", working_keys=set(), via="call"
        )
        return await task

    run(plain())
    script = ai(*replies)
    cid = app.new()
    before = ask_store.read(cid)

    async def drafted():
        turn_id, task = await _draft(cid)
        await _until(lambda: len(script.bodies) == 2)  # the model has answered: nothing written
        await asyncio.sleep(0.05)
        assert not task.done()
        assert ask_store.read(cid) == before
        assert await ask_orchestrator.commit(cid, turn_id)
        assert await ask_orchestrator.commit(cid, turn_id)  # a repeat is harmless
        return await task

    view = run(drafted())
    assert view["status"] == "done"
    assert _shape(ask_store.read(cid)) == _shape(ask_store.read(normal))
    assert _free(cid) and not ask_orchestrator._RUNNING


def test_a_gated_tool_runs_only_after_the_commit(app, held, monkeypatch):  # noqa: F811
    ran: list[str] = []
    real = ask_orchestrator.TOOLS["propose_nudge"]

    async def effect(turn, args):
        ran.append("propose_nudge")
        return {"staged": True}, {}

    monkeypatch.setitem(
        ask_orchestrator.TOOLS, "propose_nudge", dataclasses.replace(real, run=effect)
    )
    script = held(call("propose_nudge", {}), answer("Staged."), free=2)
    cid = _new(app)

    async def go():
        turn_id, task = await _draft(cid)
        await asyncio.sleep(0.2)
        assert ran == [] and len(script.bodies) == 1
        assert await ask_orchestrator.commit(cid, turn_id)
        assert ask_orchestrator.running(cid)  # committed: a normal running turn now
        await _until(lambda: ran == ["propose_nudge"])
        return await task

    assert run(go())["status"] == "done"
    records = ask_store.read(cid)
    assert [r["name"] for r in records if r.get("type") == "tool"] == ["propose_nudge"]


def test_progress_is_held_until_the_commit(app, held, found):  # noqa: F811
    script = held(call("find_sessions", {"query": "rebase"}), answer("found it"), free=1)
    cid = _new(app)

    async def go():
        turn_id, task = await _draft(cid)
        q = ask_orchestrator.listen(cid, task)
        await asyncio.wait_for(script.reached.wait(), 20)
        assert q.empty()  # nothing of a draft is shown
        assert await ask_orchestrator.commit(cid, turn_id)
        shown = []
        while not q.empty():
            shown.append(q.get_nowait())
        script.go.set()
        await task
        return shown

    shown = run(go())
    assert any(ev.get("tool") == "find_sessions" for ev in shown)


# ---- what supersedes a draft -----------------------------------------------------------------


def test_a_typed_question_discards_an_open_draft_and_runs(app, held, monkeypatch):  # noqa: F811
    ran: list[str] = []
    real = ask_orchestrator.TOOLS["propose_nudge"]

    async def effect(turn, args):
        ran.append("draft's nudge")
        return {"staged": True}, {}

    monkeypatch.setitem(
        ask_orchestrator.TOOLS, "propose_nudge", dataclasses.replace(real, run=effect)
    )
    script = held(call("propose_nudge", {}), answer("typed answer"), free=2)
    cid = _new(app)

    async def go():
        _, draft = await _draft(cid)
        await asyncio.sleep(0.2)
        task, _ = await ask_orchestrator.begin(
            cid, str(uuid.uuid4()), "typed instead", working_keys=set()
        )
        assert draft.cancelled()
        return await task

    assert run(go())["status"] == "done"
    assert ran == []
    texts = [r.get("text") for r in ask_store.read(cid) if r.get("type") == "user"]
    assert texts == ["typed instead"]
    assert len(script.bodies) == 2


def test_only_a_calls_plain_question_can_be_drafted(app, ai):  # noqa: F811
    ai(answer("x"))
    cid = app.new()

    async def go():
        for kw in ({"via": None}, {"via": "call", "option": 0}, {"via": "call", "answers": "q"}):
            with pytest.raises(ask_orchestrator.TurnError) as e:
                await ask_orchestrator.begin(
                    cid, str(uuid.uuid4()), "x", working_keys=set(), draft=True, **kw
                )
            assert e.value.status == 422

    run(go())
    assert not ask_orchestrator._RUNNING


def test_commit_and_discard_refuse_a_turn_that_is_not_the_open_draft(app, ai):  # noqa: F811
    ai(answer("x"))
    cid = app.new()

    async def go():
        assert not await ask_orchestrator.commit(cid, "nope")
        assert not await ask_orchestrator.discard(cid, "nope")
        task, _ = await ask_orchestrator.begin(
            cid, str(uuid.uuid4()), "x", working_keys=set(), via="call"
        )
        turn_id = ask_orchestrator._RUNNING[cid].turn_id
        assert not await ask_orchestrator.discard(cid, turn_id)  # a normal turn is never dropped
        await task

    run(go())
