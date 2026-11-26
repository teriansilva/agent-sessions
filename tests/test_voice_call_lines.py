"""#2342 phase 2: the FIXED lines around an HQ card are said in the call's language, and HQ knows
the Commander's last used agent and project as a server fact.

`voice_text.CALL_LINES` holds English and German. A card the judge hears (#1715) is read back,
announced, answered and settled in the call's language; a Needs-you item or a connector message
keeps its English grammar, so its lines stay English (on a call in another language it is read for
the screen). The last used agent and project come from the sidebar's own list, read as the turn's
operator, and are marked ``last_used`` in `list_agents` / `list_projects`."""

from __future__ import annotations

import asyncio
import re
import string

import pytest

from agent_sessions import asgi_call, ask_orchestrator, voice_calls, voice_text
from agent_sessions.voice_text import CALL_LINES, call_line, or_list, readback, which_of

NUDGE = {"kind": "nudge", "action_id": "a1", "title": "api-rebase", "text": "Rerun the tests."}
SESSION = {"kind": "session", "action_id": "s1", "title": "docs", "text": "Refresh the docs.",
           "runtime": "api"}  # fmt: skip


def _fields(text: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(text) if f}


# --- the table ---------------------------------------------------------------------------------


def test_every_language_uses_only_english_keys_and_the_same_fields():
    en = CALL_LINES["en"]
    for lang, table in CALL_LINES.items():
        assert set(table) <= set(en), lang
        for key, text in table.items():
            assert _fields(text) == _fields(en[key]), (lang, key)
            if key.startswith(("ordinal.",)):
                assert len(text.split("|")) == len(en[key].split("|")), (lang, key)


def test_a_language_without_a_table_falls_back_to_english():
    assert call_line("ask.send it", "fr") == "Want me to send it?"
    assert call_line("ask.send it", None) == "Want me to send it?"
    assert call_line("ask.send it", "de-AT") == "Soll ich das abschicken?"


def test_no_german_line_tells_the_commander_which_words_to_say():
    banned = re.compile(r"\bsag (bestätigen|die nummer)|nummer (eins|zwei)|auf deinem bildschirm",
                        re.IGNORECASE)  # fmt: skip
    for key, text in CALL_LINES["de"].items():
        assert not banned.search(text), key


# --- a German call -----------------------------------------------------------------------------


def test_a_card_is_read_back_in_german():
    cards = voice_calls.confirmable_cards({"proposals": [NUDGE, SESSION]}, lang="de")
    assert cards[0]["line"] == "Eine Nachricht an api-rebase: Rerun the tests."
    assert cards[1]["line"] == "Eine neue API-Session in docs: Refresh the docs."
    assert readback(cards[:1], lang="de") == (
        "Eine Nachricht an api-rebase: Rerun the tests. Soll ich das abschicken?"
    )
    # two judged cards: the batch read-back (#2389), in German too
    assert readback(cards, lang="de") == (
        "Eine Nachricht und eine neue Session, die Details stehen am Bildschirm. "
        "Soll ich mit beiden loslegen?"
    )
    # a menu the judge hears but a Needs-you item is part of: by place, in German
    ask = {**cards[1], "ask": "Allow it once, refuse it, or skip it?"}
    assert readback([cards[0], ask], lang="de").startswith("Erstens: Eine Nachricht")
    assert readback([cards[0], ask], lang="de").endswith(
        "Welche soll ich machen, die erste oder die zweite?"
    )


def test_a_batch_is_counted_and_summed_up_in_german():
    renames = [{"id": str(i), "kind": "operation", "op": "session.rename"} for i in range(12)]
    assert voice_text.batch_readback(renames, "de") == (
        "Zwölf Umbenennungen, die Details stehen am Bildschirm. "
        "Soll ich mit allen zwölf loslegen?"
    )
    mixed = [*renames[:3], {"id": "p", "kind": "operation", "op": "project.create"}]
    assert voice_text.batch_kinds(mixed, "de") == "Drei Umbenennungen und ein neues Projekt"
    assert voice_text.batch_summary(total=12, done=12, lang="de") == "Erledigt, alle zwölf."
    assert voice_text.batch_summary(total=1, done=1, lang="de") == "Erledigt."
    assert voice_text.batch_summary(
        total=6, done=3, running=2, failed=1, on_screen=1, lang="de"
    ) == (
        "Drei erledigt, zwei laufen noch und eine hat nicht geklappt. "
        "Die ist noch am Bildschirm offen."
    )
    assert voice_text.batch_summary(total=2, lang="de") == "Es wurde nichts gemacht."
    # English is unchanged
    assert voice_text.batch_summary(total=6, done=3, running=2, failed=1, on_screen=1) == (
        "Three done, two still running and one didn't go through. "
        "That one is still on your screen."
    )


