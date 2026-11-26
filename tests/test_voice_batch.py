"""#2389 — one yes covers a batch, short batch read-backs, and open cards come back when asked.

Every test drives the REAL call path (`test_voice_confirm.Rig`): an HQ turn settles with cards,
the call reads them back, the read-back plays and arms the challenge, the Commander's reply is
classified (fast path) or judged (the judge faked, its answer parsed by the real parser), and each
card goes to its own route as the operator. Pinned:

* both four-card caps are gone for judged cards: twelve renames are offered whole, the announced
  count is exactly the challenge's id set, and the twelve routes run in that order;
* a batch is read back in ONE short sentence; one card is still read in full;
* "do them all" / a plain yes settles every card through its own route, with ONE summary; a tap
  skips only its card; the Commander speaking stops the rest (not attempted); a failed route is
  reported and the batch goes on; a 202 is handed to the call's one watcher without holding the
  next card; a timed-out route is unknown and never retried; a timely yes keeps admitting past
  `CHALLENGE_S`; a batch holding a connector message keeps the grammar;
* the judge is shown the challenge's cards as bounded descriptors, and picks cards 10 and 20;
  0, 21 and numbers outside the set authorise nothing;
* `offer_cards` puts open cards of THIS conversation back, refusing everything else, and the
  call re-checks each card in the store right before its route.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from agent_sessions import (
    asgi_call,
    ask_orchestrator,
    ask_proposals,
    ask_store,
    voice_calls,
    voice_judge,
    voice_text,
)
from test_voice_confirm import Rig, run


def _pid(i: int) -> str:
    return f"{i:032x}"


TITLES = [
    "Website: Romanian and Hungarian languages",
    "Rebasing the stranded skill-import PR onto main",
    "Fixing hidden answer options in the question dock",
    "Docs refresh for the install page",
    "Auth tests on the api-rebase branch",
    "Landing page copy pass",
    "Federation handshake retries",
    "Mission composer checklist picker",
    "Scrollback race in the buffers",
    "Release notes for 0.21",
    "Terminal touch scroll on phones",
    "Pulse ask retrieval tuning",
    "Voice cue pitch check",
    "Map window overlay",
    "Session clusters topics",
    "Notification volume",
    "Relay admin controls",
    "Onboarding overhaul",
    "Settings panels refresh",
    "Connect page cache",
    "Handoff seed readiness",
    "Hermes review fallback",
]


def renames(n: int, start: int = 1) -> list[dict]:
    return [
        {
            "kind": "operation",
            "action_id": _pid(i),
            "op": "session.rename",
            "title": f"# Handoff {i}",
            "text": f"Renames it to “{TITLES[(i - 1) % len(TITLES)]}”.",
            "label": "Rename",
            "verb": "Rename",
            "needs": [],
        }
        for i in range(start, start + n)
    ]


SESSION = {"kind": "session", "action_id": _pid(900), "title": "docs refresh",
           "text": "Refresh the install docs."}  # fmt: skip
MESSAGE = {"kind": "message", "action_id": _pid(901), "recipient": "#ops",
           "connector": "Matrix", "text": "Deploy is done."}  # fmt: skip


def decided(r: Rig) -> list[str]:
    """The cards whose decide route ran, in order."""
    return [p.split("/")[4] for m, p, _ in r.routes if m == "POST" and p.endswith("/decide")]


def challenges(r: Rig) -> list[dict]:
    return [m for m in r.to_client if isinstance(m, dict) and m.get("t") == "challenge"]


def outcomes(r: Rig) -> list[str]:
    return [
        m["text"]
        for m in r.to_client
        if isinstance(m, dict) and m.get("t") == "caption" and m.get("kind") == "outcome"
    ]


def routed(r: Rig, script):
    """Replace the Rig's route with `script(card_id, rig) -> Response` (awaitable allowed)."""

    async def fake_call(app, operator, method, path, body, **kw):
        r.routes.append((method, path, body))
        if method == "GET":
            return asgi_call.Response(200, {"rows": []})
        got = script(path.split("/")[4], r)
        return await got if asyncio.iscoroutine(got) else got

    return fake_call


def done_resp() -> asgi_call.Response:
    return asgi_call.Response(200, {"proposal": {"state": "done"}})


async def offered(r: Rig, request: str = "rename them all") -> None:
    await r.ask(request)
    await r.readback_played()


# --- both caps ----------------------------------------------------------------------------------


