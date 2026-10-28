"""#1397 P2 — HQ acts by voice: one card, one spoken confirmation, through the card's own route.

The call is driven directly with the worker's events (the same JSON the Pipecat worker sends), a
scripted HQ turn and a recorded in-process route. Pinned:

* a challenge ARMS only when its read-back has played (`said` for that say id);
* a confirmation must have STARTED after arming — HQ's read-back echoing into the mic, or speech
  begun while HQ was still reading, never confirms; talking over the read-back voids it;
* one use, a 20 s window, voided by a new question or the call ending;
* strict phrases: a confirm verb, no negation, an ordinal when several cards are pending;
  "everything" is refused; "cancel" leaves the card untouched;
* settlement is the card's existing route as the operator; what is said comes from its status;
* an HQ operation that needs on-screen input is never offered (what the Needs-you read-out says
  about each item: `test_voice_needs_you.py`; deciding its permissions and menus by voice:
  `test_voice_needs_you_decide.py`).
"""

from __future__ import annotations

import asyncio
import types

import pytest

from agent_sessions import asgi_call, ask_orchestrator, voice_calls, voice_text


class Rig:
    def __init__(self, monkeypatch, view: dict | None = None, route_status: int = 200):
        self.to_client: list = []
        self.to_worker: list = []
        self.routes: list = []
        self.asked: list[str] = []
        self.route_status = route_status
        self.needs_rows: list[dict] = []
        self.needs_body: dict | None = None  # the whole Needs-you answer, when a test sets one
        self.view = view or {}
        self.turn = 0  # the worker's user-turn counter

        async def fake_begin(cid, turn_id, text, **kw):
            self.asked.append(text)
            return None, {"turn_id": turn_id, "status": "done", "answer": "Done.", **self.view}

        async def fake_call(app, operator, method, path, body, **kw):
            self.routes.append((method, path, body))
            if method == "GET" and path == "/api/pulse/needs-you":
                return asgi_call.Response(200, self.needs_body or {"rows": self.needs_rows})
            # What the real routes answer on success: a delivery record, or the card's state.
            if path.startswith("/api/pulse/actions/"):
                return asgi_call.Response(self.route_status, {"state": "delivered"})
            # The decide route answers `{"proposal": view}` (routes/ask.py).
            return asgi_call.Response(self.route_status, {"proposal": {"state": "done"}})

        monkeypatch.setattr(ask_orchestrator, "begin", fake_begin)
        monkeypatch.setattr(asgi_call, "call", fake_call)
        # The confirmation judge (#1715): by default it says `other` — not an answer — so a reply
        # the English phrases don't cover is a new question, as it always was. A test sets
        # `self.verdict` (or a callable) to judge otherwise; `self.judged` records what it saw.
        self.verdict: object = ("other", None)
        self.judged: list[tuple[str, int, str]] = []

        async def fake_judge(readback, cards, reply):
            self.judged.append((readback, cards, reply))
            v = self.verdict
            return await v() if callable(v) else v

        from agent_sessions import voice_judge

        monkeypatch.setattr(voice_judge, "judge_cards", fake_judge)
        self.call = voice_calls.Call(
            id="c", cid="conv", contact="asia-woman", voice="aria", operator=object(),
            app=object(), registry=None, state="listening",
        )  # fmt: skip

        async def to_client(m):
            self.to_client.append(m)

        async def to_worker(m):
            self.to_worker.append(m)

        self.call._to_client = to_client
        self.call._to_worker = to_worker
        # The Rig's STT was TESTED and reports no language: English is assumed (a None — never
        # tested — fails spoken confirmation closed; see the language-gate tests).
        self.call.detects_language = False

    def says(self) -> list[dict]:
        return [m for m in self.to_worker if isinstance(m, dict) and m.get("t") == "say"]

    def said_text(self) -> list[str]:
        return [m["text"] for m in self.says()]

    async def ev(self, **e):
        # The worker numbers each user turn: a `user_started` opens the next one and that turn's
        # utterance carries its number. Unless a test says otherwise, that is what it sends.
        if e.get("t") == "user_started" and "turn" not in e:
            self.turn += 1
            e["turn"] = self.turn
        elif e.get("t") == "utterance" and "turn" not in e and not e.get("late"):
            e["turn"] = self.turn
        await self.call.on_worker_event(e)

    async def ask(self, text: str):
        await self.ev(t="user_started")
        await self.ev(t="utterance", text=text)
        if self.call.turn_task is not None:
            await self.call.turn_task
        await self.side()

    async def side(self):
        """Settlements, Needs-you read-outs and judgements run beside the relay loop: wait for
        them — and for a question a judgement handed back (`other`)."""
        while True:
            # Yield first: a finished task leaves `side_tasks` only through its done callback,
            # which runs on the NEXT loop pass — awaiting already-done tasks never yields, so
            # without this the loop spins forever (it hung pr-validate once).
            await asyncio.sleep(0)
            pending = [t for t in self.call.side_tasks if not t.done()]
            turn = self.call.turn_task
            if not pending and (turn is None or turn.done()):
                return
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if turn is not None and not turn.done():
                await turn

    async def readback_played(self):
        """The worker sent the read-back's audio (`said`), then the browser played it out."""
        ch = self.call.challenge
        assert ch is not None, "no challenge pending"
        await self.ev(t="said", id=ch["say_id"])
        await self.call.on_played(ch["say_id"])


NUDGE = {"proposals": [{"kind": "nudge", "action_id": "act-1", "title": "api-rebase",
                        "text": "Rerun the auth tests."}]}  # fmt: skip
TWO = {"proposals": [
    {"kind": "nudge", "action_id": "act-1", "title": "api-rebase", "text": "Rerun tests."},
    {"kind": "session", "action_id": "prop-2", "title": "docs refresh",
     "text": "Refresh the install docs."},
]}  # fmt: skip


def run(coro):
    return asyncio.run(coro)


def test_a_card_is_read_back_and_confirmed_through_its_own_route(monkeypatch):
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests on api-rebase")
        assert r.said_text()[-1] == (
            "A message to api-rebase: Rerun the auth tests. Say confirm to send it, or cancel."
        )
        await r.readback_played()
        await r.ask("confirm")

    run(go())
    assert ("POST", "/api/pulse/actions/act-1/approve", None) in r.routes
    assert r.said_text()[-1] == "Sent."
    assert r.asked == ["Rerun the tests on api-rebase"]  # "confirm" was not a new question
    assert r.call.challenge is None  # single use


def test_speech_that_began_during_the_read_back_never_confirms(monkeypatch):
    """The echo guard: an utterance that STARTED before the challenge armed is a new question."""
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests")
        say_id = r.call.challenge["say_id"]
        await r.ev(t="user_started")  # began while HQ was still reading back (or its echo)…
        assert r.call.challenge is None  # …voids the challenge outright
        await r.ev(t="said", id=say_id)  # the read-back finishing later arms nothing
        await r.call.on_played(say_id)
        assert r.call.challenge is None
        await r.ev(t="utterance", text="confirm")  # so that speech is just a new question
        if r.call.turn_task is not None:
            await r.call.turn_task

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)
    assert r.asked == ["Rerun the tests", "confirm"]


def test_talking_over_the_read_back_voids_it(monkeypatch):
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests")
        ch = r.call.challenge
        await r.ev(t="user_started")  # barge-in before the read-back played out
        await r.ev(t="said", id=ch["say_id"])  # (an interruption drops it; even if it came)
        await r.call.on_played(ch["say_id"])
        await r.ask("confirm")

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


def test_the_challenge_expires(monkeypatch):
    r = Rig(monkeypatch, NUDGE)
    monkeypatch.setattr(voice_calls, "CHALLENGE_S", 0.01)

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await asyncio.sleep(0.05)
        await r.ask("confirm")

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