def test_english_is_unchanged_when_no_language_is_given():
    cards = voice_calls.confirmable_cards({"proposals": [NUDGE, SESSION]})
    assert readback(cards[:1]) == "A message to api-rebase: Rerun the tests. Want me to send it?"
    assert readback(cards) == (
        "One message and one new session, the details are on your screen. "
        "Shall I go ahead with both?"
    )


def test_a_long_brief_and_a_changed_card_are_announced_in_german():
    long = {**SESSION, "text": "x " * 400, "agent": "Claude"}
    (card,) = voice_calls.confirmable_cards({"proposals": [long]}, lang="de")
    assert card["line"] == (
        "Eine neue API-Session mit Claude in docs. Das Briefing ist zu lang zum Vorlesen; es "
        "steht im Chat."
    )
    changed = {**NUDGE, "replaces": "b" * 32}
    (card,) = voice_calls.confirmable_cards({"proposals": [changed]}, lang="de")
    assert card["line"].startswith("Geändert: Eine Nachricht an api-rebase")


def test_cards_left_for_the_screen_and_hqs_own_question_are_said_in_german():
    ask = {"question": "Welches Projekt?", "options": ["Home Lab", "Web App"]}
    view = {"proposals": [{"kind": "session", "action_id": "x", "title": "docs"}], "ask": ask}
    assert voice_text.card_sentences(view, lang="de") == [
        "Ich hab eine neue Session vorbereitet. Die schaust du dir am besten selbst an.",
        "Welches Projekt? Home Lab oder Web App?",
    ]
    assert or_list(["a", "b", "c"], "de") == "a, b oder c?"
    assert which_of(3, "de") == "die erste, die zweite oder die dritte"


def test_outcomes_are_said_in_german():
    assert voice_calls.final_sentence({"kind": "session"}, "started", "de") == "Die Session läuft."
    assert voice_calls.final_sentence({"kind": "operation"}, "done", "de") == "Erledigt."
    assert voice_calls.final_sentence({"kind": "operation"}, "weird", "de") == (
        "Das Ergebnis steht auf der Karte."
    )
    assert voice_calls.outcome_sentence({"kind": "nudge"}, None, "de") == (
        "Das hat nicht geklappt. Es wartet noch."
    )


def test_a_card_its_english_grammar_decides_keeps_english_questions():
    message = {"id": "m", "kind": "message", "verb": "send it", "line": "A message."}
    assert readback([message], judged=False, lang="de") == "A message. Send it, or cancel?"
    # on a call in another language such a card is read for the screen, closing in that language
    assert voice_text.readback_screen_only([message], "de") == (
        "A message. Das musst du selbst antippen."
    )


def test_replies_on_a_german_call_are_german(monkeypatch):
    from test_voice_confirm import TWO, Rig, run

    r = Rig(monkeypatch, TWO)
    r.call.language = "de"

    async def go():
        await r.ask("mach beides")
        assert r.said_text()[-1].endswith("Soll ich mit beiden loslegen?")
        await r.readback_played()
        await r.ask("confirm the fourth")  # names no card that was read out: asks which, in German
        assert r.said_text()[-1] == "Welche meinst du?"
        await r.readback_played()
        await r.ask("no")
        assert r.said_text()[-1] == "Okay, ich lass die so."

    run(go())


# --- last used: a server fact, marked in the lists HQ reads -------------------------------------


def _turn(**kw):
    t = ask_orchestrator._Turn(cid="c", turn_id="t", question="q", working_keys=set(),
                               progress=lambda _e: None)  # fmt: skip
    t.operator, t.app = object(), object()
    for k, v in kw.items():
        setattr(t, k, v)
    return t