def test_twelve_renames_are_offered_whole_and_one_yes_settles_exactly_that_set(monkeypatch):
    cards = renames(12)
    r = Rig(monkeypatch, {"proposals": cards})

    async def go():
        await offered(r)
        assert r.said_text()[-1] == (
            "Twelve renames, the details are on your screen. Shall I go ahead with all twelve?"
        )
        armed = [c for c in challenges(r) if c["cards"]][-1]
        assert armed["cards"] == [c["action_id"] for c in cards]  # announced == authorised
        assert armed["batch"] is True
        await r.ask("Yes, do them all.")

    run(go())
    assert decided(r) == [c["action_id"] for c in cards]  # every card, in order, once
    assert all(b == {"decision": "confirm"} for _, _, b in r.routes)
    assert outcomes(r) == ["Done, all twelve."]  # ONE summary, no per-card line
    assert r.asked == ["rename them all"]


def test_cards_past_the_batch_cap_stay_on_the_screen(monkeypatch):
    cards = renames(22)
    r = Rig(monkeypatch, {"proposals": cards})

    async def go():
        await offered(r)
        armed = [c for c in challenges(r) if c["cards"]][-1]
        assert armed["cards"] == [c["action_id"] for c in cards[: voice_calls.BATCH_MAX]]
        assert r.said_text()[-1].startswith("Twenty renames,")
        await r.ask("yes")

    run(go())
    assert decided(r) == [c["action_id"] for c in cards[:20]]


def test_one_card_is_still_read_in_full(monkeypatch):
    r = Rig(monkeypatch, {"proposals": renames(1)})

    async def go():
        await offered(r)
        assert r.said_text()[-1] == (
            "Rename, for # Handoff 1: Renames it to “Website: Romanian and Hungarian "
            "languages”. Shall I go ahead?"
        )

    run(go())


# --- the batch coordinator ----------------------------------------------------------------------


def test_three_cards_done_in_order_with_one_summary(monkeypatch):
    cards = renames(3)
    r = Rig(monkeypatch, {"proposals": cards})

    async def go():
        await offered(r)
        assert r.said_text()[-1] == (
            "Three renames, the details are on your screen. Shall I go ahead with all three?"
        )
        before = len(r.said_text())
        await r.ask("do them all")
        assert r.said_text()[before:] == ["Done, all three."]

    run(go())
    assert decided(r) == [c["action_id"] for c in cards]


def test_a_tap_on_card_two_mid_batch_skips_only_it(monkeypatch):
    cards = renames(3)
    r = Rig(monkeypatch, {"proposals": cards})

    async def script(pid, rig):
        if pid == cards[0]["action_id"]:
            await rig.call.on_card_decided(cards[1]["action_id"])  # tapped while card 1 runs
        return done_resp()

    monkeypatch.setattr(asgi_call, "call", routed(r, script))

    async def go():
        await offered(r)
        await r.ask("yes")

    run(go())
    assert decided(r) == [cards[0]["action_id"], cards[2]["action_id"]]
    assert outcomes(r) == ["Two done and one you'd tapped already."]


def test_the_commander_speaking_mid_batch_stops_the_rest(monkeypatch):
    cards = renames(3)
    r = Rig(monkeypatch, {"proposals": cards})

    async def script(pid, rig):
        if pid == cards[0]["action_id"]:
            await rig.ev(t="user_started")  # they start speaking while card 1 runs
        return done_resp()

    monkeypatch.setattr(asgi_call, "call", routed(r, script))

    async def go():
        await offered(r)
        await r.ask("yes")

    run(go())
    assert decided(r) == [cards[0]["action_id"]]
    assert outcomes(r) == ["One done and two not started. I stopped there."]


class _Typed:
    """A typed turn's task, as the HQ route hands it to the call."""


def test_typing_or_mute_mid_batch_stops_the_rest(monkeypatch):
    for stop in ("typed", "mute"):
        cards = renames(3)
        r = Rig(monkeypatch, {"proposals": cards})

        async def script(pid, rig, stop=stop, first=cards[0]["action_id"]):
            if pid == first:
                if stop == "typed":
                    rig.call.on_typed_turn(_Typed())
                else:
                    rig.call.on_mute(True)
            return done_resp()

        monkeypatch.setattr(asgi_call, "call", routed(r, script))

        async def go(r=r):
            await offered(r)
            await r.ask("yes")

        run(go())
        assert decided(r) == [cards[0]["action_id"]], stop