def test_a_new_question_voids_the_challenge(monkeypatch):
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ask("what about the docs mission")
        await r.ask("confirm")

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


def test_everything_is_refused_and_ordinals_pick_one(monkeypatch):
    r = Rig(monkeypatch, TWO)

    async def go():
        await r.ask("do both")
        await r.readback_played()
        await r.ask("approve everything")
        assert r.said_text()[-1] == "I can only confirm one at a time, Commander."
        await r.readback_played()  # the clarification must PLAY before the challenge re-arms
        await r.ask("confirm")
        assert r.said_text()[-1] == "Which one, Commander? Say confirm and the number."
        await r.readback_played()
        await r.ask("confirm the second")

    run(go())
    assert r.routes == [
        ("POST", "/api/ask/proposals/prop-2/decide", {"decision": "start", "bypass": False})
    ]
    assert r.said_text()[-1] in ("Done.", "Accepted. It's starting now.")


def test_cancel_leaves_the_card(monkeypatch):
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ask("no")

    run(go())
    assert r.routes == [] and r.said_text()[-1] == "Okay. It stays on your screen."


def test_a_changed_card_is_reported_from_the_route(monkeypatch):
    r = Rig(monkeypatch, NUDGE, route_status=409)

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ask("send it")

    run(go())
    assert r.said_text()[-1] == "That card changed, so I didn't send it. It's on your screen."


def test_an_ended_call_settles_nothing(monkeypatch):
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.call.end("hung_up")
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="confirm")

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


def test_only_the_browser_playing_the_read_back_arms_it(monkeypatch):
    """`said` (the worker SENT the audio) is not enough; nor is a `played` ack for another say,
    or one that arrives before `said`."""
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests")
        ch = r.call.challenge
        await r.call.on_played(ch["say_id"])  # before the worker sent it: ignored
        assert ch["armed_at"] is None
        await r.ev(t="said", id=ch["say_id"])
        assert ch["armed_at"] is None  # sent, not yet played
        await r.call.on_played("another-say")
        assert ch["armed_at"] is None
        await r.call.on_played(ch["say_id"])
        assert ch["armed_at"] is not None

    run(go())


def test_a_tap_or_mute_makes_the_challenge_inert(monkeypatch):
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.call.on_card_decided("act-1")  # the operator tapped Send
        assert r.call.challenge is None
        await r.ask("confirm")
        assert not any(p.endswith("/approve") for _, p, _ in r.routes)

    run(go())


def test_an_accepted_start_is_followed_to_its_end(monkeypatch):
    from agent_sessions import ask_proposals

    r = Rig(monkeypatch, TWO, route_status=202)
    monkeypatch.setattr(voice_calls, "OUTCOME_EVERY_S", 0.01)
    states = iter(["deciding", "started"])

    def views(cid):
        return "ok", {"prop-2": {"state": next(states, "started")}}

    monkeypatch.setattr(ask_proposals, "views", views)

    async def go():
        await r.ask("start the docs session")
        await r.readback_played()
        await r.ask("start the second")

    run(go())
    assert r.said_text()[-2:] == ["Accepted. It's starting now.", "The session is up."]


def test_operations_that_need_input_are_never_offered():
    view = {"proposals": [
        {"kind": "operation", "action_id": "op-1", "title": "web", "label": "Move project",
         "needs": ["folder"]},
        {"kind": "operation", "action_id": "op-2", "title": "api", "label": "Archive session",
         "op": "session.archive", "needs": [],
         "text": "Archives the session api: its agent stops."},
    ]}  # fmt: skip
    cards = voice_calls.confirmable_cards(view)
    assert [c["id"] for c in cards] == ["op-2"]


def _library_view(op_id: str) -> dict:
    card = {"kind": "operation", "action_id": "op-1", "title": "nightly", "label": "Enable",
            "verb": "Enable", "op": op_id, "needs": [],
            "text": "Turns it on. From then it starts agents by itself."}  # fmt: skip
    return {"proposals": [card]}


@pytest.mark.parametrize("op_id", [o.id for o in __import__("agent_sessions.ask_library",
                                   fromlist=["library_ops"]).library_ops()])  # fmt: skip
def test_a_library_operation_is_never_confirmed_by_voice(op_id):
    """A Library card offers Confirm only once its content is on screen (#1611), and enabling or
    widening an automation consents to the scope the card shows: none of it is in the one
    sentence a call would read, so every Library operation is a screen decision."""
    assert voice_calls.confirmable_cards(_library_view(op_id)) == []


@pytest.mark.parametrize("op_id", [None, "", "session.nope", 7])
def test_an_operation_the_catalog_does_not_know_is_never_confirmed_by_voice(op_id):
    assert voice_calls.confirmable_cards(_library_view(op_id)) == []  # type: ignore[arg-type]


def test_session_and_project_operations_stay_confirmable_by_voice():
    view = _library_view("session.archive")
    view["proposals"][0]["text"] = "Archives the session api: its agent stops."
    assert [c["id"] for c in voice_calls.confirmable_cards(view)] == ["op-1"]


def test_needs_you_offers_no_row_it_cannot_read_in_full(monkeypatch):
    r = Rig(monkeypatch)
    r.needs_rows = [
        {"title": "api-rebase", "kind": "question",
         "action": {"id": "act-q", "verb": "answer", "can_approve": True, "answer": "Use main."}},
        {"title": "deploy", "kind": "approval",
         "action": {"id": "act-p", "verb": "answer", "can_approve": True, "answer": "yes"}},
        {"title": "docs", "kind": "choice",
         "action": {"id": "act-c", "verb": "choose", "can_approve": True}},
    ]  # fmt: skip

    async def go():
        await r.ask("What needs me right now?")
        assert r.said_text()[-2] == (
            "3 things need you. api-rebase asks a question. The suggested answer is: Use main."
            " deploy wants you to confirm something I can't read out — it's on your screen."
            " docs is showing a menu I can't read out — it's on your screen."
            " Let's decide that one."
        )
        assert "api-rebase asks a question" in r.said_text()[-1]
        await r.readback_played()
        await r.ask("confirm")

    run(go())
    assert ("POST", "/api/pulse/actions/act-q/approve", None) in r.routes
    assert not any("act-p" in p or "act-c" in p for _, p, _ in r.routes)
    assert r.asked == []  # answered from the server's list, never a model turn


@pytest.mark.parametrize(
    "said,n,want",
    [
        ("confirm", 1, ("confirm", 0)),
        ("send it", 1, ("confirm", 0)),
        ("don't send it", 1, ("other", None)),
        ("confirm the second", 1, ("ambiguous", None)),
        ("confirm the second", 2, ("confirm", 1)),
        ("confirm all of them", 2, ("all", None)),
        ("no", 1, ("cancel", None)),
        ("yes", 1, ("other", None)),  # a bare yes is not a confirmation
        ("please confirm the first one for me now", 2, ("other", None)),  # too long
    ],
)
def test_classifier(said, n, want):
    assert voice_text.classify_confirmation(said, n) == want


def test_the_needs_you_chip_reads_the_same_list(monkeypatch):
    """The call panel's chip ({"t": "needs_you"}) runs the spoken "what needs me?" path."""
    r = Rig(monkeypatch)
    r.needs_rows = [
        {"title": "api-rebase", "kind": "question",
         "action": {"id": "act-q", "verb": "answer", "can_approve": True, "answer": "Use main."}},
    ]  # fmt: skip

    async def go():
        await r.call.on_needs_you_request()
        await r.side()
        assert "api-rebase asks a question" in r.said_text()[-1]
        await r.readback_played()
        await r.ask("confirm")

    run(go())
    assert ("POST", "/api/pulse/actions/act-q/approve", None) in r.routes
    assert r.asked == []