def _sessions(monkeypatch, body, status=200):
    seen = []

    async def fake(app, operator, method, path, b, **kw):
        seen.append((method, path, kw.get("query")))
        return asgi_call.Response(status, body)

    monkeypatch.setattr(asgi_call, "call", fake)
    return seen


#: The route's answer (#2342, Hermes 9002): the page holds an OLDER favorite first; `newest` is
#: the route's fact over the whole scoped set, and is what counts.
BODY = {
    "sessions": [{"id": "claude:a", "engine": "claude", "created_at": 100.0, "sticky": True,
                  "project": {"kind": "project", "id": "p-old"}}],
    "next_offset": 1,
    "newest": {"engine": "codex-api", "project": {"kind": "project", "id": "p-new"}},
}  # fmt: skip


def test_last_used_is_the_routes_newest_read_once_as_the_operator(monkeypatch):
    seen = _sessions(monkeypatch, BODY)
    t = _turn()
    assert asyncio.run(ask_orchestrator._last_used(t)) == {
        "engine": "codex-api", "project_id": "p-new"
    }  # fmt: skip
    assert asyncio.run(ask_orchestrator._last_used(t))["engine"] == "codex-api"
    assert seen == [("GET", "/api/sessions", ask_orchestrator.LAST_USED_QUERY)]


def test_a_newest_session_in_no_project_marks_only_its_agent(monkeypatch):
    _sessions(
        monkeypatch,
        {**BODY, "newest": {"engine": "claude", "project": {"kind": "folder", "id": "/tmp/x"}}},
    )
    assert asyncio.run(ask_orchestrator._last_used(_turn())) == {"engine": "claude",
                                                                "project_id": ""}  # fmt: skip


@pytest.mark.parametrize(
    "body, status, operator",
    [(BODY, 500, True), ({"sessions": []}, 200, True), (BODY, 200, False),
     ({**BODY, "newest": {"engine": 5}}, 200, True), ({**BODY, "newest": "claude"}, 200, True)],
)  # fmt: skip
def test_last_used_is_empty_when_it_cannot_be_read(monkeypatch, body, status, operator):
    _sessions(monkeypatch, body, status)
    t = _turn()
    if not operator:
        t.operator = None
    assert asyncio.run(ask_orchestrator._last_used(t)) == {}


def test_list_agents_and_list_projects_mark_last_used(monkeypatch):
    _sessions(monkeypatch, BODY)
    monkeypatch.setattr(ask_orchestrator, "_agent_rows", lambda: [
        {"id": "claude", "name": "Claude Code", "can_start": True},
        {"id": "codex-api", "name": "Codex", "can_start": True, "kind": "api"},
    ])  # fmt: skip
    monkeypatch.setattr(ask_orchestrator, "_project_rows", lambda: [
        {"id": "p-old", "name": "old", "cwd": "/o", "archived": False, "startable": True},
        {"id": "p-new", "name": "new", "cwd": "/n", "archived": False, "startable": True},
    ])  # fmt: skip
    t = _turn()
    agents, _ = asyncio.run(ask_orchestrator._list_agents(t, {}))
    projects, _ = asyncio.run(ask_orchestrator._list_projects(t, {}))
    assert [a.get("last_used", False) for a in agents["agents"]] == [False, True]
    assert [p.get("last_used", False) for p in projects["projects"]] == [False, True]
    assert "last_used" in agents["note"] and "last_used" in projects["note"]


def test_nothing_is_marked_and_no_note_is_added_without_a_last_session(monkeypatch):
    _sessions(monkeypatch, {"sessions": []})
    monkeypatch.setattr(ask_orchestrator, "_project_rows", lambda: [
        {"id": "p", "name": "api", "cwd": "/a", "archived": False, "startable": True},
    ])  # fmt: skip
    projects, _ = asyncio.run(ask_orchestrator._list_projects(_turn(), {}))
    assert projects == {
        "projects": [{"index": 0, "name": "api", "archived": False, "can_start_session": True}]
    }


def test_the_call_prompts_take_last_used_when_neither_is_named():
    from agent_sessions import prompts

    for pid in ("hq_call", "hq_call_detailed"):
        text = " ".join(prompts.get(pid).default.split())
        assert "When they name neither, take what list_agents and list_projects mark last_used" in (
            text
        )