def test_a_mute_and_unmute_mid_batch_still_stops_the_rest(monkeypatch):
    """Hermes 8975: a mute revokes the yes for good — unmuting before card 1's route returns
    must not let cards 2 and 3 run without a fresh confirmation."""
    cards = renames(3)
    r = Rig(monkeypatch, {"proposals": cards})

    async def script(pid, rig):
        if pid == cards[0]["action_id"]:
            rig.call.on_mute(True)  # the socket's own mute path (`routes/voice.py`)
            await rig.call.void_challenge()
            rig.call.on_mute(False)  # …and unmuted again while the route still runs
        return done_resp()

    monkeypatch.setattr(asgi_call, "call", routed(r, script))

    async def go():
        await offered(r)
        await r.ask("yes")

    run(go())
    assert decided(r) == [cards[0]["action_id"]]
    assert outcomes(r) == ["One done and two not started. I stopped there."]


def test_a_failed_route_is_reported_and_the_batch_goes_on(monkeypatch):
    cards = renames(3)
    r = Rig(monkeypatch, {"proposals": cards})

    def script(pid, rig):
        if pid == cards[1]["action_id"]:
            return asgi_call.Response(409, {"detail": "busy"})
        return done_resp()

    monkeypatch.setattr(asgi_call, "call", routed(r, script))
    # Re-read before the summary: the failed card is still pending, so it is on the screen.
    monkeypatch.setattr(
        ask_proposals, "views", lambda cid: ("ok", {cards[1]["action_id"]: {"state": "pending"}})
    )

    async def go():
        await offered(r)
        await r.ask("yes")

    run(go())
    assert decided(r) == [c["action_id"] for c in cards]
    assert outcomes(r) == ["Two done and one didn't go through. That one is still on your screen."]
    settled = {m["card"]: m for m in r.to_client if isinstance(m, dict) and m.get("t") == "settled"}
    assert settled[cards[1]["action_id"]]["ok"] is False
    assert settled[cards[1]["action_id"]]["status"] == 409


def test_a_failed_card_no_longer_pending_is_not_said_to_be_on_screen(monkeypatch):
    cards = renames(2)
    r = Rig(monkeypatch, {"proposals": cards})
    monkeypatch.setattr(
        asgi_call, "call", routed(r, lambda pid, rig: asgi_call.Response(409, {"detail": "x"}))
    )
    monkeypatch.setattr(ask_proposals, "views", lambda cid: ("ok", {}))

    async def go():
        await offered(r)
        await r.ask("yes")

    run(go())
    assert outcomes(r) == ["Two didn't go through."]


def test_a_202_card_still_running_past_its_bound_goes_to_the_watcher(monkeypatch):
    """A slow outcome never holds the batch past the card's own bound: a launch still running
    after `ROUTE_TIMEOUT_S` is handed to the call's one watcher, and the next card goes on."""
    monkeypatch.setattr(voice_calls, "OUTCOME_EVERY_S", 0.01)
    monkeypatch.setattr(voice_calls, "ROUTE_TIMEOUT_S", 0.1)
    monkeypatch.setattr(voice_calls, "ACCEPTED_POLL_S", 0.01)
    cards = [SESSION, *renames(2)]
    r = Rig(monkeypatch, {"proposals": cards})
    states = {SESSION["action_id"]: "deciding"}
    reads: list[float] = []

    def views(cid):
        reads.append(1)
        return "ok", {k: {"state": v} for k, v in states.items()}

    monkeypatch.setattr(ask_proposals, "views", views)

    def script(pid, rig):
        if pid == SESSION["action_id"]:
            return asgi_call.Response(202, {"proposal": {"state": "deciding"}})
        return done_resp()

    monkeypatch.setattr(asgi_call, "call", routed(r, script))

    async def go():
        await offered(r)
        assert r.said_text()[-1] == (
            "One new session and two renames, the details are on your screen. "
            "Shall I go ahead with all three?"
        )
        await r.ask("yes")
        # The renames ran without waiting for the session to come up; one watcher follows it.
        assert decided(r) == [c["action_id"] for c in cards]
        assert outcomes(r) == ["Two done and one still running."]
        assert list(r.call._watched) == [SESSION["action_id"]]
        watcher = r.call._watcher
        assert watcher is not None and not watcher.done()
        states[SESSION["action_id"]] = "started"
        await asyncio.wait_for(watcher, 5)

    run(go())
    finals = [m for m in r.to_client if isinstance(m, dict) and m.get("final") == "started"]
    assert [m["card"] for m in finals] == [SESSION["action_id"]]  # its end, told once
    assert not r.call._watched