# --- the #1631 reviews: what may settle a card by voice -----------------------------------------


REFUSALS = [
    "I can't confirm that", "I won't confirm", "I cannot confirm", "confirm it shouldn't",
    "don’t send it", "should I send it", "did you send it?", "how do I do it",
    "can you confirm?", "what does confirm do", "why would I start it", "send it later",
    "approve nothing", "confirmed? really?", "confirm nothing", "I can only confirm one",
]  # fmt: skip


@pytest.mark.parametrize("said", REFUSALS)
def test_a_refusal_or_a_question_never_writes(monkeypatch, said):
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ask(said)

    run(go())
    assert not any(m == "POST" for m, _, _ in r.routes)


@pytest.mark.parametrize("said", ["confirm, but first", "confirm the first and two",
                                  "confirm one at a time"])  # fmt: skip
def test_contradictory_picks_never_write(monkeypatch, said):
    r = Rig(monkeypatch, TWO)

    async def go():
        await r.ask("both")
        await r.readback_played()
        await r.ask(said)

    run(go())
    assert not any(m == "POST" for m, _, _ in r.routes)


@pytest.mark.parametrize(
    "text",
    [
        "Rerun the tests. " * 20,  # longer than can be read back in full
        "Run `git push --force origin main` now.",  # code: not read out
        "See https://x.y/z and fix it.",  # a link: not read out
    ],
)
def test_what_cannot_be_heard_in_full_is_never_offered_by_voice(monkeypatch, text):
    view = {"proposals": [{"kind": "nudge", "action_id": "act-1", "title": "api-rebase",
                           "text": text}]}  # fmt: skip
    r = Rig(monkeypatch, view)

    async def go():
        await r.ask("Rerun the tests")

    run(go())
    assert r.call.challenge is None
    assert "I've drafted a message to api-rebase. It's on your screen to send." in r.said_text()


def test_the_spoken_read_back_is_exactly_what_is_sent(monkeypatch):
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests")

    run(go())
    assert NUDGE["proposals"][0]["text"] in r.said_text()[-1]


def test_a_late_transcript_never_confirms(monkeypatch):
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ev(t="user_started")  # any new sound after arming …
        await r.ev(t="utterance", text="confirm", late=True)  # … but this began before it
        if r.call.turn_task is not None:
            await r.call.turn_task
        await r.side()

    run(go())
    assert not any(m == "POST" for m, _, _ in r.routes)


def test_hqs_own_clarification_cannot_confirm_before_it_has_played(monkeypatch):
    r = Rig(monkeypatch, TWO)

    async def go():
        await r.ask("both")
        await r.readback_played()
        await r.ask("confirm")  # ambiguous: HQ asks "Which one?"
        await r.ask("confirm the second")  # its echo, before that question was played

    run(go())
    assert not any(m == "POST" for m, _, _ in r.routes)


def test_a_delivery_that_failed_is_not_reported_as_sent(monkeypatch):
    r = Rig(monkeypatch, NUDGE)

    async def failed(app, operator, method, path, body, **kw):
        r.routes.append((method, path, body))
        return asgi_call.Response(200, {"state": "failed"})

    monkeypatch.setattr(asgi_call, "call", failed)

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ask("confirm")

    run(go())
    assert r.said_text()[-1] == "It wasn't sent, Commander. The card has the reason."
    settled = [m for m in r.to_client if isinstance(m, dict) and m.get("t") == "settled"]
    assert settled and settled[-1]["ok"] is False


def test_settling_never_holds_up_the_call(monkeypatch):
    r = Rig(monkeypatch, NUDGE)
    gate = asyncio.Event()

    async def slow(app, operator, method, path, body, **kw):
        r.routes.append((method, path, body))
        await gate.wait()
        return asgi_call.Response(200, {"state": "delivered"})

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        monkeypatch.setattr(asgi_call, "call", slow)
        await r.ev(t="user_started")
        await asyncio.wait_for(r.ev(t="utterance", text="confirm"), 1)  # returns at once
        await asyncio.wait_for(r.ev(t="user_stopped"), 1)  # the relay keeps turning
        gate.set()
        await r.side()

    run(go())
    assert r.said_text()[-1] == "Sent."


def test_a_hang_up_right_after_confirm_settles_nothing(monkeypatch):
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="confirm")  # the settlement is scheduled, not run …
        await r.call.end("hung_up")  # … and the call ends first
        await r.side()

    run(go())
    assert not any(m == "POST" for m, _, _ in r.routes)


def test_a_fifth_card_is_still_mentioned(monkeypatch):
    view = {"proposals": [{"kind": "nudge", "action_id": f"act-{i}", "title": f"s{i}",
                           "text": "Rerun the tests."} for i in range(5)]}  # fmt: skip
    r = Rig(monkeypatch, view)

    async def go():
        await r.ask("all of them")

    run(go())
    assert len(r.call.challenge["cards"]) == 4
    assert "I've drafted a message to s4. It's on your screen to send." in r.said_text()


def test_a_played_ack_racing_the_said_marker_still_arms(monkeypatch):
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests")
        sid = r.call.challenge["say_id"]

        async def reentrant(m):  # the browser acks the instant the marker reaches it
            r.to_client.append(m)
            if isinstance(m, dict) and m.get("t") == "said":
                await r.call.on_played(m["id"])

        r.call._to_client = reentrant
        await r.ev(t="said", id=sid)
        assert r.call.challenge["armed_at"] is not None

    run(go())


def test_an_interruption_during_the_read_back_voids_the_challenge(monkeypatch):
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests")
        say_id = r.call.challenge["say_id"]
        await r.ev(t="said", id=say_id)
        await r.ev(t="interrupt")  # barge-in: the read-back was cut before it played out
        assert r.call.challenge is None
        await r.call.on_played(say_id)  # a stale ack arms nothing
        assert r.call.challenge is None
        await r.ask("confirm")

    run(go())
    assert not any(m == "POST" for m, _, _ in r.routes)


# --- review 6863 ----------------------------------------------------------------------------


DELETE = {"proposals": [{"kind": "operation", "action_id": "op-1", "title": "Data",
                         "label": "Delete project", "verb": "delete", "needs": [],
                         "op": "project.delete",
                         "text": "Deletes the project Data: its shared memory and launch "
                                 "defaults are lost."}]}  # fmt: skip


def test_a_destructive_operation_reads_its_whole_effect_back(monkeypatch):
    r = Rig(monkeypatch, DELETE)

    async def go():
        await r.ask("delete the data project")
        assert r.said_text()[-1] == (
            "Delete project, for Data: Deletes the project Data: its shared memory and launch "
            "defaults are lost. Say confirm to do it, or cancel."
        )
        await r.readback_played()
        await r.ask("confirm")

    run(go())
    assert ("POST", "/api/ask/proposals/op-1/decide", {"decision": "confirm"}) in r.routes
    assert r.said_text()[-1] == "Done."


def test_an_operation_whose_effect_cannot_be_read_stays_on_screen(monkeypatch):
    view = {"proposals": [{**DELETE["proposals"][0], "text": "Deletes `data/` — see https://x"}]}
    r = Rig(monkeypatch, view)

    async def go():
        await r.ask("delete the data project")

    run(go())
    assert r.call.challenge is None


@pytest.mark.parametrize(
    "state,said,ok",
    [("done", "Done.", True), ("stale", "That card changed, so nothing ran.", False),
     ("dismissed", "It was dismissed, so nothing ran.", False)],
)  # fmt: skip
def test_the_decide_routes_real_answer_shape_is_read(monkeypatch, state, said, ok):
    r = Rig(monkeypatch, DELETE)

    async def shaped(app, operator, method, path, body, **kw):
        r.routes.append((method, path, body))
        return asgi_call.Response(200, {"proposal": {"state": state}})

    async def go():
        await r.ask("delete the data project")
        monkeypatch.setattr(asgi_call, "call", shaped)
        await r.readback_played()
        await r.ask("confirm")

    run(go())
    assert r.said_text()[-1] == said
    settled = [m for m in r.to_client if isinstance(m, dict) and m.get("t") == "settled"]
    assert settled[-1]["ok"] is ok


