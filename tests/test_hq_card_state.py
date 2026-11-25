"""#2372: on a call HQ knows a card's live state.

The 2026-11-25 call: the Commander said "Do it!", the card was accepted, the API session took 39 s
to start, and HQ twice said the card was "still sitting there waiting". Three causes, pinned here:

* HQ's turn carried no card facts, so it trusted its own earlier words (`_card_facts`, with an
  observation watermark `cards_seen` on each settled answer);
* the spoken decision never reached HQ's history as the Commander's words (`_exchanges` replays
  it, keyed by card, with every outcome said about that card);
* the call stopped following after 30 s, so the end was never said (`_follow_late`, one end per
  card, nothing after hang-up).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from agent_sessions import ask_orchestrator as orch
from agent_sessions import ask_proposals, ask_store, voice_calls
from test_voice_confirm import Rig

X = "a" * 32
Y = "b" * 32
T1, T2, T3 = (f"0000000{i}-0000-4000-8000-000000000000" for i in (1, 2, 3))


def _proposal(aid: str, title: str = "Battlelab", turn: str = T1) -> dict:
    return {"type": "proposal", "turn_id": turn, "action_id": aid, "kind": "session",
            "title": title, "agent": "Claude — API", "text": "Investigate search."}  # fmt: skip


def _views(states: dict[str, str], status: str = "ok"):
    return lambda cid: (status, {k: {"state": v, "session_key": ""} for k, v in states.items()})


def _facts(monkeypatch, records, seen, states, status="ok"):
    monkeypatch.setattr(ask_proposals, "views", _views(states, status))
    note, noted = orch._card_facts("conv", records, seen)
    body = json.loads(note.split("\n", 3)[-1]) if note and status == "ok" else None
    return note, noted, body


# --- card facts on the request ------------------------------------------------------------------


def test_a_card_confirmed_while_hq_was_idle_reaches_the_next_turn_as_it_is(monkeypatch):
    note, noted, body = _facts(monkeypatch, [_proposal(X)], {X: "pending"}, {X: "deciding"})
    assert orch.CARD_FACTS_NOTE in note
    assert body["cards"] == [{"card": X, "what": "a new session in Battlelab", "state": "deciding"}]
    assert noted == {X: "deciding"}
    _, _, body = _facts(monkeypatch, [_proposal(X)], {X: "deciding"}, {X: "started"})
    assert body["cards"][0]["state"] == "started"  # never "pending" once decided


def test_a_settled_card_hq_was_already_told_about_is_not_repeated(monkeypatch):
    note, noted, _ = _facts(monkeypatch, [_proposal(X)], {X: "started"}, {X: "started"})
    assert note == "" and noted == {}


def test_a_settlement_between_snapshot_and_answer_still_reaches_the_next_turn(monkeypatch):
    """Snapshot (pending) → the card settles → the answer is written → the next turn: the
    watermark is what the answer was TOLD (pending), so the next turn still gets `started`."""
    turn = orch._Turn(
        cid="conv", turn_id=T1, question="q", working_keys=set(), progress=lambda ev: None
    )
    monkeypatch.setattr(ask_proposals, "views", _views({X: "pending"}))
    turn.card_note, turn.cards_noted = orch._card_facts("conv", [_proposal(X)], {})
    status = orch._status(turn, "done")  # the card started meanwhile; the answer never saw it
    assert status["cards_seen"] == {X: "pending"}
    admitted = ask_store._admit(status, [])  # the store keeps it
    assert admitted["cards_seen"] == {X: "pending"}
    records = [_proposal(X), {"type": "user", "turn_id": T1, "text": "q"}, admitted]
    seen = orch._cards_seen_before(records, T2)
    _, _, body = _facts(monkeypatch, records, seen, {X: "started"})
    assert body["cards"][0]["state"] == "started"


def test_past_the_cap_an_older_unsettled_card_is_still_listed_and_the_rest_stay_unobserved(
    monkeypatch,
):
    ids = [f"{i:032x}" for i in range(1, 13)]
    records = [_proposal(i) for i in ids]
    states = {i: "started" for i in ids}
    states[ids[0]] = "deciding"  # the OLDEST card is the one still launching
    _, noted, body = _facts(monkeypatch, records, {}, states)
    listed = [c["card"] for c in body["cards"]]
    assert listed[0] == ids[0] and len(listed) == orch.CARD_FACTS_MAX
    assert body["more"].startswith(f"{12 - orch.CARD_FACTS_MAX} more")
    assert set(noted) == set(listed)  # overflowed cards are not marked as told


def test_unreadable_states_are_said_to_be_unreadable_never_guessed(monkeypatch):
    note, noted, _ = _facts(monkeypatch, [_proposal(X)], {}, {}, status="unknown")
    assert orch.CARD_FACTS_UNREADABLE in note and noted == {}


def test_the_store_refuses_a_malformed_watermark():
    base = {"type": "status", "turn_id": T1, "status": "done"}
    with pytest.raises(ValueError):
        ask_store._admit({**base, "cards_seen": {"not-a-card": "started"}}, [])
    with pytest.raises(ValueError):
        ask_store._admit({**base, "status": "failed", "cards_seen": {X: "started"}}, [])


# --- the spoken decision in HQ's history ---------------------------------------------------------


def _call_records() -> list[dict]:
    """Two cards decided by voice, interleaved with another exchange, the first one's outcome
    recorded BEFORE its decision's link and its late end AFTER another answer."""
    return [
        {"type": "user", "turn_id": T1, "text": "Start two sessions", "via": "call"},
        _proposal(X), _proposal(Y, "Docs site"),
        {"type": "assistant", "turn_id": T1, "text": "Two cards are up."},
        {"type": "status", "turn_id": T1, "status": "done"},
        {"type": "heard", "heard_id": "111111111111", "route": "received", "chars": 9,
         "text": "Do it!"},
        {"type": "said", "kind": "outcome", "text": "Accepted. It's starting now.", "card": X},
        {"type": "heard_link", "heard_id": "111111111111", "route": "confirm", "card": X},
        {"type": "user", "turn_id": T2, "text": "I love you", "via": "call"},
        {"type": "assistant", "turn_id": T2, "text": "Kind of you."},
        {"type": "status", "turn_id": T2, "status": "done"},
        {"type": "said", "kind": "outcome", "text": "The session is up.", "card": X},
        {"type": "heard", "heard_id": "222222222222", "route": "received", "chars": 7,
         "text": "drop the other"},
        {"type": "heard_link", "heard_id": "222222222222", "route": "confirm", "card": Y},
        {"type": "said", "kind": "outcome", "text": "Dismissed.", "card": Y},
        {"type": "user", "turn_id": T3, "text": "What card did you just start?", "via": "call"},
    ]  # fmt: skip