def test_repeated_202_batches_share_one_watcher_and_one_read_per_tick(monkeypatch):
    """Hermes on #2389: a TOTAL per-call bound on followers, and bounded store reads across
    repeated 202-heavy batches — not just one follower per card."""
    monkeypatch.setattr(voice_calls, "OUTCOME_EVERY_S", 0.02)
    monkeypatch.setattr(voice_calls, "FOLLOWED_MAX", 6)
    monkeypatch.setattr(voice_calls, "ROUTE_TIMEOUT_S", 0.02)
    monkeypatch.setattr(voice_calls, "ACCEPTED_POLL_S", 0.01)
    reads: list[int] = []

    def views(cid):
        reads.append(1)
        return "ok", {}  # every card still unreadable: they stay followed

    monkeypatch.setattr(ask_proposals, "views", views)
    batches = [
        [{**SESSION, "action_id": _pid(1000 + b * 10 + i), "title": f"s{b}{i}"} for i in range(4)]
        for b in range(3)
    ]
    r = Rig(monkeypatch)
    monkeypatch.setattr(
        asgi_call,
        "call",
        routed(r, lambda pid, rig: asgi_call.Response(202, {"proposal": {"state": "deciding"}})),
    )

    async def go():
        for cards in batches:
            r.view = {"proposals": cards}
            await offered(r, "start them")
            await r.ask("yes")
        assert len(decided(r)) == 12
        assert len(r.call._watched) == 6  # the call's total bound
        assert r.call._followers == {}
        n0 = len(reads)
        await asyncio.sleep(0.2)
        # ONE watcher, one store read per tick for all six cards (≈10 ticks), never one per card.
        assert len(reads) - n0 <= 12
        tasks = [t for t in asyncio.all_tasks() if t.get_coro().__name__ == "_watch_loop"]
        assert len(tasks) == 1
        await r.call.end("test")
        await asyncio.sleep(0)

    run(go())


def test_accepted_cards_wait_for_the_decide_lock_and_are_in_the_one_summary(monkeypatch):
    """An HQ operation's decide route answers 202 and runs as a server task holding the
    conversation's decide lock: a second decide meanwhile is refused (the drill found ten of
    twelve renames refused that way). The next card waits for it, within its own bound, and the
    ends are said in the ONE summary, never in lines of their own."""
    monkeypatch.setattr(voice_calls, "OUTCOME_EVERY_S", 0.01)
    monkeypatch.setattr(voice_calls, "ACCEPTED_POLL_S", 0.01)
    cards = renames(3)
    r = Rig(monkeypatch, {"proposals": cards})
    states: dict[str, str] = {}

    async def finish(pid):
        await asyncio.sleep(0.05)  # the rename's server task
        states[pid] = "done"

    def script(pid, rig):
        if "deciding" in states.values():
            # `ask_proposals._decide_lock`: another decision of this conversation is running.
            return asgi_call.Response(409, {"detail": "still starting"})
        states[pid] = "deciding"
        asyncio.get_running_loop().create_task(finish(pid))
        return asgi_call.Response(202, {"proposal": {"state": "deciding"}})

    monkeypatch.setattr(asgi_call, "call", routed(r, script))
    monkeypatch.setattr(
        ask_proposals, "views", lambda cid: ("ok", {k: {"state": v} for k, v in states.items()})
    )

    async def go():
        await offered(r)
        await r.ask("yes")
        await asyncio.sleep(0.1)  # a watcher, had there been one, would have spoken by now

    run(go())
    assert decided(r) == [c["action_id"] for c in cards]
    assert outcomes(r) == ["Done, all three."]  # ONE line for all three
    assert not r.call._watched
    finals = [m["card"] for m in r.to_client if isinstance(m, dict) and m.get("final") == "done"]
    assert finals == [c["action_id"] for c in cards]  # the screen still gets each end


def test_a_timed_out_route_is_unknown_and_never_retried(monkeypatch):
    monkeypatch.setattr(voice_calls, "ROUTE_TIMEOUT_S", 0.05)
    cards = renames(3)
    r = Rig(monkeypatch, {"proposals": cards})
    finished: list[str] = []
    release: dict = {}

    async def script(pid, rig):
        if pid == cards[1]["action_id"]:
            await release["route"].wait()  # held until the test lets it finish
            finished.append(pid)
        return done_resp()

    monkeypatch.setattr(asgi_call, "call", routed(r, script))
    monkeypatch.setattr(ask_proposals, "views", lambda cid: ("ok", {}))

    async def go():
        release["route"] = asyncio.Event()
        await offered(r)
        await r.ask("yes")
        # The batch went on to card 3 and said its summary while card 2 was still running.
        assert decided(r) == [c["action_id"] for c in cards]
        assert finished == []
        assert outcomes(r) == ["Two done and one I can't tell yet."]
        release["route"].set()  # the slow route finishes on its own…
        for _ in range(100):
            if finished:
                break
            await asyncio.sleep(0.01)

    run(go())
    assert finished == [cards[1]["action_id"]]
    assert decided(r).count(cards[1]["action_id"]) == 1  # …and is never run again
    unknown = [m for m in r.to_client if isinstance(m, dict) and m.get("unknown")]
    assert [m["card"] for m in unknown] == [cards[1]["action_id"]]