def test_an_older_needs_you_read_never_replaces_a_newer_challenge(monkeypatch):
    r = Rig(monkeypatch, NUDGE)
    r.needs_rows = [
        {"title": "old", "kind": "question",
         "action": {"id": "old-action", "verb": "answer", "can_approve": True,
                    "answer": "Use main."}},
    ]  # fmt: skip
    gate = asyncio.Event()
    real = asgi_call.call

    async def slow_needs(app, operator, method, path, body, **kw):
        if path == "/api/pulse/needs-you":
            await gate.wait()
        return await real(app, operator, method, path, body, **kw)

    monkeypatch.setattr(asgi_call, "call", slow_needs)

    async def go():
        await r.call.on_needs_you_request()  # the read-out waits on its GET …
        await asyncio.sleep(0)
        # … a newer question offers its own card (not `ask()`: that waits on the held read)
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Rerun the tests")
        await r.call.turn_task
        assert r.call.challenge["cards"][0]["id"] == "act-1"
        gate.set()  # the old read finishes last
        await r.side()
        assert r.call.challenge["cards"][0]["id"] == "act-1"
        await r.readback_played()
        await r.ask("confirm")

    run(go())
    posts = [p for m, p, _ in r.routes if m == "POST"]
    assert posts == ["/api/pulse/actions/act-1/approve"]


def test_an_old_read_back_paused_mid_send_never_arms_over_a_newer_one(monkeypatch):
    """Review 6867: the OLD Needs-you read-back's send is held; a newer read offers its card;
    the old send then completes. The challenge must stay the newer card."""
    r = Rig(monkeypatch)

    def row(name):
        return {"title": name, "kind": "question",
                "action": {"id": f"{name}-action", "verb": "answer", "can_approve": True,
                           "answer": "Use main."}}  # fmt: skip

    r.needs_rows = [row("old")]
    gate = asyncio.Event()
    plain = r.call._to_worker

    async def held(m):
        if isinstance(m, dict) and m.get("t") == "say" and "old asks" in str(m.get("text")):
            await gate.wait()  # the old read-back's send pauses here
        await plain(m)

    r.call._to_worker = held

    async def go():
        await r.call.on_needs_you_request()
        for _ in range(20):
            await asyncio.sleep(0)  # the old read reaches its read-back send, and waits
        r.needs_rows = [row("new")]
        await r.call.on_needs_you_request()
        while r.call.challenge is None or r.call.challenge["cards"][0]["id"] != "new-action":
            await asyncio.sleep(0)
        gate.set()  # the old send completes last
        await r.side()
        assert r.call.challenge["cards"][0]["id"] == "new-action"
        await r.readback_played()
        await r.ask("confirm")

    run(go())
    posts = [p for m, p, _ in r.routes if m == "POST"]
    assert posts == ["/api/pulse/actions/new-action/approve"]


@pytest.mark.parametrize("early_played", [False, True])
def test_markers_that_arrive_before_the_offer_settles_still_arm(monkeypatch, early_played):
    """Review 6869: the worker can send `said` (and the browser `played`) for the read-back
    while the read-back's own send is still awaited, before `offer` installs the challenge."""
    r = Rig(monkeypatch, NUDGE)
    plain = r.call._to_worker

    async def fast_worker(m):
        await plain(m)
        if isinstance(m, dict) and m.get("t") == "say" and "Say confirm" in str(m.get("text")):
            await r.call.on_worker_event({"t": "said", "id": m["id"]})  # before the send returns
            if early_played:
                await r.call.on_played(m["id"])

    r.call._to_worker = fast_worker

    async def go():
        await r.ask("Rerun the tests")
        ch = r.call.challenge
        assert ch is not None and ch["sent"] is True
        if not early_played:
            await r.call.on_played(ch["say_id"])
        assert r.call.challenge["armed_at"] is not None
        await r.ask("confirm")

    run(go())
    assert ("POST", "/api/pulse/actions/act-1/approve", None) in r.routes


@pytest.mark.parametrize("gap_event", ["talked_over", "interrupt", "mute", "tap"])
def test_an_interrupted_read_back_never_arms_however_the_awaits_resolve(monkeypatch, gap_event):
    """Review 6871: while the read-back's send is still awaited, the worker reports `said`, the
    Commander talks over the read-back before the browser acks `played` (the real worker sends
    `user_started` then `interrupt`; or an `interrupt` alone), or mutes, or taps the card after
    it. The challenge must stay void — and a later "confirm" must POST nothing."""
    r = Rig(monkeypatch, NUDGE)
    plain = r.call._to_worker

    async def racing_worker(m):
        await plain(m)
        if isinstance(m, dict) and m.get("t") == "say" and "Say confirm" in str(m.get("text")):
            await r.call.on_worker_event({"t": "said", "id": m["id"]})
            if gap_event == "talked_over":
                await r.ev(t="user_started")
            if gap_event in ("talked_over", "interrupt"):
                await r.ev(t="interrupt")
            await r.call.on_played(m["id"])
            if gap_event == "mute":
                r.call.muted = True
                await r.call.void_challenge()
                r.call.muted = False
            elif gap_event == "tap":
                await r.call.on_card_decided("act-1")

    r.call._to_worker = racing_worker

    async def go():
        await r.ask("Rerun the tests")
        assert r.call.challenge is None
        await r.ask("confirm")

    run(go())
    assert not any(m == "POST" for m, _, _ in r.routes)


@pytest.mark.parametrize("superseded_by", ["chip", "utterance"])
def test_a_new_question_whose_clear_is_paused_never_arms_an_older_card(monkeypatch, superseded_by):
    """Review 6874: a new question voids the pending challenge and its client send is held. The
    OLDER Needs-you read finishes during that pause and offers its card. However the awaits
    resolve, that card must never arm — a later "confirm" must not settle it."""
    r = Rig(monkeypatch)  # a typed question answers with no card

    def row(name):
        return {"title": name, "kind": "question",
                "action": {"id": f"{name}-action", "verb": "answer", "can_approve": True,
                           "answer": "Use main."}}  # fmt: skip

    r.needs_rows = [row("stale")]
    gets: list[asyncio.Event] = []  # one gate per Needs-you GET
    real = asgi_call.call

    async def held_get(app, operator, method, path, body, **kw):
        if path == "/api/pulse/needs-you":
            gate = asyncio.Event()
            gets.append(gate)
            await gate.wait()
        return await real(app, operator, method, path, body, **kw)

    monkeypatch.setattr(asgi_call, "call", held_get)
    clear_gate = asyncio.Event()
    hold = {"on": False}
    plain = r.call._to_client

    async def client(m):
        if hold["on"] and m == {"t": "challenge", "cards": []}:
            hold["on"] = False
            await clear_gate.wait()  # the new question's challenge-clear send pauses here
        await plain(m)

    r.call._to_client = client

    async def until(pred):
        for _ in range(200):
            if pred():
                return
            await asyncio.sleep(0)
        raise AssertionError("never happened")

    async def go():
        await r.call.on_needs_you_request()  # the OLD read: its GET waits
        await until(lambda: len(gets) == 1)
        # a challenge is pending (same generation), so the new question has one to clear
        await r.call.offer(voice_calls.needs_you_cards([row("pending")]), gen=r.call.generation)
        await r.readback_played()
        if superseded_by == "chip":
            hold["on"] = True
            newer = asyncio.create_task(r.call.on_needs_you_request())
        else:
            await r.ev(t="user_started")  # after arming: what follows is a reply, or a question
            hold["on"] = True
            newer = asyncio.create_task(r.ev(t="utterance", text="How is the docs refresh?"))
        await until(lambda: not hold["on"])  # the clear is paused …
        gets[0].set()  # … while the old read finishes and offers its card
        for _ in range(50):
            await asyncio.sleep(0)
        ch = r.call.challenge
        if ch is not None and ch["cards"][0]["id"] == "stale-action":
            await r.readback_played()  # its read-back plays out
        clear_gate.set()
        await newer
        if r.call.turn_task is not None:
            await r.call.turn_task
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="confirm")
        for _ in range(50):
            await asyncio.sleep(0)
        for g in gets:
            g.set()
        await r.side()
        if r.call.turn_task is not None:
            await r.call.turn_task

    run(go())
    assert ("POST", "/api/pulse/actions/stale-action/approve", None) not in r.routes


