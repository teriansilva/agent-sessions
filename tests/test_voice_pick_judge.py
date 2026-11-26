"""#2342 H1 — a folder pick on an HQ card is answered by name, description or place, in any
language, and judged by the AI (the operator's comment 141852 on #2342):

* the options are offered as "X or Y?", one frozen snapshot per card: the spoken line, the English
  fast path (its numbers), the judge and the settlement all read that list, and a pick against a
  list that changed since (reordered, a folder gone or added) is refused;
* the judge (`voice_judge.judge_pick`, the guarded `voice_pick_judge` prompt) answers from a
  closed set — `pick N`, `none`, `cancel` — parsed strictly; anything else is no verdict;
* a judged pick only FILLS the card: the card's own yes is asked next ("… In ~/code/docs. Shall I
  go ahead?"), and only that yes runs it;
* ambiguity asks back, never picks: `none`, or a pick of an option whose name another option
  shares, is "Which one do you mean?"; no verdict is "Sorry, which one?" once, then the card stays
  with nothing picked; `cancel` leaves it; "skip" is the English grammar's, before the judge.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_sessions import ask_operations, prompts, voice_calls, voice_judge, voice_text
from test_voice_confirm import Rig, run
from test_voice_folder_pick import FOLDERS, PICK_ASK, PICK_LINE, _view

CORPUS = Path(__file__).parent / "fixtures" / "voice_pick_corpus.json"
DECIDE = "/api/ask/proposals/op-1/decide"


def _rig(monkeypatch, folders=FOLDERS) -> Rig:
    now = {"folders": list(folders)}
    monkeypatch.setattr(
        ask_operations, "folder_choices", lambda pid: now["folders"] if pid == "op-1" else []
    )
    r = Rig(monkeypatch, _view())
    r.now = now  # a test changes the card's choices through this
    return r


def _decides(r: Rig) -> list:
    return [b for m, p, b in r.routes if m == "POST" and p == DECIDE]


async def _offered(r: Rig, ask: str = "Make web-docs the default folder for web") -> None:
    await r.ask(ask)
    await r.readback_played()


# --- the verdict's contract ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "content, expect",
    [
        ("pick 1", ("pick", 0)),
        ("pick 2", ("pick", 1)),
        ("  pick 2\n", ("pick", 1)),
        ("none", ("none", None)),
        ("cancel", ("cancel", None)),
        ("pick 3", None),  # past the options read
        ("pick 0", None),
        ("pick", None),  # a pick names its option
        ("none 1", None),
        ("cancel 2", None),
        ("Pick 1", None),
        ("pick 1.", None),
        ("pick one", None),
        ("I'd say pick 1", None),
        ("other", None),
        ("", None),
        (None, None),
        (["pick 1"], None),
    ],
)
def test_the_verdict_is_parsed_strictly(content, expect):
    assert voice_judge.parse_pick_verdict(content, 2) == expect


class _Resp:
    def __init__(self, content, status=200, finish="stop", refusal=None):
        self.status_code = status
        self._body = {
            "choices": [
                {"finish_reason": finish,
                 "message": {"content": content, "refusal": refusal}}
            ]
        }  # fmt: skip

    def json(self):
        return self._body


def _transport(monkeypatch, reply):
    from agent_sessions import review

    sent = []

    async def post(cfg, body):
        sent.append(body)
        return await reply() if callable(reply) else reply

    monkeypatch.setattr(review, "_require_config", lambda: {"model": "m"})
    monkeypatch.setattr(review, "post_chat_response", post)
    return sent


def test_the_judge_is_the_guarded_registered_prompt_over_the_options_as_data(monkeypatch):
    sent = _transport(monkeypatch, _Resp("pick 2"))
    got = asyncio.run(voice_judge.judge_pick("Which folder: a or b?", FOLDERS, "the docs one"))
    assert got == ("pick", 1)
    (body,) = sent
    system, user = body["messages"]
    assert system["content"] == prompts.effective("voice_pick_judge")
    assert prompts.VOICE_PICK_JUDGE_FORMAT in system["content"]
    p = prompts.get("voice_pick_judge")
    assert p.guarded and p.locked == prompts.VOICE_PICK_JUDGE_FORMAT
    assert body["temperature"] == 0
    # the options, the question and the reply go in as quoted data, never as instructions
    assert (
        json.dumps([{"n": 1, "option": FOLDERS[0]}, {"n": 2, "option": FOLDERS[1]}])
        in (user["content"])
    )
    assert '"the docs one"' in user["content"]


@pytest.mark.parametrize(
    "reply",
    [
        _Resp("pick 1", status=500),
        _Resp("pick 1", finish="length"),
        _Resp("pick 1", finish=None),
        _Resp("pick 1", refusal="no"),
        _Resp("the first one"),
    ],
)
def test_anything_but_a_clean_answer_is_no_verdict(monkeypatch, reply):
    _transport(monkeypatch, reply)
    assert asyncio.run(voice_judge.judge_pick("q", FOLDERS, "docs")) is None


def test_a_judge_past_its_time_is_no_verdict(monkeypatch):
    async def slow():
        await asyncio.sleep(1)
        return _Resp("pick 1")

    _transport(monkeypatch, slow)
    monkeypatch.setattr(voice_judge, "JUDGE_TIMEOUT_S", 0.01)
    assert asyncio.run(voice_judge.judge_pick("q", FOLDERS, "docs")) is None


def test_more_options_than_a_pick_reads_are_never_judged(monkeypatch):
    sent = _transport(monkeypatch, _Resp("pick 1"))
    six = [f"/p{i}" for i in range(6)]
    assert asyncio.run(voice_judge.judge_pick("q", six, "p1")) is None
    assert sent == []
    assert voice_judge.PICK_OPTIONS_MAX == voice_calls.PICK_OPTIONS_TOLD


# --- on a call -----------------------------------------------------------------------------------


def test_a_folder_picked_by_name_fills_the_card_and_its_own_yes_runs_it(monkeypatch):
    r = _rig(monkeypatch)
    r.pick_verdict = ("pick", 1)
    r.verdict = ("confirm", 0)  # the card judge hears the yes to the filled card

    async def go():
        await _offered(r)
        await r.ask("the docs one")
        # filled, not run: the card's own question is asked next
        assert _decides(r) == []
        assert r.said_text()[-1] == f"{PICK_LINE} In /home/op/code/web-docs. Shall I go ahead?"
        assert [c["kind"] for c in r.call.challenge["cards"]] == ["picked"]
        await r.readback_played()
        await r.ask("Yes.")

    run(go())
    (readback, options, reply) = r.picked[0]
    assert readback == f"{PICK_LINE} {PICK_ASK}" and options == FOLDERS and reply == "the docs one"
    assert _decides(r) == [{"decision": "confirm", "inputs": {"folder": "/home/op/code/web-docs"}}]


def test_a_german_call_picks_by_name_and_confirms_in_german(monkeypatch):
    r = _rig(monkeypatch)
    r.call.language = "de"
    r.pick_verdict = ("pick", 0)
    r.verdict = ("confirm", 0)

    async def go():
        await _offered(r, "Setz den Standardordner")
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Den web-Ordner, nicht die Docs", heard_language="de")
        await r.side()
        assert r.said_text()[-1] == (
            "Default launch folder, für web: New sessions start in the folder you pick. "
            "In /home/op/code/web. Soll ich loslegen?"
        )
        await r.readback_played()
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="Ja, mach", heard_language="de")
        await r.side()

    run(go())
    assert _decides(r) == [{"decision": "confirm", "inputs": {"folder": "/home/op/code/web"}}]


def test_none_asks_which_and_picks_nothing(monkeypatch):
    r = _rig(monkeypatch)
    r.pick_verdict = ("none", None)

    async def go():
        await _offered(r)
        await r.ask("the other thing")
        assert r.said_text()[-1] == "Which one do you mean?"
        await r.readback_played()  # re-armed only once that question played
        assert r.call.challenge["armed_at"] is not None
        r.pick_verdict = ("pick", 0)
        await r.ask("the plain web one")
        assert r.said_text()[-1].endswith("In /home/op/code/web. Shall I go ahead?")

    run(go())
    assert _decides(r) == []


def test_no_verdict_asks_once_then_the_card_waits_with_nothing_picked(monkeypatch):
    r = _rig(monkeypatch)  # the Rig's pick judge gives no verdict

    async def go():
        await _offered(r)
        await r.ask("äh, den da")
        assert r.said_text()[-1] == "Sorry, which one?"
        await r.readback_played()
        await r.ask("hmm, den da drüben")

    run(go())
    assert r.said_text()[-1] == "Sorry, I didn't catch that. It's still waiting for you."
    assert r.call.challenge is None and _decides(r) == []


def test_cancel_leaves_the_card(monkeypatch):
    r = _rig(monkeypatch)
    r.pick_verdict = ("cancel", None)

    async def go():
        await _offered(r)
        await r.ask("lass mal, doch nicht")

    run(go())
    assert r.said_text()[-1] == "Okay, I'll leave it."
    assert r.call.challenge is None and _decides(r) == []


def test_skip_is_the_grammars_and_never_reaches_the_judge(monkeypatch):
    r = _rig(monkeypatch)
    r.pick_verdict = ("pick", 0)

    async def go():
        await _offered(r)
        await r.ask("Skip it.")

    run(go())
    assert r.picked == [] and _decides(r) == []
    assert r.said_text()[-1] == "Okay. That was the last one."


def test_a_number_is_still_the_instant_path(monkeypatch):
    r = _rig(monkeypatch)
    r.pick_verdict = ("pick", 0)  # would be wrong: the fast path decides first

    async def go():
        await _offered(r)
        await r.ask("The second one.")

    run(go())
    assert r.picked == []
    assert _decides(r) == [{"decision": "confirm", "inputs": {"folder": "/home/op/code/web-docs"}}]


def test_a_pick_of_an_option_whose_name_another_shares_asks_which(monkeypatch):
    r = _rig(monkeypatch, folders=["/home/op/a/docs", "/home/op/b/docs"])
    r.pick_verdict = ("pick", 0)  # the judge took the first match: refused

    async def go():
        await _offered(r)
        await r.ask("the docs one")

    run(go())
    assert r.said_text()[-1] == "Which one do you mean?"
    assert [c["kind"] for c in r.call.challenge["cards"]] == ["pick"] and _decides(r) == []


@pytest.mark.parametrize(
    "now",
    [
        list(reversed(FOLDERS)),  # reordered: "the second one" would be another folder
        FOLDERS[:1],  # one gone
        [*FOLDERS, "/home/op/code/web-api"],  # one added
        [],
    ],
)
def test_a_pick_against_a_list_that_changed_is_refused(monkeypatch, now):
    r = _rig(monkeypatch)

    async def go():
        await _offered(r)
        r.now["folders"] = now
        await r.ask("The second one.")

    run(go())
    assert _decides(r) == []
    assert r.said_text()[-1] == "That one changed in the meantime, so I left it alone."


def test_a_judged_pick_whose_list_changed_before_its_yes_runs_nothing(monkeypatch):
    r = _rig(monkeypatch)
    r.pick_verdict = ("pick", 1)
    r.verdict = ("confirm", 0)  # the card judge hears the yes to the filled card

    async def go():
        await _offered(r)
        await r.ask("the docs one")
        await r.readback_played()
        r.now["folders"] = list(reversed(FOLDERS))
        await r.ask("Yes.")

    run(go())
    assert _decides(r) == []


def test_a_verdict_after_a_newer_turn_began_picks_nothing(monkeypatch):
    r = _rig(monkeypatch)

    async def late():
        await r.ev(t="user_started")  # the Commander spoke again while the judge thought
        return ("pick", 1)

    r.pick_verdict = late

    async def go():
        await _offered(r)
        await r.ask("the docs one")

    run(go())
    assert _decides(r) == []
    assert not any("Shall I go ahead" in t for t in r.said_text())


# --- the corpus ----------------------------------------------------------------------------------


def _corpus() -> list[dict]:
    return json.loads(CORPUS.read_text())["cases"]


def test_the_corpus_covers_both_languages_and_every_hazard():
    cases = _corpus()
    assert {c["lang"] for c in cases} == {"en", "de"}
    kinds = {c["kind"] for c in cases}
    assert {"name", "description", "ordinal", "negation", "duplicate", "cancel", "unclear"} <= kinds
    for c in cases:
        assert 2 <= len(c["options"]) <= voice_judge.PICK_OPTIONS_MAX
        assert c["expect"] in ("none", "cancel") or 0 <= c["expect"] < len(c["options"])


@pytest.mark.parametrize("case", _corpus(), ids=lambda c: f'{c["lang"]}-{c["reply"]}')
def test_the_fast_path_never_picks_what_the_corpus_forbids(case):
    """The English grammar's instant path either agrees with the corpus or leaves it to the judge
    (`other`): it never picks an option the corpus says must not be picked."""
    options = [{"option": i + 1, "label": o, "as": "pick"} for i, o in enumerate(case["options"])]
    verdict, choice = voice_text.classify_decision(
        case["reply"], {"kind": "pick", "options": options}
    )
    if verdict == "choose":
        assert options.index(choice) == case["expect"]
    elif verdict == "cancel":
        assert case["expect"] == "cancel"
    else:
        assert verdict in ("other", "skip", "all")


@pytest.mark.parametrize(
    "case", [c for c in _corpus() if c["kind"] == "duplicate"], ids=lambda c: c["reply"]
)
def test_the_duplicate_guard_refuses_any_pick_the_corpus_calls_ambiguous(case):
    options = [{"label": o} for o in case["options"]]
    for i in range(len(options)):
        if voice_calls._option_key(options[i]["label"]) in case["reply"].casefold():
            assert voice_calls._ambiguous_option(options, i)


def test_an_older_offer_never_displaces_a_pick_being_judged(monkeypatch):
    """While a pick's reply is judged, an offer started before it (same generation) is dropped."""
    r = _rig(monkeypatch)
    gate = asyncio.Event()

    async def held():
        await gate.wait()
        return ("none", None)

    r.pick_verdict = held
    nudge = {"id": "act-9", "kind": "nudge", "verb": "send it", "line": "A message."}

    async def go():
        await _offered(r)
        await r.ev(t="user_started")
        await r.ev(t="utterance", text="the docs one")
        await r.call.offer([nudge], gen=r.call.generation)  # an older offer lands meanwhile
        assert [c["kind"] for c in r.call.challenge["cards"]] == ["pick"]
        gate.set()
        await r.side()

    run(go())
    assert r.said_text()[-1] == "Which one do you mean?"
    assert [c["kind"] for c in r.call.challenge["cards"]] == ["pick"]