def test_a_timely_yes_keeps_admitting_past_the_challenge_window(monkeypatch):
    monkeypatch.setattr(voice_calls, "CHALLENGE_S", 0.6)
    cards = renames(4)
    r = Rig(monkeypatch, {"proposals": cards})

    async def script(pid, rig):
        await asyncio.sleep(0.3)  # four cards: twice the window
        return done_resp()

    monkeypatch.setattr(asgi_call, "call", routed(r, script))

    async def go():
        await offered(r)
        await r.ask("yes")

    run(go())
    assert decided(r) == [c["action_id"] for c in cards]
    assert outcomes(r) == ["Done, all four."]


def test_a_late_yes_is_still_held_to_the_window(monkeypatch):
    """The window still bounds the yes itself: a reply heard after it settles nothing."""
    cards = renames(3)
    r = Rig(monkeypatch, {"proposals": cards})

    async def go():
        await offered(r)
        r.call.challenge["expires_at"] = 0.0  # the window has passed
        await r.ask("yes")

    run(go())
    assert decided(r) == []


def test_a_batch_holding_a_connector_message_keeps_the_grammar(monkeypatch):
    cards = [MESSAGE, *renames(1)]
    r = Rig(monkeypatch, {"proposals": cards})

    async def go():
        await offered(r)
        said = r.said_text()[-1]
        assert said.startswith("First: A message to #ops on Matrix: Deploy is done.")
        assert said.endswith("Confirm the first or the second, or cancel?")
        await r.ask("do them all")
        assert r.said_text()[-1] == (
            "I can only do one at a time. Confirm the first or the second, or cancel?"
        )

    run(go())
    assert decided(r) == []


@pytest.mark.parametrize("said", ["dismiss all", "drop them all", "dismiss everything"])
def test_dismiss_all_is_refused_and_never_the_batchs_yes(monkeypatch, said):
    r = Rig(monkeypatch, {"proposals": renames(3)})

    async def go():
        await offered(r)
        await r.ask(said)

    run(go())
    assert r.routes == [] or all(m == "GET" for m, _, _ in r.routes)
    assert r.said_text()[-1] == "I can only drop one at a time. Which one should I drop?"


@pytest.mark.parametrize("first", ["dismiss it", "dismiss all"])
@pytest.mark.parametrize("then", ["yes", "go ahead", "do them all", "Ja, alle"])
def test_a_yes_to_hqs_which_one_to_drop_never_runs_the_batch(monkeypatch, first, then):
    """Hermes 8973: after "Which one should I drop?" the batch question no longer stands — a
    yes answers the clarification, never "go ahead with all three"; the judge sees that line."""
    r = Rig(monkeypatch, {"proposals": renames(3)})
    r.verdict = ("all", None)  # even a judge that hears "all" there: it is "drop them all"

    async def go():
        await offered(r)
        await r.ask(first)
        assert r.said_text()[-1].endswith("Which one should I drop?")
        await r.readback_played()  # the clarification played: the challenge re-armed
        await r.ask(then)

    run(go())
    assert not [b for m, _, b in r.routes if m == "POST"]  # nothing confirmed, nothing dropped
    for readback, _n, _reply in r.judged:
        assert readback.endswith("Which one should I drop?")


def test_after_which_one_do_you_mean_a_bare_yes_is_not_the_batch(monkeypatch):
    r = Rig(monkeypatch, {"proposals": renames(3)})
    r.verdict = ("ambiguous", None)

    async def go():
        await offered(r)
        await r.ask("the handoff one")  # judged: fits several cards
        assert r.said_text()[-1] == "Which one do you mean?"
        await r.readback_played()
        r.verdict = ("other", None)
        await r.ask("yes")

    run(go())
    assert not [b for m, _, b in r.routes if m == "POST"]
    assert r.judged[-1][0] == "Which one do you mean?"  # the judge saw the line just asked