def test_the_turn_start_interrupt_never_voids_an_armed_challenge(monkeypatch):
    """Second review of #1631: Pipecat broadcasts an interruption at EVERY user turn start, so
    the worker sends `user_started` then `interrupt` before each utterance — a confirmation's too.
    An armed challenge's read-back has already played: nothing was talked over."""
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ev(t="user_started")
        await r.ev(t="interrupt")  # what the real worker sends next
        await r.ev(t="utterance", text="confirm")
        await r.side()

    run(go())
    assert ("POST", "/api/pulse/actions/act-1/approve", None) in r.routes
    assert r.asked == ["Rerun the tests"]


def test_a_turn_that_began_before_arming_never_confirms_after_a_newer_one_started(monkeypatch):
    """Second review of #1631: a turn's transcript arrives up to the STT grace after it ended, so
    a NEWER turn's `user_started` can come first. The older turn began before the challenge armed:
    it never confirms, whatever the latest `user_started` was."""
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ev(t="user_started")  # turn 1 asks …
        await r.ev(t="utterance", text="Rerun the tests")
        await r.ev(t="user_started")  # … turn 2 starts while HQ is still thinking (no challenge)
        await r.call.turn_task  # the read-back is offered …
        await r.readback_played()  # … and armed
        await r.ev(t="user_started")  # turn 3 starts after arming (Pipecat's turn-start interrupt)
        await r.ev(t="interrupt")
        await r.ev(t="utterance", text="go ahead", turn=2)  # turn 2's transcript, arriving now
        if r.call.turn_task is not None:
            await r.call.turn_task
        await r.side()

    run(go())
    assert not any(m == "POST" for m, _, _ in r.routes)


def test_an_utterance_without_a_turn_number_never_confirms(monkeypatch):
    """Fail closed: an utterance that cannot show which turn it was heard in cannot show it began
    after arming."""
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ev(t="user_started")
        await r.call.on_worker_event({"t": "utterance", "text": "confirm"})  # no "turn"
        if r.call.turn_task is not None:
            await r.call.turn_task
        await r.side()

    run(go())
    assert not any(m == "POST" for m, _, _ in r.routes)


# --- #1397 P3: the language gate --------------------------------------------------------------


def test_a_call_in_another_language_arms_hq_cards_for_the_judge(monkeypatch):
    """#1715 P1: a German call arms a card's challenge, and a German reply the English phrases
    don't cover is judged — the judge sees the read-back word for word and the reply."""
    r = Rig(monkeypatch, NUDGE)
    r.call.language = "de"
    r.call.detects_language = True
    r.verdict = ("confirm", 0)

    async def go():
        await r.ask("Starte die Tests neu")
        assert r.call.challenge is not None
        await r.readback_played()
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Ja, mach das", heard_language="de")
        await r.side()

    run(go())
    assert r.judged == [("A message to api-rebase: Rerun the auth tests. Say confirm to send it, "
                         "or cancel.", 1, "Ja, mach das")]  # fmt: skip
    assert ("POST", "/api/pulse/actions/act-1/approve", None) in r.routes
    assert r.asked == ["Starte die Tests neu"]


def test_a_needs_you_decision_on_another_language_call_stays_on_screen(monkeypatch):
    """P2 is held for its own approval: a Needs-you item keeps the English grammar, so on a German
    call it is read for the screen and arms nothing."""
    r = Rig(monkeypatch)
    r.call.language = "de"
    (card,) = voice_calls.confirmable_cards(NUDGE)
    card = {**card, "kind": "needs_you"}

    async def go():
        await r.call.offer([card])

    run(go())
    assert r.call.challenge is None
    assert r.said_text()[-1].endswith("Confirm it on your screen.")
    assert r.judged == []


@pytest.mark.parametrize(
    "heard,ok",
    [("en", True), ("de", False), (None, False)],  # detection on: unknown fails closed
)
def test_with_detection_only_an_english_confirmation_settles(monkeypatch, heard, ok):
    r = Rig(monkeypatch, NUDGE)
    r.call.detects_language = True

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="confirm", heard_language=heard)
        if r.call.turn_task is not None:
            await r.call.turn_task
        await r.side()

    run(go())
    approved = any(p.endswith("/approve") for _, p, _ in r.routes)
    assert approved is ok
    # The FAST PATH decided only where English was known; otherwise the reply went to the judge
    # (a nudge is a judged kind), which this rig answers `other`.
    assert (r.judged == []) is ok


def test_auto_without_detection_counts_as_english(monkeypatch):
    r = Rig(monkeypatch, NUDGE)
    r.call.detects_language = False

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ask("confirm")

    run(go())
    posts = [p for m, p, _ in r.routes if m == "POST"]
    assert posts == ["/api/pulse/actions/act-1/approve"]
    assert any(p.endswith("/approve") for _, p, _ in r.routes)


@pytest.mark.parametrize(
    "heard,ok",
    [("en", True), ("de", False), (None, False)],
)
def test_a_fixed_english_call_still_checks_what_was_heard(monkeypatch, heard, ok):
    """Review 103975: on a fixed-English call whose endpoint detects language, a "confirm" heard
    as German or unknown fails closed too — fixed English is not a licence to skip the check."""
    r = Rig(monkeypatch, NUDGE)
    r.call.fixed_language = r.call.language = "en"
    r.call.detects_language = True

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="confirm", heard_language=heard, language="en")
        if r.call.turn_task is not None:
            await r.call.turn_task

    run(go())
    assert any(p.endswith("/approve") for _, p, _ in r.routes) is ok
    assert (r.judged == []) is ok  # refused by the fast path: judged instead, never matched


def test_a_fixed_english_call_without_detection_counts_as_english(monkeypatch):
    r = Rig(monkeypatch, NUDGE)
    r.call.fixed_language = r.call.language = "en"
    r.call.detects_language = False

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ask("confirm")

    run(go())
    assert any(p.endswith("/approve") for _, p, _ in r.routes)


def test_a_superseded_offer_on_another_language_call_says_nothing(monkeypatch):
    """Second review of #1635: the screen-only read-back is fenced by the generation too."""
    r = Rig(monkeypatch)
    r.call.language = "de"
    r.call.generation = 5

    async def go():
        await r.call.offer(voice_calls.confirmable_cards(NUDGE), gen=4)

    run(go())
    assert r.says() == []


def test_an_untested_stt_endpoint_fails_spoken_confirmation_closed(monkeypatch):
    """Review 6887: detection unknown (never tested, or its endpoint changed) is not "assume
    English" — a "confirm" settles nothing."""
    r = Rig(monkeypatch, NUDGE)
    r.call.detects_language = None

    async def go():
        await r.ask("Rerun the tests")
        await r.readback_played()
        await r.ask("confirm")

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)
    assert [j[2] for j in r.judged] == ["confirm"]  # the fast path refused: only the judge saw it


