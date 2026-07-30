"""The Pulse chat that can act (#726 Phase 4).

The property worth guarding here is that a chat instruction is NOT a privileged write path. It
would be easy to let the chat act directly — the operator asked for it — but that would mean
two routes to a PTY with two sets of guards, and the newer one would be the weaker. So these
tests assert the chat goes through the same closed verb set, the same id validation, the same
tier gating and the same ledger as a scheduled pass.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import httpx
import pytest

from agent_sessions import (
    aitasks,
    orchestrator,
    orchestrator_chat,
    prefs,
    pulse,
    review,
)
from agent_sessions import orchestrator_ledger as ledger

BASE = "https://ai.test/v1"
SECRET = "sk-chat-test"  # noqa: S105 — test fixture value
UID = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
KEY = f"claude:{UID}"


@dataclass
class FakeSession:
    engine: str
    uuid: str
    cwd: str
    last_mtime: float
    first_user_message: str = "first"
    archived: bool = False

    @property
    def short_uuid(self) -> str:
        return self.uuid[:8]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_PREFS", str(tmp_path / "prefs.json"))
    monkeypatch.setenv("AGENT_SESSIONS_ORCHESTRATOR_LEDGER", str(tmp_path / "led.jsonl"))
    monkeypatch.setattr(review, "_TRANSPORT", None)
    aitasks.reset()
    prefs.set_ai_review({"enabled": True, "base_url": BASE, "api_key": SECRET, "model": "m"})
    yield
    aitasks.reset()


def _setup(monkeypatch, sessions):
    monkeypatch.setattr(pulse.engines, "scan_all", lambda: sessions)
    monkeypatch.setattr(pulse.metadata, "load", lambda *a, **k: {})
    monkeypatch.setattr(pulse.metadata, "load_aliases", lambda *a, **k: {})
    monkeypatch.setattr(pulse.projects, "load", lambda *a, **k: {})
    monkeypatch.setattr(orchestrator.metadata, "load", lambda *a, **k: {})
    monkeypatch.setattr(orchestrator.metadata, "load_aliases", lambda *a, **k: {})
    monkeypatch.setattr(orchestrator.scrollback, "live_tail_text", lambda *a, **k: "x")


def _scripted(replies: list[dict]):
    """A transport that answers successive calls with successive payloads — the chat makes a
    routing call and then (for instruct) a second one."""
    seq = list(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        payload = seq.pop(0) if seq else {}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    return httpx.MockTransport(handler)


def test_an_instruction_becomes_a_proposal_not_a_write(monkeypatch):
    """The core property: the chat produces a ledger PROPOSAL through the normal verb path.
    It never reaches a PTY on its own."""
    _setup(monkeypatch, [FakeSession("claude", UID, "/a", time.time())])
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _scripted(
            [
                {"intent": "instruct"},
                {
                    "answer": "Nudged it.",
                    "actions": [
                        {
                            "session_id": KEY,
                            "verb": "continue",
                            "confidence": 0.9,
                            "rationale": "asked to keep going",
                        }
                    ],
                },
            ]
        ),
    )
    r = asyncio.run(orchestrator_chat.ask("tell the claude session to keep going"))
    assert r["intent"] == "instruct"
    assert len(r["actions"]) == 1
    act = r["actions"][0]
    # SUGGEST is the default tier, so even an explicit instruction still queues for a tap.
    assert act["state"] == "proposed"
    assert act["source"] == "chat"
    assert "precondition" in act  # bound like any other deliverable proposal
    assert ledger.get(act["id"])["state"] == "proposed"


def test_the_chat_cannot_name_a_session_it_was_not_shown(monkeypatch):
    """Same anti-hallucination rule as a scheduled pass — the chat is not exempt."""
    _setup(monkeypatch, [FakeSession("claude", UID, "/a", time.time())])
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _scripted(
            [
                {"intent": "instruct"},
                {
                    "answer": "ok",
                    "actions": [
                        {
                            "session_id": "claude:99999999-9999-4999-8999-999999999999",
                            "verb": "continue",
                            "confidence": 1.0,
                        },
                        {"session_id": "rm -rf /", "verb": "continue", "confidence": 1.0},
                    ],
                },
            ]
        ),
    )
    assert asyncio.run(orchestrator_chat.ask("nudge everything"))["actions"] == []


def test_the_chat_cannot_use_a_verb_outside_the_closed_set(monkeypatch):
    _setup(monkeypatch, [FakeSession("claude", UID, "/a", time.time())])
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _scripted(
            [
                {"intent": "instruct"},
                {"actions": [{"session_id": KEY, "verb": "run_shell", "confidence": 1.0}]},
            ]
        ),
    )
    assert asyncio.run(orchestrator_chat.ask("run ls"))["actions"] == []


def test_an_excluded_session_is_not_reachable_from_the_chat(monkeypatch):
    """Withdrawing agency must hold against a direct instruction, not just the scheduler."""
    _setup(monkeypatch, [FakeSession("claude", UID, "/a", time.time())])
    monkeypatch.setattr(orchestrator, "eligible_cards", lambda **k: ([], {"excluded": 1}))
    monkeypatch.setattr(review, "_TRANSPORT", _scripted([{"intent": "instruct"}]))
    r = asyncio.run(orchestrator_chat.ask("nudge it"))
    assert r["actions"] == []
    assert "excluded" in r["answer"]


def test_history_is_read_from_the_ledger_not_re_inferred(monkeypatch):
    """'Why did you do X' must be answered from the RECORD. A model reconstructing its own past
    reasoning is writing fiction, and this is the question where the operator can least afford
    that — they are auditing an autonomous system."""
    ledger.append(
        {
            "id": "a1",
            "state": "delivered",
            "verb": "continue",
            "session_id": KEY,
            "rationale": "it had stopped mid-task",
        }
    )
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps({"intent": "history"})}}]}
        )

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))
    r = asyncio.run(orchestrator_chat.ask("why did you nudge that one?"))
    assert r["intent"] == "history"
    assert r["actions"][0]["rationale"] == "it had stopped mid-task"
    # Exactly ONE model call — the routing one. The answer itself is not generated.
    assert len(calls) == 1


def test_retrieval_is_delegated_to_the_existing_ask(monkeypatch):
    seen: list = []

    async def fake_ask(query, history=None, **kw):
        seen.append(query)
        return {"answer": "that one", "matches": [], "stage": "catalog", "configured": True}

    monkeypatch.setattr(orchestrator_chat.pulse_chat, "ask", fake_ask)
    monkeypatch.setattr(review, "_TRANSPORT", _scripted([{"intent": "find"}]))
    r = asyncio.run(orchestrator_chat.ask("which session was the websocket bug?"))
    assert r["intent"] == "find" and r["answer"] == "that one"
    assert seen == ["which session was the websocket bug?"]


def test_an_unclassifiable_message_falls_back_to_describing(monkeypatch):
    """Unsure between find and instruct → find. Describing is safe; acting is not."""
    monkeypatch.setattr(review, "_TRANSPORT", _scripted([{"intent": "nonsense"}]))
    called: list = []

    async def fake_ask(query, history=None, **kw):
        called.append(1)
        return {"answer": "", "matches": [], "stage": "empty", "configured": True}

    monkeypatch.setattr(orchestrator_chat.pulse_chat, "ask", fake_ask)
    assert asyncio.run(orchestrator_chat.ask("???"))["intent"] == "find"
    assert called == [1]


def test_a_failed_routing_call_falls_back_to_describing(monkeypatch):
    """An endpoint hiccup must not silently promote a message to an instruction."""

    def boom(request):
        return httpx.Response(500)

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(boom))

    async def fake_ask(query, history=None, **kw):
        return {"answer": "", "matches": [], "stage": "empty", "configured": True}

    monkeypatch.setattr(orchestrator_chat.pulse_chat, "ask", fake_ask)
    assert asyncio.run(orchestrator_chat.ask("do something"))["intent"] == "find"


# --- #731 review: policy must be re-read ACROSS the model call --------------------------


def _instruct_then(monkeypatch, on_second):
    """Scripted transport whose SECOND reply (the instruct call) is produced only after
    ``on_second`` runs — i.e. policy changes while the call is in flight, which is exactly the
    window a real endpoint round-trip opens."""
    seq = [{"intent": "instruct"}]

    def handler(request: httpx.Request) -> httpx.Response:
        if seq:
            payload = seq.pop(0)
        else:
            on_second()
            payload = {
                "answer": "On it.",
                "actions": [
                    {
                        "session_id": KEY,
                        "verb": "continue",
                        "confidence": 0.99,
                        "rationale": "asked to keep going",
                    }
                ],
            }
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(payload)}}]}
        )

    monkeypatch.setattr(review, "_TRANSPORT", httpx.MockTransport(handler))


def test_disabling_orchestration_mid_call_cannot_leave_an_approved_action(monkeypatch):
    """An operator who withdraws agency while the model is thinking must not come back to an
    `approved` action. The config read before the call is stale by the time it returns."""
    _setup(monkeypatch, [FakeSession("claude", UID, "/a", time.time())])
    # YOLO + continue is the one combination that auto-approves, so this is the case where a
    # stale read has real consequences: it would deliver without a tap.
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})
    _instruct_then(monkeypatch, lambda: prefs.set_orchestrator({"enabled": False}))

    r = asyncio.run(orchestrator_chat.ask("keep it going"))

    states = [a["state"] for a in r["actions"]]
    assert (
        "approved" not in states
    ), "orchestration was disabled during the call; the stale config still auto-approved"
    for a in r["actions"]:
        assert ledger.get(a["id"])["state"] != "approved"


def test_excluding_a_session_mid_call_drops_it_before_anything_is_recorded(monkeypatch):
    """Eligibility is derived before the call and can be wrong after it. A session excluded
    (or made non-actuable) in that window must be dropped, not recorded against."""
    _setup(monkeypatch, [FakeSession("claude", UID, "/a", time.time())])
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})

    def exclude_it():
        # Re-derivation goes through _eligible_ids; make it report nothing eligible.
        monkeypatch.setattr(orchestrator, "_eligible_ids", lambda wk: [])

    _instruct_then(monkeypatch, exclude_it)

    r = asyncio.run(orchestrator_chat.ask("keep it going"))

    assert r["actions"] == [], "an action was recorded against a session excluded mid-call"
    assert (
        "no longer mine to act on" in r["answer"]
    ), "the answer still claimed success over an empty action list"


def test_a_concurrent_pass_cannot_double_queue_the_same_session(monkeypatch):
    """The TOCTOU the post-model recheck alone does not close. The chat and a scheduled pass
    run under DIFFERENT single-flights, so both can see a session as free, both mint an action,
    and both append. Two live actions for one session can both reach the actuator — and if the
    first write has not yet changed the screen, the second precondition passes too, putting
    duplicate input into a real session.

    Reproduced Hermes's way: inject the rival action AFTER eligibility has been re-derived but
    BEFORE the chat persists."""
    _setup(monkeypatch, [FakeSession("claude", UID, "/a", time.time())])
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})

    real_eligible_ids = orchestrator._eligible_ids

    def eligible_then_rival(working_keys):
        out = real_eligible_ids(working_keys)
        # A scheduled pass lands here, between our check and our write.
        ledger.append(
            {
                "id": "rival",
                "state": "approved",
                "verb": "continue",
                "session_id": KEY,
                "engine": "claude",
                "ts": time.time(),
            }
        )
        return out

    monkeypatch.setattr(orchestrator, "_eligible_ids", eligible_then_rival)
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _scripted(
            [
                {"intent": "instruct"},
                {
                    "answer": "On it.",
                    "actions": [
                        {
                            "session_id": KEY,
                            "verb": "continue",
                            "confidence": 0.99,
                            "rationale": "keep going",
                        }
                    ],
                },
            ]
        ),
    )

    r = asyncio.run(orchestrator_chat.ask("keep it going"))

    live = [a for a in ledger.live_actions() if a.get("session_id") == KEY]
    assert len(live) == 1, f"{len(live)} live actions queued for one session"
    assert live[0]["id"] == "rival", "the chat overwrote a pass's action instead of yielding"
    # And the reply must not claim to have queued something the ledger refused — neither in
    # the action list NOR in the prose. The model said "On it."; the ledger said no.
    assert r["actions"] == [], "the response reported an action that was never written"
    assert (
        "On it." not in r["answer"]
    ), "the answer still sounded like acceptance over an empty action list"
    assert "Nothing was queued" in r["answer"]


def test_a_yolo_chat_instruction_reaches_the_actuator(monkeypatch):
    """The bug the #729 merge created, which neither PR had alone.

    Phase 2 added `deliver_pass_actions` and wired it into the scheduled and manual passes.
    The chat route predates that, so a `yolo` instruction produced an `approved` record that
    nothing ever delivered: those sweeps only deliver what their own `run_pass()` produced, and
    the chat's live action makes that session ineligible for them. The action sat until a
    manual tap or expiry — the exact inert-`yolo` condition Phase 2 removed elsewhere.
    """
    from agent_sessions import actuator

    _setup(monkeypatch, [FakeSession("claude", UID, "/a", time.time())])
    prefs.set_orchestrator({"enabled": True, "autonomy": "yolo"})

    delivered: list = []

    async def fake_deliver_pass_actions(records, *, registry=None):
        delivered.extend(records)
        return [{**r, "state": "delivered"} for r in records]

    monkeypatch.setattr(actuator, "deliver_pass_actions", fake_deliver_pass_actions)
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _scripted(
            [
                {"intent": "instruct"},
                {
                    "answer": "On it.",
                    "actions": [
                        {
                            "session_id": KEY,
                            "verb": "continue",
                            "confidence": 0.99,
                            "rationale": "keep going",
                        }
                    ],
                },
            ]
        ),
    )

    r = asyncio.run(orchestrator_chat.ask("keep it going"))
    # The chat itself still only proposes/approves — delivery is the ROUTE's job, so this
    # asserts the record it hands back is deliverable rather than already sent.
    assert len(r["actions"]) == 1
    assert (
        r["actions"][0]["state"] == "approved"
    ), "yolo did not auto-approve, so the route would have nothing to deliver"


def test_suggest_leaves_a_chat_instruction_as_a_proposal(monkeypatch):
    """The other half: outside yolo the operator's tap is still required, so the route must not
    deliver. A chat that acts unprompted in `suggest` would be worse than one that never acts."""
    _setup(monkeypatch, [FakeSession("claude", UID, "/a", time.time())])
    prefs.set_orchestrator({"enabled": True, "autonomy": "suggest"})
    monkeypatch.setattr(
        review,
        "_TRANSPORT",
        _scripted(
            [
                {"intent": "instruct"},
                {
                    "answer": "Queued.",
                    "actions": [
                        {
                            "session_id": KEY,
                            "verb": "continue",
                            "confidence": 0.99,
                            "rationale": "keep going",
                        }
                    ],
                },
            ]
        ),
    )
    r = asyncio.run(orchestrator_chat.ask("keep it going"))
    assert (
        r["actions"][0]["state"] == "proposed"
    ), "suggest must leave the action awaiting a tap, not deliver it"