def test_a_voice_decision_is_its_own_past_exchange_with_every_outcome_of_its_card():
    _summary, pairs, _ = orch._exchanges(_call_records(), T3)
    ids = [t for t, _q, _a in pairs]
    assert ids == [T1, f"{orch.VOICE_DECISION_ID}{X}", T2, f"{orch.VOICE_DECISION_ID}{Y}"]
    q, a = pairs[1][1], pairs[1][2]
    assert '"Do it!"' in q and "a new session in Battlelab" in q
    assert "Already done: not a new request" in q
    assert a == ("[Said on the call: Accepted. It's starting now.]\n"
                 "[Said on the call: The session is up.]")  # fmt: skip
    # The other answer between them keeps its own words only.
    assert pairs[2][2] == "Kind of you."
    assert '"drop the other"' in pairs[3][1] and "Docs site" in pairs[3][1]
    assert pairs[3][2] == "[Said on the call: Dismissed.]"


def test_an_outcome_with_no_voice_decision_stays_with_the_answer_it_followed():
    records = [
        {"type": "user", "turn_id": T1, "text": "Start it", "via": "call"},
        _proposal(X),
        {"type": "assistant", "turn_id": T1, "text": "Card's up."},
        {"type": "status", "turn_id": T1, "status": "done"},
        {"type": "said", "kind": "outcome", "text": "The session is up.", "card": X},
        {"type": "user", "turn_id": T2, "text": "and now?", "via": "call"},
    ]
    _summary, pairs, _ = orch._exchanges(records, T2)
    assert [t for t, _q, _a in pairs] == [T1]
    assert pairs[0][2] == "Card's up.\n\n[Said on the call: The session is up.]"