# --- #1715 P1: the confirmation judge ----------------------------------------------------------


async def _armed(r, view_text: str = "Rerun the tests"):
    await r.ask(view_text)
    await r.readback_played()


async def _reply(r, text: str, **kw):
    """The Commander's reply: its own turn start (and Pipecat's interrupt with it), then its
    transcript — the order the worker sends them."""
    await r.ev(t="user_started")
    await r.ev(t="interrupt")
    await r.ev(t="utterance", text=text, **kw)


def test_the_fixed_phrases_decide_without_asking_the_judge(monkeypatch):
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await _armed(r)
        await r.ask("confirm")

    run(go())
    assert r.judged == [] and any(p.endswith("/approve") for _, p, _ in r.routes)


@pytest.mark.parametrize("words", ["passt, los", "yep, ship it", "mach das bitte"])
def test_a_judged_confirm_settles_through_the_cards_own_route(monkeypatch, words):
    r = Rig(monkeypatch, NUDGE)
    r.verdict = ("confirm", 0)

    async def go():
        await _armed(r)
        await _reply(r, words)
        await r.side()

    run(go())
    assert [p for m, p, _ in r.routes if m == "POST"] == ["/api/pulse/actions/act-1/approve"]
    assert r.call.challenge is None and r.asked == ["Rerun the tests"]
    captions = [m for m in r.to_client if m.get("t") == "caption" and m.get("text") == words]
    assert len(captions) == 1  # shown once, when heard


def test_a_judged_number_names_the_card_as_read_back(monkeypatch):
    r = Rig(monkeypatch, TWO)
    r.verdict = ("confirm", 1)

    async def go():
        await _armed(r)
        await _reply(r, "die zweite bitte")
        await r.side()

    run(go())
    posts = [p for m, p, _ in r.routes if m == "POST"]
    assert posts == ["/api/ask/proposals/prop-2/decide"]


def test_no_verdict_decides_nothing_and_asks_nothing(monkeypatch):
    r = Rig(monkeypatch, NUDGE)
    r.verdict = None

    async def go():
        await _armed(r)
        await _reply(r, "hmm, ja vielleicht")
        await r.side()

    run(go())
    assert not any(m == "POST" for m, _, _ in r.routes)
    assert r.asked == ["Rerun the tests"]  # not passed on as a question either
    assert r.said_text()[-1] == "I couldn't tell, Commander. It's on your screen."
    assert r.call.challenge is None


def test_a_judged_other_is_a_new_question_captioned_once(monkeypatch):
    r = Rig(monkeypatch, NUDGE)
    r.verdict = ("other", None)

    async def go():
        await _armed(r)
        await _reply(r, "Wie viele Sitzungen laufen?")
        await r.side()

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)
    assert r.asked == ["Rerun the tests", "Wie viele Sitzungen laufen?"]
    captions = [
        m
        for m in r.to_client
        if m.get("t") == "caption" and m.get("text") == "Wie viele Sitzungen laufen?"
    ]
    assert len(captions) == 1  # fmt: skip


def test_a_judged_cancel_leaves_it_on_screen(monkeypatch):
    r = Rig(monkeypatch, NUDGE)
    r.verdict = ("cancel", None)

    async def go():
        await _armed(r)
        await _reply(r, "nee, lass mal")
        await r.side()

    run(go())
    assert not any(m == "POST" for m, _, _ in r.routes)
    assert r.said_text()[-1] == "Okay. It stays on your screen."


def test_a_judged_ambiguous_reply_asks_which_and_rearms(monkeypatch):
    r = Rig(monkeypatch, TWO)
    r.verdict = ("ambiguous", None)

    async def go():
        await _armed(r)
        await _reply(r, "ja, mach")
        await r.side()
        assert r.said_text()[-1] == "Which one, Commander? Say confirm and the number."
        ch = r.call.challenge
        assert ch is not None and ch["armed_at"] is None  # re-arms once that line has played
        await r.readback_played()
        assert r.call.challenge["armed_at"] is not None

    run(go())
    assert not any(m == "POST" for m, _, _ in r.routes)


# The freshness fence: a delayed verdict lands after the situation changed — it decides nothing.


def _gated(r):
    gate = asyncio.Event()

    async def later():
        await gate.wait()
        return ("confirm", 0)

    r.verdict = later
    return gate


async def _race(r, change):
    gate = _gated(r)
    await _armed(r)
    await _reply(r, "ja, mach das")
    await asyncio.sleep(0)  # the judge is asked, and waits
    assert r.judged
    await change()
    gate.set()
    await r.side()


@pytest.mark.parametrize(
    "what",
    ["new-turn", "tap", "mute", "typed", "expired", "rearmed", "replaced", "new-question", "ended"],
)
def test_a_delayed_confirm_after_anything_newer_decides_nothing(monkeypatch, what):
    import time

    r = Rig(monkeypatch, NUDGE)

    async def change():
        ch = r.call.challenge
        if what == "new-turn":
            await r.ev(t="user_started")  # a SUBSEQUENT turn, after the reply's transcript
        elif what == "tap":
            await r.call.on_card_decided("act-1")
        elif what == "mute":
            r.call.muted = True
        elif what == "typed":
            r.call.on_typed_turn()
        elif what == "expired":
            ch["expires_at"] = time.monotonic() - 0.01
        elif what == "rearmed":
            ch["armed_at"] = ch["armed_at"] + 1.0  # a later arming (after a feedback line)
        elif what == "replaced":
            r.call.challenge = {**ch}  # a NEWER challenge, identical in every field but identity
        elif what == "new-question":
            r.call.generation += 1
        elif what == "ended":
            r.call.state = "ended"

    run(_race(r, change))
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)
    if what in ("mute", "typed", "expired", "rearmed", "replaced", "ended"):
        assert r.call.challenge is not None  # the stale verdict consumed nothing


def test_a_turn_begun_before_the_judge_task_starts_still_voids_its_verdict(monkeypatch):
    """Hermes 7282: the freshness snapshot is taken when the reply is HEARD, not when the judge's
    task first runs — a turn begun in between is newer than the reply."""
    r = Rig(monkeypatch, NUDGE)
    r.verdict = ("confirm", 0)

    async def go():
        await _armed(r)
        await _reply(r, "ja, mach das")
        # No await that yields: the judge's task has not started yet when the next turn begins.
        await r.ev(t="user_started")
        await r.side()

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


def test_a_turn_begun_while_the_caption_is_sent_still_voids_its_verdict(monkeypatch):
    """Hermes 7288: the snapshot comes before ANY await — a turn begun while the reply's caption is
    still being sent is newer than the reply."""
    r = Rig(monkeypatch, NUDGE)
    r.verdict = ("confirm", 0)
    held = asyncio.Event()
    release = asyncio.Event()
    plain = r.call._to_client

    async def slow_client(m):
        if m.get("t") == "caption" and m.get("text") == "ja, mach das":
            held.set()
            await release.wait()
        await plain(m)

    async def go():
        await _armed(r)
        r.call._to_client = slow_client
        await r.ev(t="user_started")
        reply = asyncio.create_task(r.ev(t="utterance", text="ja, mach das"))
        await held.wait()  # the caption send is in flight…
        await r.ev(t="user_started")  # …when the next turn begins
        release.set()
        await reply
        await r.side()

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


def test_a_transcript_overtaken_by_a_newer_turn_is_never_judged(monkeypatch):
    """Hermes 7288: the worker can flush turn N's transcript after turn N+1 has started. That reply
    is never judged — the newer turn is what the Commander means now."""
    r = Rig(monkeypatch, NUDGE)
    r.verdict = ("confirm", 0)

    async def go():
        await _armed(r)
        await r.ev(t="user_started")  # the reply's turn, N
        n = r.turn
        await r.ev(t="user_started")  # turn N+1 begins…
        await r.ev(t="utterance", text="ja, mach das", turn=n)  # …before N's transcript lands
        await r.side()

    run(go())
    assert r.judged == []
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