def test_a_judged_all_settles_the_batch(monkeypatch):
    """A reply the fast path doesn't cover ("Ja, alle") goes to the judge: its `all` is a batch."""
    cards = renames(3)
    r = Rig(monkeypatch, {"proposals": cards})
    r.verdict = ("all", None)

    async def go():
        await offered(r)
        await r.ask("Ja, mach sie alle")

    run(go())
    assert decided(r) == [c["action_id"] for c in cards]
    assert outcomes(r) == ["Done, all three."]


def test_a_verdict_judged_after_a_newer_turn_settles_nothing(monkeypatch):
    """The whole fence at acceptance: the Commander spoke again while the judge was out."""
    cards = renames(3)
    r = Rig(monkeypatch, {"proposals": cards})

    async def slow():
        await r.ev(t="user_started")  # a newer turn began during the judgement
        return ("all", None)

    r.verdict = slow

    async def go():
        await offered(r)
        await r.ask("Ja, mach sie alle")

    run(go())
    assert decided(r) == []


# --- the judge sees every card ------------------------------------------------------------------


def judge_answers(r: Rig, answer: str):
    """The judge faked at the MODEL: its raw answer goes through the real parser."""

    async def v():
        return voice_judge.parse_card_verdict(answer, r.judged[-1][1])

    r.verdict = v


def test_the_judge_is_shown_each_card_and_a_uniquely_named_one_is_chosen(monkeypatch):
    cards = renames(12)
    r = Rig(monkeypatch, {"proposals": cards})
    judge_answers(r, "confirm 7")  # "Federation handshake retries" is card 7

    async def go():
        await offered(r)
        await r.ask("just the federation one")

    run(go())
    (listed,) = r.listed
    assert [d["n"] for d in listed] == list(range(1, 13))
    assert listed[6]["target"] == "# Handoff 7"
    assert "Federation handshake retries" in listed[6]["change"]
    assert all(len(d["change"]) <= voice_judge.DESCRIPTOR_MAX for d in listed)
    assert decided(r) == [cards[6]["action_id"]]
    assert r.said_text()[-1] == "Done."


def test_an_ambiguous_name_settles_nothing_and_asks_without_a_menu(monkeypatch):
    r = Rig(monkeypatch, {"proposals": renames(12)})
    judge_answers(r, "confirm")  # the words fit more than one card

    async def go():
        await offered(r)
        await r.ask("the handoff one")

    run(go())
    assert decided(r) == []
    assert r.said_text()[-1] == "Which one do you mean?"


@pytest.mark.parametrize(("n", "answer"), [(12, "confirm 10"), (20, "confirm 20")])
def test_cards_ten_and_twenty_can_be_chosen(monkeypatch, n, answer):
    cards = renames(n)
    r = Rig(monkeypatch, {"proposals": cards})
    judge_answers(r, answer)

    async def go():
        await offered(r)
        await r.ask("die letzte bitte" if n == 20 else "die zehnte bitte")

    run(go())
    assert decided(r) == [cards[int(answer.split()[1]) - 1]["action_id"]]


@pytest.mark.parametrize("answer", ["confirm 0", "confirm 21", "confirm 13"])
def test_zero_twenty_one_and_numbers_outside_the_set_authorise_nothing(monkeypatch, answer):
    cards = renames(22)  # 20 offered; 21 and 22 are on the screen only
    r = Rig(monkeypatch, {"proposals": cards[:12] if answer == "confirm 13" else cards})
    judge_answers(r, answer)

    async def go():
        await offered(r)
        await r.ask("die einundzwanzigste")

    run(go())
    assert decided(r) == []
    assert len(r.listed[0]) == (12 if answer == "confirm 13" else 20)  # only the set is shown


# --- coming back to a card: offer_cards ---------------------------------------------------------


def _records(cards: list[dict]) -> list[dict]:
    out: list[dict] = [{"type": "user", "turn_id": "t-old", "text": "rename them"}]
    for c in cards:
        rec = {"type": "proposal", "turn_id": "t-old", **c}
        out.append(rec)
    return out


def _tool_turn(via="call"):
    return types.SimpleNamespace(via=via, cid="conv", card_offers=[])


def _store(monkeypatch, cards: list[dict], states: dict | None, status: str = "ok"):
    monkeypatch.setattr(ask_store, "read", lambda cid: _records(cards))
    monkeypatch.setattr(
        ask_proposals,
        "views",
        lambda cid: (status, {k: dict(v) for k, v in (states or {}).items()}),
    )


OPEN = {c["action_id"]: {"state": "pending"} for c in renames(3)}


