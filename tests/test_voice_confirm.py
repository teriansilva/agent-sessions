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
* an HQ operation that needs on-screen input is never offered; Needs-you offers only text answers
  to questions — never permission prompts or menus (what the read-out SAYS about each item:
  `test_voice_needs_you.py`).
"""

from __future__ import annotations

import asyncio

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
        """Settlements and Needs-you read-outs run beside the relay loop: wait for them."""
        while self.call.side_tasks:
            await asyncio.gather(*list(self.call.side_tasks), return_exceptions=True)

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
         "needs": [], "text": "Archives the session api: its agent stops."},
    ]}  # fmt: skip
    cards = voice_calls.confirmable_cards(view)
    assert [c["id"] for c in cards] == ["op-2"]


def test_needs_you_offers_only_text_answers_to_questions(monkeypatch):
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
            "3 things need you."
            " deploy wants you to confirm something I can't read out — it's on your screen."
            " docs is showing a menu I can't read out — it's on your screen."
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


def test_a_call_in_another_language_never_arms_a_spoken_confirmation(monkeypatch):
    """The matcher is English-only: a German call reads the card for the screen and arms nothing,
    so "bestätigen" — or a misheard English word — cannot settle it."""
    r = Rig(monkeypatch, NUDGE)
    r.call.language = "de"

    async def go():
        await r.ask("Starte die Tests neu")
        assert r.call.challenge is None
        assert r.said_text()[-1].endswith("Confirm it on your screen.")
        await r.ask("confirm")

    run(go())
    assert not any(p.endswith("/approve") for _, p, _ in r.routes)


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