@pytest.mark.parametrize("path", ["judged", "fast"])
def test_a_turn_begun_before_the_settlement_still_voids_the_reply(monkeypatch, path):
    """Hermes 7310: the reply's snapshot is checked right before the card's route. A turn begun
    while the call is still clearing the challenge (after the verdict, before the route) is newer
    than the reply — judged or English fast path, nothing is settled."""
    r = Rig(monkeypatch, NUDGE)
    r.verdict = ("confirm", 0)
    held = asyncio.Event()
    release = asyncio.Event()
    plain = r.call._to_client

    async def slow_client(m):
        if m.get("t") == "challenge" and m.get("cards") == []:
            held.set()
            await release.wait()
        await plain(m)

    async def go():
        await _armed(r)
        r.call._to_client = slow_client
        await r.ev(t="user_started")
        words = "ja, mach das" if path == "judged" else "confirm"
        reply = asyncio.create_task(r.ev(t="utterance", text=words))
        side = asyncio.create_task(r.side())
        await held.wait()  # the verdict is in; the challenge-clear send is in flight…
        r.call._to_client = plain
        await r.ev(t="user_started")  # …when the next turn begins
        release.set()
        await reply
        await side
        await r.side()

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


@pytest.mark.parametrize("path", ["judged", "fast"])
def test_a_card_offered_before_the_settlement_voids_the_old_reply(monkeypatch, path):
    """Hermes 7318: no new turn, but a NEWER challenge is installed (another card offered) while
    the old verdict's settlement is still pending — the old card is never settled."""
    r = Rig(monkeypatch, NUDGE)
    r.verdict = ("confirm", 0)
    held = asyncio.Event()
    release = asyncio.Event()
    plain = r.call._to_client

    async def slow_client(m):
        if m.get("t") == "challenge" and m.get("cards") == []:
            held.set()
            await release.wait()
        await plain(m)

    async def go():
        await _armed(r)
        r.call._to_client = slow_client
        await r.ev(t="user_started")
        words = "ja, mach das" if path == "judged" else "confirm"
        reply = asyncio.create_task(r.ev(t="utterance", text=words))
        side = asyncio.create_task(r.side())
        await held.wait()
        r.call._to_client = plain
        other = voice_calls.confirmable_cards(TWO)[1:]  # a different card, same generation
        offered = asyncio.create_task(r.call.offer(other))
        await asyncio.sleep(0)
        assert r.call.challenge is not None and r.call.challenge["cards"] == other
        release.set()
        await asyncio.gather(reply, side, offered)
        await r.side()

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


@pytest.mark.parametrize("path", ["judged", "fast"])
def test_a_card_offered_then_tapped_before_the_settlement_voids_the_old_reply(monkeypatch, path):
    """Hermes 7336: a NEWER card is offered while the old verdict's settlement is pending, then
    tapped (voiding its challenge, so none is installed again) before the release. The newer
    offer still superseded the old reply: nothing it named is settled."""
    r = Rig(monkeypatch, NUDGE)
    r.verdict = ("confirm", 0)
    held = asyncio.Event()
    release = asyncio.Event()
    plain = r.call._to_client

    async def slow_client(m):
        if m.get("t") == "challenge" and m.get("cards") == []:
            held.set()
            await release.wait()
        await plain(m)

    async def go():
        await _armed(r)
        r.call._to_client = slow_client
        await r.ev(t="user_started")
        words = "ja, mach das" if path == "judged" else "confirm"
        reply = asyncio.create_task(r.ev(t="utterance", text=words))
        side = asyncio.create_task(r.side())
        await held.wait()
        r.call._to_client = plain
        other = voice_calls.confirmable_cards(TWO)[1:]  # a different card, same generation
        offered = asyncio.create_task(r.call.offer(other))
        await asyncio.sleep(0)
        assert r.call.challenge is not None and r.call.challenge["cards"] == other
        await r.call.on_card_decided(other[0]["id"])  # tapped on screen: its challenge voids
        assert r.call.challenge is None
        release.set()
        await asyncio.gather(reply, side, offered)
        await r.side()

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


def test_an_overtaken_fast_path_confirm_decides_nothing(monkeypatch):
    """Hermes 7336: the overtaken-transcript check runs before EITHER path. An English "confirm"
    whose turn N lands after turn N+1 began is a new question, never settled."""
    r = Rig(monkeypatch, NUDGE)

    async def go():
        await _armed(r)
        await r.ev(t="user_started")  # the reply's turn, N
        n = r.turn
        await r.ev(t="user_started")  # turn N+1 begins…
        await r.ev(t="utterance", text="confirm", turn=n)  # …before N's transcript lands
        await r.side()

    run(go())
    assert r.judged == []
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


def test_a_newer_question_before_the_settlement_voids_the_old_reply(monkeypatch):
    """No new turn and no newer challenge, but a NEWER question (the "What needs me?" chip bumps
    the generation) while the old verdict's settlement is pending: nothing is settled."""
    r = Rig(monkeypatch, NUDGE)
    held = asyncio.Event()
    release = asyncio.Event()
    plain = r.call._to_client

    async def slow_client(m):
        if m.get("t") == "challenge" and m.get("cards") == []:
            held.set()
            await release.wait()
        await plain(m)

    async def go():
        await _armed(r)
        r.call._to_client = slow_client
        await r.ev(t="user_started")
        reply = asyncio.create_task(r.ev(t="utterance", text="confirm"))
        side = asyncio.create_task(r.side())
        await held.wait()
        r.call._to_client = plain
        gen = r.call.generation
        await r.call.on_needs_you_request()
        assert r.call.generation == gen + 1 and r.call.challenge is None
        release.set()
        await asyncio.gather(reply, side)
        await r.side()

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


def _late_clock(monkeypatch, offset: list[float]):
    """`voice_calls`' own clock, `offset[0]` seconds ahead once a test moves it (the event loop's
    clock is untouched)."""
    import time as real

    shim = types.SimpleNamespace(
        **{k: getattr(real, k) for k in dir(real) if not k.startswith("_")},
    )
    shim.monotonic = lambda: real.monotonic() + offset[0]
    monkeypatch.setattr(voice_calls, "time", shim)


@pytest.mark.parametrize("what", ["typed", "expired"])
@pytest.mark.parametrize("path", ["judged", "fast"])
def test_typing_or_expiry_before_the_settlement_voids_the_reply(monkeypatch, path, what):
    """Independent review of a160b29d: the route-boundary fence's typed and window clauses had no
    test of their own. A turn TYPED, or the reply's window passing, while the challenge-clear send
    is in flight (after the verdict, before the route): nothing is settled."""
    r = Rig(monkeypatch, NUDGE)
    r.verdict = ("confirm", 0)
    offset = [0.0]
    _late_clock(monkeypatch, offset)
    held = asyncio.Event()
    release = asyncio.Event()
    plain = r.call._to_client

    async def slow_client(m):
        if m.get("t") == "challenge" and m.get("cards") == []:
            held.set()
            await release.wait()
        await plain(m)

    async def go():
        await _armed(r)
        r.call._to_client = slow_client
        await r.ev(t="user_started")
        words = "ja, mach das" if path == "judged" else "confirm"
        reply = asyncio.create_task(r.ev(t="utterance", text=words))
        side = asyncio.create_task(r.side())
        await held.wait()
        r.call._to_client = plain
        if what == "typed":
            assert r.call.on_typed_turn()
        else:
            offset[0] = voice_calls.CHALLENGE_S + 1
        release.set()
        await asyncio.gather(reply, side)
        await r.side()

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