@pytest.mark.parametrize(
    ("states", "status", "match"),
    [
        ({**OPEN, _pid(2): {"state": "done"}}, "ok", "already done"),
        ({**OPEN, _pid(2): {"state": "dismissed"}}, "ok", "already dismissed"),
        ({**OPEN, _pid(2): {"state": "dismissed", "replaced_by": _pid(9)}}, "ok", "replaced by"),
        (OPEN, "unknown", "could not be read"),
    ],
    ids=["settled", "withdrawn", "replaced", "unreadable"],
)
def test_offer_cards_refuses_what_is_not_open(monkeypatch, states, status, match):
    _store(monkeypatch, renames(3), states, status)
    turn = _tool_turn()
    with pytest.raises(ask_orchestrator._ToolRefused, match=match):
        run(ask_orchestrator._offer_cards(turn, {"cards": [_pid(1), _pid(2)]}))
    assert turn.card_offers == []  # nothing offered, not even the open one


def test_offer_cards_refuses_another_conversations_and_unknown_ids(monkeypatch):
    # The sidecar knows the id (another conversation's card can't be in it, but even if a read
    # returned one), yet no proposal record of THIS conversation does.
    _store(monkeypatch, renames(2), {**OPEN, _pid(7): {"state": "pending"}})
    for bad in (_pid(7), "0" * 32, "not-an-id"):
        turn = _tool_turn()
        with pytest.raises(ask_orchestrator._ToolRefused, match="not a card of this conversation"):
            run(ask_orchestrator._offer_cards(turn, {"cards": [_pid(1), bad]}))
        assert turn.card_offers == []


def test_offer_cards_is_refused_off_a_call(monkeypatch):
    _store(monkeypatch, renames(1), OPEN)
    with pytest.raises(ask_orchestrator._ToolRefused, match="not on a call"):
        run(ask_orchestrator._offer_cards(_tool_turn(None), {"cards": [_pid(1)]}))


def test_offer_cards_deduplicates_and_keeps_the_order_named(monkeypatch):
    _store(monkeypatch, renames(3), OPEN)
    turn = _tool_turn()
    got, _ = run(ask_orchestrator._offer_cards(turn, {"cards": [_pid(3), _pid(1), _pid(3)]}))
    assert turn.card_offers == [_pid(3), _pid(1)] and got["offered"] == 2


def test_offer_cards_is_a_call_only_tool_that_waits_for_the_commit():
    names = lambda specs: {s["function"]["name"] for s in specs}  # noqa: E731
    assert "offer_cards" in names(ask_orchestrator.tool_specs("call"))
    assert "offer_cards" not in names(ask_orchestrator.tool_specs(None))
    assert ask_orchestrator.TOOLS["offer_cards"].kind == "call"
    assert "offer_cards" not in ask_orchestrator.DRAFT_READ_TOOLS


def _offer_view(ids: list[str]) -> dict:
    return {"answer": "Here they are again.", "card_offers": ids}


def test_an_offered_card_arms_a_fresh_challenge_naming_only_the_offered_ids(monkeypatch):
    cards = renames(3)
    states = {c["action_id"]: {"state": "pending"} for c in cards}
    _store(monkeypatch, cards, states)
    r = Rig(monkeypatch, _offer_view([_pid(2)]))

    async def go():
        await offered(r, "do that rename")
        armed = [c for c in challenges(r) if c["cards"]][-1]
        assert armed["cards"] == [_pid(2)] and armed["batch"] is False
        assert r.said_text()[-1].startswith("Rename, for # Handoff 2:")
        await r.ask("go ahead")

    run(go())
    assert decided(r) == [_pid(2)]  # referring to one card never authorises another
    assert r.said_text()[-1] == "Done."


def test_several_offered_cards_are_one_short_batch(monkeypatch):
    cards = renames(3)
    _store(monkeypatch, cards, {c["action_id"]: {"state": "pending"} for c in cards})
    r = Rig(monkeypatch, _offer_view([_pid(3), _pid(1)]))

    async def go():
        await offered(r, "do those renames")
        assert r.said_text()[-1] == (
            "Two renames, the details are on your screen. Shall I go ahead with both?"
        )
        await r.ask("yes")

    run(go())
    assert decided(r) == [_pid(3), _pid(1)]