def test_compaction_never_ends_a_summary_on_a_voice_decision():
    records = _call_records()[:-1]
    for r in records:
        if r.get("type") == "assistant":
            r["text"] = r["text"] + " " + "x" * 4000
    plan = orch._compaction_plan(records, 3000)
    assert plan is not None
    _n, upto, _prev, folded = plan
    assert not upto.startswith(orch.VOICE_DECISION_ID)
    assert not folded[-1][0].startswith(orch.VOICE_DECISION_ID)


def test_the_store_takes_a_card_on_a_decision_and_an_outcome_and_nothing_else():
    link = {"type": "heard_link", "heard_id": "111111111111", "route": "confirm", "card": X}
    said = {"type": "said", "kind": "outcome", "text": "Accepted.", "card": X}
    assert ask_store._admit(link, [])["card"] == X
    assert ask_store._admit(said, [])["card"] == X
    with pytest.raises(ValueError):
        ask_store._admit({**said, "card": "../x"}, [])


# --- the call tells the end once, after any launch length ----------------------------------------

ONE = {"proposals": [{"kind": "session", "action_id": X, "title": "Battlelab",
                      "text": "Investigate search."}]}  # fmt: skip


def _rig(monkeypatch, states):
    r = Rig(monkeypatch, ONE, route_status=202)
    recs: list[dict] = []
    r.call._record = recs.append
    monkeypatch.setattr(voice_calls, "OUTCOME_EVERY_S", 0.001)
    monkeypatch.setattr(voice_calls, "OUTCOME_POLL_S", 0.01)
    monkeypatch.setattr(voice_calls, "OUTCOME_LATE_EVERY_S", 0.001)
    it = iter(states)

    def views(cid):
        nxt = next(it, states[-1])
        return ("unknown", {}) if nxt is None else ("ok", {X: {"state": nxt}})

    monkeypatch.setattr(ask_proposals, "views", views)
    return r, recs


async def _confirm(r):
    await r.ask("start it")
    await r.readback_played()
    await r.ask("confirm")


async def _drain(r):
    while r.call._followers:
        await asyncio.gather(*r.call._followers.values(), return_exceptions=True)
        await asyncio.sleep(0)


async def _play_all(r):
    """The browser played every line out (its `played` acks)."""
    for m in r.says():
        await r.call.on_played(m["id"])


def test_a_slow_launch_is_told_when_it_lands_once_with_the_card_on_every_record(monkeypatch):
    r, recs = _rig(monkeypatch, ["deciding"] * 60 + ["started"])

    async def go():
        await _confirm(r)
        await _drain(r)
        await r.call._tell_end(ONE["proposals"][0] | {"id": X}, "started")  # a repeat says nothing
        await _play_all(r)

    asyncio.run(go())
    assert r.said_text()[-3:] == [
        "Accepted. It's starting now.",
        voice_calls.STILL_STARTING_LINE,
        "The session is up.",
    ]
    outcomes = [x for x in recs if x.get("type") == "said" and x.get("kind") == "outcome"]
    assert [x.get("card") for x in outcomes[-3:]] == [X, X, X]
    links = [x for x in recs if x.get("type") == "heard_link" and x.get("route") == "confirm"]
    # The words when heard; the card-tagged link once its route really ran.
    assert [x.get("card") for x in links] == [None, X]


def test_an_unreadable_state_is_read_again_and_never_spends_the_one_end(monkeypatch):
    r, _recs = _rig(monkeypatch, ["deciding"] * 60 + [None, None, "failed"])

    async def go():
        await _confirm(r)
        await _drain(r)

    asyncio.run(go())
    assert r.said_text()[-1] == voice_calls.final_sentence({"kind": "session"}, "failed")
    assert r.said_text().count(voice_calls.final_sentence({"kind": "session"}, "failed")) == 1