@pytest.mark.parametrize("verdict", [("other", None), None])
def test_a_bare_turn_start_during_the_judgement_never_swallows_the_words(monkeypatch, verdict):
    """Independent review of a160b29d: `other` and no verdict authorise nothing, so a turn start
    with no words (room noise) while the judge thinks must not drop them silently. `other`: the
    words are asked; no verdict: HQ says it couldn't tell."""
    r = Rig(monkeypatch, NUDGE)
    judging = asyncio.Event()
    release = asyncio.Event()

    async def slow():
        judging.set()
        await release.wait()
        return verdict

    r.verdict = slow

    async def go():
        await _armed(r)
        await _reply(r, "Wie viele Sitzungen laufen?")
        side = asyncio.create_task(r.side())
        await judging.wait()
        await r.ev(t="user_started")  # noise: a turn start, and no transcript ever follows
        release.set()
        await side
        await r.side()

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)
    if verdict is None:
        assert r.call.challenge is None
        assert r.said_text()[-1] == "I couldn't tell, Commander. It's on your screen."
        assert "Wie viele Sitzungen laufen?" not in r.asked
    else:
        # Asked as a question (HQ's answer offers its card afresh: a NEW, unarmed challenge).
        assert r.asked[-1] == "Wie viele Sitzungen laufen?"
        assert r.call.challenge is None or r.call.challenge["armed_at"] is None


def test_the_judged_words_are_dropped_when_a_newer_turn_was_typed(monkeypatch):
    """…but a TYPED turn during the judgement is newer and was taken: the older spoken words are
    no longer what the Commander means, so they are not asked."""
    r = Rig(monkeypatch, NUDGE)
    judging = asyncio.Event()
    release = asyncio.Event()

    async def slow():
        judging.set()
        await release.wait()
        return ("other", None)

    r.verdict = slow

    async def go():
        await _armed(r)
        await _reply(r, "Wie viele Sitzungen laufen?")
        side = asyncio.create_task(r.side())
        await judging.wait()
        assert r.call.on_typed_turn()
        release.set()
        await side
        await r.side()

    run(go())
    assert "Wie viele Sitzungen laufen?" not in r.asked
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


def _held_judge(r, verdicts):
    """Each judgement waits for its own release; `verdicts[i]` is what judgement i answers."""
    started = [asyncio.Event() for _ in verdicts]
    release = [asyncio.Event() for _ in verdicts]
    n = [0]

    async def judge():
        i = n[0]
        n[0] += 1
        started[i].set()
        await release[i].wait()
        return verdicts[i]

    r.verdict = judge
    return started, release


@pytest.mark.parametrize("first_back", ["older", "newer"])
def test_two_non_answers_in_one_judgement_window_never_lose_the_newer(monkeypatch, first_back):
    """Independent review of 834d1045: the Commander says two things while a card is armed (Pipecat
    splits one request at a pause), and both are judged `other`. Whichever verdict comes back
    first, the NEWER words are asked last — the older ones never displace them."""
    r = Rig(monkeypatch, NUDGE)
    started, release = _held_judge(r, [("other", None), ("other", None)])

    async def go():
        await _armed(r)
        await _reply(r, "Erste Frage?")
        side = asyncio.create_task(r.side())
        await started[0].wait()
        await _reply(r, "Zweite Frage?")
        await started[1].wait()
        order = [0, 1] if first_back == "older" else [1, 0]
        for i in order:
            release[i].set()
            for _ in range(5):
                await asyncio.sleep(0)
        await side
        await r.side()

    run(go())
    assert r.asked[-1] == "Zweite Frage?"
    if first_back == "newer":
        assert "Erste Frage?" not in r.asked  # older words, after newer ones: stepped aside
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


@pytest.mark.parametrize("path", ["fast", "judged"])
def test_older_words_are_not_asked_after_a_newer_confirmation(monkeypatch, path):
    """Hermes 7359: a question still being judged, then a newer reply CONFIRMS the card (the English
    fast path, or a judged confirm). The older judgement's `other` must not then ask its words: the
    Commander has acted since, and those words would start a new HQ turn after "Sent."."""
    r = Rig(monkeypatch, NUDGE)
    verdicts = [("other", None)] if path == "fast" else [("other", None), ("confirm", 0)]
    started, release = _held_judge(r, verdicts)

    async def go():
        await _armed(r)
        await _reply(r, "Erste Frage?")
        side = asyncio.create_task(r.side())
        await started[0].wait()
        if path == "fast":
            await _reply(r, "confirm")
        else:
            await _reply(r, "ja, mach das")
            await started[1].wait()
            release[1].set()
        for _ in range(20):
            await asyncio.sleep(0)
        release[0].set()
        await side
        await r.side()

    run(go())
    assert any(p.endswith("/approve") for _, p, _ in r.routes)
    assert "Erste Frage?" not in r.asked


def test_the_chip_during_the_judgement_drops_the_older_words(monkeypatch):
    """The "What needs me?" chip pressed while a reply is judged is a newer request: the judged
    non-answer steps aside instead of being asked after it."""
    r = Rig(monkeypatch, NUDGE)
    started, release = _held_judge(r, [("other", None)])

    async def go():
        await _armed(r)
        await _reply(r, "Wie viele Sitzungen laufen?")
        side = asyncio.create_task(r.side())
        await started[0].wait()
        await r.call.on_needs_you_request()
        release[0].set()
        await side
        await r.side()

    run(go())
    assert "Wie viele Sitzungen laufen?" not in r.asked


def test_no_verdict_after_a_newer_confirm_says_nothing_false(monkeypatch):
    """Independent review of 834d1045: a reply still being judged, then a newer English "confirm"
    settles the card ("Sent."). The older judgement's no-verdict must not then say the card is
    on the screen."""
    r = Rig(monkeypatch, NUDGE)
    started, release = _held_judge(r, [None])

    async def go():
        await _armed(r)
        await _reply(r, "hmm, vielleicht")
        side = asyncio.create_task(r.side())
        await started[0].wait()
        await _reply(r, "confirm")
        for _ in range(20):
            await asyncio.sleep(0)
        release[0].set()
        await side
        await r.side()

    run(go())
    assert any(p.endswith("/approve") for _, p, _ in r.routes)
    assert "I couldn't tell, Commander. It's on your screen." not in r.said_text()


def test_judged_words_take_their_place_before_a_newer_question(monkeypatch):
    """Independent review of a160b29d: `other` used to await the challenge-clear send BEFORE the
    words took their generation, so a newer question landing in that send was asked first and the
    older words after it. They now take their place first."""
    r = Rig(monkeypatch, NUDGE)
    held = asyncio.Event()
    release = asyncio.Event()
    plain = r.call._to_client

    async def slow_client(m):
        if m.get("t") == "challenge" and m.get("cards") == [] and not release.is_set():
            held.set()
            await release.wait()
        await plain(m)

    async def go():
        await _armed(r)
        r.call._to_client = slow_client
        await _reply(r, "Erste Frage?")
        side = asyncio.create_task(r.side())
        await held.wait()
        r.call._to_client = plain
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Zweite Frage?")
        release.set()
        await side
        await r.side()

    run(go())
    assert r.asked[-2:] == ["Erste Frage?", "Zweite Frage?"]


def test_the_replys_own_interrupt_never_voids_its_verdict(monkeypatch):
    """Pipecat interrupts at EVERY turn start, the reply's own included (before its transcript):
    that is not a newer turn, and the judged confirm acts."""
    r = Rig(monkeypatch, NUDGE)

    async def nothing():
        return None

    run(_race(r, nothing))
    assert ("POST", "/api/pulse/actions/act-1/approve", None) in r.routes
