"""#1715 P2 / #2342 H2 — Needs-you decisions on a call are answered in natural words, in any
language, and judged by the AI (the operator's comment 141855 on #1715):

* the questions are plain: "Should I allow it, just this once?", "Which one: main or release
  2.0?", "Want me to send that answer?";
* the English grammar stays the instant path; any other reply goes to ONE judgement
  (`voice_judge.judge_decision`, the guarded `voice_decision_judge` prompt) whose closed verdicts,
  per kind, are parsed strictly and mapped onto the grammar's own outcomes — the item's RECORDED
  single-use allow, its recorded refusal, a recorded menu option, the question's answer;
* a broader grant ("always", "immer", "nicht mehr fragen", …) is refused FIRST, before both the
  grammar and the judge, in any language: allow-always stays on the screen;
* the call arms these items in any language; every verdict acts only while the reply's fence
  holds, and `/choose` re-checks the recorded dialog either way.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from agent_sessions import prompts, voice_calls, voice_judge, voice_text
from test_voice_confirm import run
from test_voice_needs_you import BASH_SAID, _perm
from test_voice_needs_you_decide import (
    M_SID,
    P_SID,
    Q_SID,
    _menu_body,
    _permission_body,
    _posts,
    _rig,
    _three,
)

CORPUS = Path(__file__).parent / "fixtures" / "voice_decision_corpus.json"
ALLOW = (f"/api/pulse/actions/act-{P_SID}/choose", {"option": 1, "label": "Yes"})
REFUSE = (f"/api/pulse/actions/act-{P_SID}/choose", {"option": 3, "label": "No"})


async def _offered(r) -> None:
    await r.ask("What needs me?")
    await r.readback_played()


async def _reply(r, text: str, lang: str | None = None) -> None:
    await r.ev(t="user_started")
    await r.ev(t="utterance", text=text, **({"heard_language": lang} if lang else {}))
    await r.side()


# --- the verdict's contract ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "content, kind, expect",
    [
        ("allow_once", "permission", ("allow_once", None)),
        ("refuse", "permission", ("refuse", None)),
        ("broader", "permission", ("broader", None)),
        ("skip", "permission", ("skip", None)),
        ("cancel", "permission", ("cancel", None)),
        ("other", "permission", ("other", None)),
        ("pick 1", "permission", None),  # not this kind's
        ("send", "permission", None),
        ("pick 2", "menu", ("pick", 1)),
        ("pick 3", "menu", None),  # past the options read
        ("pick", "menu", None),
        ("unclear", "menu", ("unclear", None)),
        ("allow_once", "menu", None),
        ("send", "needs_you", ("send", None)),
        ("allow_once", "needs_you", None),
        ("Allow_once", "permission", None),
        ("allow_once.", "permission", None),
        ("allow once", "permission", None),
        ("skip 1", "permission", None),
        ("  refuse\n", "permission", ("refuse", None)),
        (None, "permission", None),
        ("allow_once", "nudge", None),  # never a kind this judge decides
    ],
)
def test_the_verdict_is_parsed_strictly_per_kind(content, kind, expect):
    assert voice_judge.parse_decision_verdict(content, kind, 2) == expect


class _Resp:
    def __init__(self, content, status=200, finish="stop"):
        self.status_code = status
        self._body = {"choices": [{"finish_reason": finish, "message": {"content": content}}]}

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


MENU = {
    "id": "m",
    "kind": "menu",
    "options": [
        {"option": 1, "label": "main", "as": "pick"},
        {"option": 2, "label": "release 2.0", "as": "pick"},
    ],
}


def test_the_judge_is_the_guarded_registered_prompt_over_the_item_as_data(monkeypatch):
    sent = _transport(monkeypatch, _Resp("pick 2"))
    got = asyncio.run(voice_judge.judge_decision(MENU, "Which one: main or release 2.0?", "2.0"))
    assert got == ("pick", 1)
    system, user = sent[0]["messages"]
    assert system["content"] == prompts.effective("voice_decision_judge")
    p = prompts.get("voice_decision_judge")
    assert p.guarded and p.locked == prompts.VOICE_DECISION_JUDGE_FORMAT
    assert sent[0]["temperature"] == 0
    item = {
        "kind": "menu",
        "options": [{"n": 1, "option": "main"}, {"n": 2, "option": "release 2.0"}],
    }
    assert json.dumps(item) in user["content"] and '"2.0"' in user["content"]
    # a permission tells the judge only whether allowing it once is on offer
    perm = {"kind": "permission", "allow": None, "deny": {"option": 3}, "line": "secret words"}
    assert voice_judge.decision_item(perm) == {"kind": "permission", "can_allow_once": False}


@pytest.mark.parametrize(
    "reply", [_Resp("refuse", status=500), _Resp("refuse", finish="length"), _Resp("no")]
)
def test_anything_but_a_clean_answer_is_no_verdict(monkeypatch, reply):
    _transport(monkeypatch, reply)
    assert asyncio.run(voice_judge.judge_decision(MENU, "q", "x")) is None


def test_a_judge_past_its_time_is_no_verdict(monkeypatch):
    async def slow():
        await asyncio.sleep(1)
        return _Resp("refuse")

    _transport(monkeypatch, slow)
    monkeypatch.setattr(voice_judge, "JUDGE_TIMEOUT_S", 0.01)
    assert asyncio.run(voice_judge.judge_decision(MENU, "q", "x")) is None
    assert voice_judge.MENU_OPTIONS_MAX == voice_calls.MENU_OPTIONS_TOLD


# --- the broader-grant veto ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "said",
    [
        "Allow always.", "yes, for this session", "yes and don't ask again", "allow everything",
        "Ja, immer.", "für immer erlauben", "fur immer", "ja und nicht mehr fragen",
        "Ja, jedes Mal", "erlaub alles", "dauerhaft erlauben",
    ],
)  # fmt: skip
def test_a_broader_grant_is_caught_in_either_language(said):
    assert voice_text.broader_grant(said)


@pytest.mark.parametrize("said", ["Yes.", "ja, einmal", "go ahead", "Ja, erlaub das.", "allow it"])
def test_a_plain_yes_is_not_a_broader_grant(said):
    assert not voice_text.broader_grant(said)


@pytest.mark.parametrize("said, lang", [("Ja, immer.", "de"), ("yes, for this session", None)])
def test_a_broader_grant_is_refused_before_the_grammar_and_the_judge(monkeypatch, said, lang):
    r = _rig(monkeypatch, _permission_body())
    r.decision_verdict = ("allow_once", None)  # would be wrong: the veto runs first

    async def go():
        await _offered(r)
        await _reply(r, said, lang)

    run(go())
    assert r.decided == [] and not any(p.endswith("/choose") for p, _ in _posts(r))
    assert (
        r.said_text()[-1]
        == "By voice I can only allow it once. For anything more, you'll need to tap it."
    )


# --- on a call -----------------------------------------------------------------------------------


def test_a_plain_yes_allows_it_once_through_choose(monkeypatch):
    r = _rig(monkeypatch, _permission_body())
    r.decision_verdict = ("allow_once", None)

    async def go():
        await r.ask("What needs me?")
        assert (
            r.said_text()[-1] == f"deploy in infra {BASH_SAID} Should I allow it, just this once?"
        )
        await r.readback_played()
        await r.ask("Yes, go ahead.")

    run(go())
    card, readback, reply = r.decided[0]
    assert card["kind"] == "permission" and reply == "Yes, go ahead."
    assert readback.endswith("Should I allow it, just this once?")
    assert _posts(r)[-1] == ALLOW
    assert r.said_text()[-1] == "Allowed, once."


def test_a_german_call_arms_the_dialog_and_decides_it_in_german(monkeypatch):
    r = _rig(monkeypatch, _permission_body())
    r.call.language = "de"
    r.decision_verdict = ("refuse", None)

    async def go():
        await r.ask("What needs me?")
        assert r.said_text()[-1].endswith("Soll ich das erlauben, nur dieses eine Mal?")
        assert r.call.challenge is not None  # armed, not read for the screen
        await r.readback_played()
        await _reply(r, "Nein, lehn das ab.", "de")

    run(go())
    assert _posts(r)[-1] == REFUSE


def test_allow_once_where_none_is_offered_says_so_and_chooses_nothing(monkeypatch):
    rec = _perm("claude_permission_bash.raw")
    rec = dict(rec, options=[dict(o) for o in rec["options"]])
    rec["options"][0]["persistent"] = True
    r = _rig(monkeypatch, _permission_body(rec))
    r.decision_verdict = ("allow_once", None)

    async def go():
        await _offered(r)
        await r.ask("Yes please.")

    run(go())
    assert not any(p.endswith("/choose") for p, _ in _posts(r))
    assert r.said_text()[-1] == "I can't allow this one by voice. Should I refuse it?"


def test_a_menu_option_is_picked_by_name(monkeypatch):
    r = _rig(monkeypatch, _menu_body())
    r.decision_verdict = ("pick", 1)

    async def go():
        await _offered(r)
        await r.ask("the release one")

    run(go())
    assert _posts(r)[-1] == (
        "/api/pulse/actions/act-codex:m1/choose",
        {"option": 2, "label": "release 2.0"},
    )


@pytest.mark.parametrize("verdict", [("unclear", None), ("pick", 1)])
def test_an_unclear_or_shared_name_asks_which_and_picks_nothing(monkeypatch, verdict):
    import copy

    body = copy.deepcopy(_menu_body())  # never the shared MENU fixture
    if verdict[0] == "pick":  # two options under one label: the judge's pick is refused
        for row in body["rows"]:
            menu = row["action"]["menu"]
            menu["options"] = [dict(o, label="main") for o in menu["options"]]
    r = _rig(monkeypatch, body)
    r.decision_verdict = verdict

    async def go():
        await _offered(r)
        await r.ask("the main one")

    run(go())
    assert not any(p.endswith("/choose") for p, _ in _posts(r))
    assert r.said_text()[-1] == "Which one do you mean?"


def _question_body() -> dict:
    from test_voice_needs_you import _card, _rows

    pending = [
        {"id": "ans-q", "session_id": Q_SID, "verb": "answer", "state": "pending",
         "can_approve": True, "can_reject": True, "ts": 700.0, "answer": "Use main."}
    ]  # fmt: skip
    return _rows([_card(Q_SID, "api-rebase")], pending,
                 {Q_SID: {"prompt_class": "question", "menu": None}})  # fmt: skip


def test_a_suggested_answer_is_sent_on_a_plain_yes(monkeypatch):
    r = _rig(monkeypatch, _question_body())
    r.call.language = "de"
    r.decision_verdict = ("send", None)

    async def go():
        await r.ask("What needs me?")
        assert r.said_text()[-1].endswith("Soll ich die Antwort abschicken?")
        await r.readback_played()
        await _reply(r, "Ja, schick", "de")

    run(go())
    assert ("/api/pulse/actions/ans-q/approve", None) in _posts(r)


def test_skip_judged_moves_on_to_the_next_item(monkeypatch):
    r = _rig(monkeypatch, _three())
    r.decision_verdict = ("skip", None)

    async def go():
        await _offered(r)
        await r.ask("später")

    run(go())
    assert not any(p.endswith("/choose") for p, _ in _posts(r))
    assert r.call.challenge is not None
    assert [c["id"] for c in r.call.challenge["cards"]] == [f"act-{M_SID}"]


def test_no_verdict_keeps_the_item_and_says_it_could_not_tell(monkeypatch):
    r = _rig(monkeypatch, _permission_body())  # the Rig's decision judge gives no verdict

    async def go():
        await _offered(r)
        await r.ask("hmm, ich weiß nicht")

    run(go())
    assert not any(p.endswith("/choose") for p, _ in _posts(r))
    assert r.said_text()[-1] == "Sorry, I didn't catch that. It's still waiting for you."
    assert "hmm, ich weiß nicht" not in r.asked


def test_other_passes_the_words_on_as_a_new_question(monkeypatch):
    r = _rig(monkeypatch, _permission_body())
    r.decision_verdict = ("other", None)

    async def go():
        await _offered(r)
        await r.ask("what does that command do?")

    run(go())
    assert not any(p.endswith("/choose") for p, _ in _posts(r))
    assert r.asked[-1] == "what does that command do?"


def test_a_verdict_after_a_newer_turn_began_decides_nothing(monkeypatch):
    r = _rig(monkeypatch, _permission_body())

    async def late():
        await r.ev(t="user_started")
        return ("allow_once", None)

    r.decision_verdict = late

    async def go():
        await _offered(r)
        await r.ask("ja, mach")

    run(go())
    assert not any(p.endswith("/choose") for p, _ in _posts(r))


def test_the_english_words_still_decide_at_once(monkeypatch):
    r = _rig(monkeypatch, _permission_body())
    r.decision_verdict = ("refuse", None)  # would be wrong: the grammar decides first

    async def go():
        await _offered(r)
        await r.ask("Allow it.")

    run(go())
    assert r.decided == [] and _posts(r)[-1] == ALLOW


# --- the corpus ----------------------------------------------------------------------------------


def _corpus() -> list[dict]:
    return json.loads(CORPUS.read_text())["cases"]


def test_the_corpus_covers_both_languages_and_every_kind():
    cases = _corpus()
    assert {c["lang"] for c in cases} == {"en", "de"}
    assert {c["kind"] for c in cases} == {"permission", "menu", "needs_you"}
    seen = set()
    for c in cases:
        e = c["expect"]
        seen |= set(e) if isinstance(e, list) else {e if isinstance(e, str) else "pick"}
        if isinstance(e, list):  # a hedge may land on any of these, never on a yes
            assert not {"allow_once", "send"} & set(e)
    assert {"allow_once", "refuse", "broader", "skip", "cancel", "other", "send", "unclear",
            "pick"} <= seen  # fmt: skip


@pytest.mark.parametrize(
    "case", [c for c in _corpus() if c["expect"] == "broader"], ids=lambda c: c["reply"]
)
def test_every_broader_reply_of_the_corpus_is_vetoed_before_the_judge(case):
    assert voice_text.broader_grant(case["reply"])


@pytest.mark.parametrize(
    "case",
    [c for c in _corpus() if c["kind"] == "permission" and c["expect"] == "allow_once"],
    ids=lambda c: c["reply"],
)
def test_no_allow_once_reply_of_the_corpus_is_vetoed(case):
    assert not voice_text.broader_grant(case["reply"])