def test_nothing_is_said_after_hang_up(monkeypatch):
    r, recs = _rig(monkeypatch, ["deciding"])

    async def go():
        await _confirm(r)
        assert r.call._followers  # still launching: a follower holds it
        await r.call.end("hangup")
        await _drain(r)

    asyncio.run(go())
    assert r.said_text()[-1] == voice_calls.STILL_STARTING_LINE
    assert not r.call._followers
    assert not any("The session is up." in (x.get("text") or "") for x in recs)


def test_the_card_note_rides_on_hqs_own_request(monkeypatch):
    turn = orch._Turn(
        cid="conv", turn_id=T2, question="what card?", working_keys=set(), progress=lambda ev: None
    )
    monkeypatch.setattr(ask_proposals, "views", _views({X: "started"}))
    turn.card_note, turn.cards_noted = orch._card_facts("conv", [_proposal(X)], {X: "deciding"})
    message, _shown = orch._user_message(turn, {"model": "m", "base_url": "u", "api_key": "k"})
    assert orch.CARD_FACTS_NOTE in message["content"] and '"state": "started"' in message["content"]


# --- Hermes 8935 ---------------------------------------------------------------------------


def test_an_end_said_after_a_summary_covered_its_decision_still_reaches_hq():
    """1: compaction covered the voice decision while the launch was pending; the end, said after
    the summarised prefix, must not vanish behind `summary.upto`."""
    records = _call_records()[:11]  # through T2's settle, the decision and its "Accepted" line
    n = len(records)
    records.append({"type": "summary", "upto": T2, "n": n, "text": "Two cards; one confirmed."})
    records.append({"type": "said", "kind": "outcome", "text": "The session is up.", "card": X})
    records.append({"type": "user", "turn_id": T3, "text": "Is it up?", "via": "call"})
    summary, pairs, _ = orch._exchanges(records, T3)
    assert summary is not None and summary["upto"] == T2
    late = [p for p in pairs if p[0] == f"{orch.VOICE_DECISION_ID}{X}:late"]
    assert late and late[0][2] == "[Said on the call: The session is up.]"
    assert "a new session in Battlelab" in late[0][1]


def test_words_are_tied_to_the_card_only_when_its_route_really_runs(monkeypatch):
    """2: a fence that stops the settlement (a mute, newer words) leaves the words heard, never
    a history claiming the card's route ran."""
    r, recs = _rig(monkeypatch, ["started"])
    monkeypatch.setattr(r.call, "_settle_fence_holds", lambda snap: False)

    async def go():
        await _confirm(r)

    asyncio.run(go())
    assert not any(p.endswith("/decide") for _m, p, _b in r.routes)  # no route ran
    links = [x for x in recs if x.get("type") == "heard_link" and x.get("route") == "confirm"]
    assert [x.get("card") for x in links] == [None]  # heard, never tied to the card


def test_an_outcome_line_is_recorded_as_said_only_once_played(monkeypatch):
    """3: an undelivered or cut-off line is never claimed as said; the end is still announced
    at most once."""
    r, recs = _rig(monkeypatch, ["started"])

    async def go():
        await _confirm(r)
        outcomes = [x for x in recs if x.get("type") == "said" and x.get("card") == X]
        assert outcomes == []  # sent, not yet played
        await _play_all(r)

    asyncio.run(go())
    texts = [x["text"] for x in recs if x.get("type") == "said" and x.get("card") == X]
    assert texts == ["Accepted. It's starting now.", "The session is up."]


def test_a_hang_up_during_the_outcome_caption_records_and_sends_nothing(monkeypatch):
    """4: the call ended while the outcome's caption was being sent: no `said`, and no `say` to
    the worker after its stop."""
    r, recs = _rig(monkeypatch, ["started"])
    to_client = r.call._to_client

    async def hang_up_on_outcome(m):
        await to_client(m)
        if isinstance(m, dict) and m.get("t") == "caption" and "starting" in (m.get("text") or ""):
            await r.call.end("hangup")

    r.call._to_client = hang_up_on_outcome

    async def go():
        await _confirm(r)
        await _play_all(r)

    asyncio.run(go())
    assert not [x for x in recs if x.get("type") == "said" and x.get("card") == X]
    sent = [m.get("t") for m in r.to_worker if isinstance(m, dict)]
    assert "stop" in sent
    after_stop = sent[sent.index("stop") :]
    assert "say" not in after_stop