def test_a_changed_card_is_offered_and_the_replaced_one_never(monkeypatch):
    """ "The third one, but call it X": HQ proposes the corrected card with `replaces` (the old id
    retires as replaced) — the new card is this turn's own and is offered; the old one is not."""
    old = renames(3)
    new = {**old[2], "action_id": _pid(50), "text": "Renames it to “Question dock fix”.",
           "replaces": _pid(3)}  # fmt: skip
    states = {
        _pid(1): {"state": "pending"},
        _pid(2): {"state": "pending"},
        _pid(3): {"state": "dismissed", "replaced_by": _pid(50)},
        _pid(50): {"state": "pending", "replaces": _pid(3)},
    }
    _store(monkeypatch, old, states)
    r = Rig(monkeypatch, {"proposals": [new], "card_offers": [_pid(3)]})

    async def go():
        await offered(r, "the third one, but call it Question dock fix")
        armed = [c for c in challenges(r) if c["cards"]][-1]
        assert armed["cards"] == [_pid(50)]
        assert r.said_text()[-1].startswith("Changed: Rename, for # Handoff 3:")
        await r.ask("go ahead")

    run(go())
    assert decided(r) == [_pid(50)]


def test_a_card_settled_between_offer_and_confirm_is_refused_at_settlement(monkeypatch):
    cards = renames(2)
    states = {c["action_id"]: {"state": "pending"} for c in cards}
    _store(monkeypatch, cards, states)
    r = Rig(monkeypatch, _offer_view([_pid(1)]))

    async def go():
        await offered(r, "do that rename")
        states[_pid(1)] = {"state": "done"}  # settled elsewhere before the yes
        await r.ask("go ahead")

    run(go())
    assert decided(r) == []
    assert r.said_text()[-1] == "That one changed in the meantime, so I left it alone."


def test_a_card_replaced_between_offer_and_confirm_is_refused_in_a_batch(monkeypatch):
    cards = renames(2)
    states = {c["action_id"]: {"state": "pending"} for c in cards}
    _store(monkeypatch, cards, states)
    r = Rig(monkeypatch, _offer_view([_pid(1), _pid(2)]))

    async def go():
        await offered(r, "do those renames")
        states[_pid(1)] = {"state": "dismissed", "replaced_by": _pid(60)}
        await r.ask("yes")

    run(go())
    assert decided(r) == [_pid(2)]
    assert outcomes(r) == ["One done and one didn't go through."]


def test_a_closed_card_named_in_card_offers_is_never_offered(monkeypatch):
    cards = renames(2)
    _store(monkeypatch, cards, {_pid(1): {"state": "done"}, _pid(2): {"state": "pending"}})
    r = Rig(monkeypatch, _offer_view([_pid(1), _pid(2)]))

    async def go():
        await offered(r, "do those renames")
        armed = [c for c in challenges(r) if c["cards"]][-1]
        assert armed["cards"] == [_pid(2)]

    run(go())


# --- short read-backs and the fast path ---------------------------------------------------------


def test_mixed_kinds_are_one_clause_each_and_every_kind_is_named():
    cards = [
        *[{"kind": "operation", "op": "session.rename", "line": "x"}] * 3,
        {"kind": "operation", "op": "project.delete", "line": "y"},
        {"kind": "session", "line": "z"},
        {"kind": "operation", "op": "session.review", "label": "Review", "line": "w"},
    ]
    assert voice_text.readback(cards) == (
        "Three renames, one project to delete, one new session and one review change, the "
        "details are on your screen. Shall I go ahead with all six?"
    )


@pytest.mark.parametrize(
    ("said", "verdict"),
    [
        ("yes", ("all", None)),
        ("Yes please.", ("all", None)),
        ("do them all", ("all", None)),
        ("yes, all of them", ("all", None)),
        ("go ahead", ("all", None)),
        ("confirm the second", ("confirm", 1)),
        ("dismiss it", ("ambiguous_dismiss", None)),
        ("dismiss all", ("dismiss_all", None)),
        ("no", ("cancel", None)),
        ("yes?", ("other", None)),
        ("yes but not the third", ("other", None)),
    ],
)
def test_a_plain_yes_answers_a_batchs_question(said, verdict):
    assert voice_text.classify_confirmation(said, 12, dismiss=True, batch=True) == verdict


def test_outside_a_batch_a_bare_yes_still_goes_to_the_judge():
    assert voice_text.classify_confirmation("yes", 2) == ("other", None)
    assert voice_text.classify_confirmation("do them all", 2) == ("all", None)  # refused there


def test_the_judge_contract_and_the_call_agree_on_the_batch_cap():
    from agent_sessions import prompts

    assert voice_judge.CARDS_MAX == voice_calls.BATCH_MAX == 20
    assert "from 1 to 20" in prompts.VOICE_CONFIRM_JUDGE_FORMAT
