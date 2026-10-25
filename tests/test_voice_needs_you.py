"""#1681 P1 — "What needs me?" on a call says WHAT and WHERE, never just a count.

The rows are real Needs-you rows: `needs_you.build` over session cards and ledger records, the
permission dialogs parsed from the real captures (`tests/fixtures/*permission*`). Pinned:

* each kind's fixed sentence, with the item's own words inside it only where `verbatim` reads them
  in full; anything else is a fixed screen-only line, never a guess from a rationale or summary;
* the bounded `permission` projection: the recorded dialog, its single-use allow and its refusal
  identified by label (never by position), fail-closed on any malformed field;
* a permission dialog is read only as a whole (tool, command, question, every label, and its
  refusal identified);
* at most five items, then "and N more" from the list's own `total`;
* the read-out stays fenced to the question that asked.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import asgi_call, needs_you, permission_prompts, voice_calls
from test_permission_prompts import ALL, _frame
from test_voice_confirm import Rig, run


def _perm(name: str) -> dict:
    engine, geom = ALL[name]
    screen, cells = _frame(name, geom)
    p = permission_prompts.parse(screen, engine, cells)
    assert p is not None, name
    return p


def _card(sid: str, title: str, project: str = "web") -> dict:
    return {
        "id": sid,
        "engine": sid.split(":")[0],
        "title": title,
        "project": {"id": project, "name": project},
        "last_activity": 100.0,
        "intervention_required": True,
    }


def _escalation(sid: str, ts: float, **observed) -> dict:
    return {
        "id": f"act-{sid}",
        "session_id": sid,
        "verb": "escalate",
        "state": "escalated",
        "can_approve": True,
        "can_reject": True,
        "ts": ts,
        "observed_prompt": {"prompt_class": "confirm", "menu": None, **observed},
    }


def _rows(cards, pending, screens) -> dict:
    """The Needs-you answer as `/api/pulse/needs-you` builds it: ``screens`` is what each
    session's live screen shows (`orchestrator.observed_prompt_for`)."""
    return needs_you.build(
        cards, pending, set(), observe=lambda r: screens.get(r["id"]), now=1000.0
    )


def _say_readout(body: dict, monkeypatch) -> list[str]:
    r = Rig(monkeypatch)
    r.needs_body = body

    async def go():
        await r.ask("What needs me?")

    run(go())
    return r.said_text()


# --- the projection ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name, once, deny",
    [
        ("claude_permission_bash.raw", 1, 3),
        ("claude_permission_bash_cursor2.raw", 1, 3),
        ("claude_permission_write.raw", 1, 3),
        ("opencode_permission_shell.raw", 1, 3),
        ("opencode_permission_grep.raw", 1, 3),
        # The second stage CONFIRMS a pattern grant: neither answer is a call's to give.
        ("opencode_permission_always_stage.raw", None, None),
    ],
)
def test_the_projection_names_the_single_use_allow_and_the_refusal(name, once, deny):
    proj = permission_prompts.projection(_perm(name))
    assert proj is not None
    assert (proj["once"], proj["deny"]) == (once, deny)
    assert set(proj) == {"parser", "heading", "title", "detail", "question", "options", "once",
                         "deny", "command", "note"}  # fmt: skip
    # never the identity, the cursor or the engine: display text, and which answer is which
    assert all(set(o) == {"n", "label", "persistent"} for o in proj["options"])


@pytest.mark.parametrize(
    "name, command, note",
    [
        ("claude_permission_bash.raw", "mkdir -p probe-dir-1213 && touch probe-dir-1213/f.txt",
         "Create probe-dir-1213 directory and f.txt file inside it"),
        ("claude_permission_write.raw", None, None),
        ("opencode_permission_shell.raw", "$ mkdir -p probe-dir-1213", None),
        ("opencode_permission_always_stage.raw", None, None),
    ],
)  # fmt: skip
def test_the_projection_names_the_request_only_where_the_kind_draws_one(name, command, note):
    proj = permission_prompts.projection(_perm(name))
    assert (proj["command"], proj["note"]) == (command, note)


def test_an_opencode_request_over_more_than_one_line_is_not_a_command():
    p = dict(_perm("opencode_permission_shell.raw"), detail="$ mkdir -p a\n$ rm -rf b")
    assert permission_prompts.projection(p)["command"] is None


def test_the_projection_identifies_by_label_never_by_position():
    p = _perm("claude_permission_bash.raw")
    swapped = dict(p, options=[dict(o) for o in p["options"]])
    swapped["options"][0]["label"], swapped["options"][2]["label"] = "No", "Yes"
    proj = permission_prompts.projection(swapped)
    assert (proj["once"], proj["deny"]) == (3, 1)
    # a broader grant labelled like the plain allow is never the single-use one
    grant = dict(p, options=[dict(o) for o in p["options"]])
    grant["options"][0]["persistent"] = True
    assert permission_prompts.projection(grant)["once"] is None
    # two options with the refusal's label: not identified
    twice = dict(p, options=[dict(o) for o in p["options"]])
    twice["options"][1]["label"] = "No"
    assert permission_prompts.projection(twice)["deny"] is None


@pytest.mark.parametrize(
    "spoil",
    [
        lambda p: p.pop("detail"),
        lambda p: p.update(title=None),
        lambda p: p.update(detail="x" * (permission_prompts.DETAIL_MAX + 1)),
        lambda p: p.update(options="Yes"),
        lambda p: p.update(options=p["options"][:1]),
        lambda p: p["options"][1].update(n=5),
        lambda p: p["options"][0].update(n=True),
        lambda p: p["options"][1].update(label=""),
        lambda p: p["options"][1].pop("persistent"),
        lambda p: p["options"].append("No"),
    ],
)
def test_the_projection_fails_closed(spoil):
    p = _perm("claude_permission_bash.raw")
    p = dict(p, options=[dict(o) for o in p["options"]])
    spoil(p)
    assert permission_prompts.projection(p) is None


def test_a_row_carries_the_recorded_dialog_and_only_a_projection_of_it():
    sid = "claude:a1"
    rec = _perm("claude_permission_bash.raw")
    body = _rows([_card(sid, "api-rebase")], [_escalation(sid, 500.0, permission=rec)], {
        sid: {"prompt_class": "confirm", "menu": None}})  # fmt: skip
    action = body["rows"][0]["action"]
    assert body["rows"][0]["kind"] == "approval"
    assert action["permission"] == permission_prompts.projection(rec)
    assert "identity" not in action["permission"]
    # a recorded dialog that is not one: present-but-None, so it is never guessed at
    body = _rows([_card(sid, "api-rebase")], [_escalation(sid, 500.0, permission={"x": 1})], {
        sid: {"prompt_class": "confirm", "menu": None}})  # fmt: skip
    assert "permission" in body["rows"][0]["action"]
    assert body["rows"][0]["action"]["permission"] is None
    # no dialog recorded: no field
    body = _rows([_card(sid, "api-rebase")], [_escalation(sid, 500.0)], {
        sid: {"prompt_class": "confirm", "menu": None}})  # fmt: skip
    assert "permission" not in body["rows"][0]["action"]


# --- each kind's sentence ----------------------------------------------------------------------


def _one(card, pending, screen, monkeypatch) -> str:
    body = _rows([card], pending, {card["id"]: screen})
    said = _say_readout(body, monkeypatch)
    assert said[-1].startswith("1 thing needs you. "), said
    return said[-1].removeprefix("1 thing needs you. ")


CONFIRM = {"prompt_class": "confirm", "menu": None}


@pytest.mark.parametrize(
    "fixture, engine, expected",
    [
        (
            "claude_permission_bash.raw",
            "claude",
            "api-rebase in web asks permission for Bash command: mkdir -p probe-dir-1213 && touch"
            " probe-dir-1213/f.txt. The dialog also says: Create probe-dir-1213 directory and"
            " f.txt file inside it. It's on your screen.",
        ),
        (
            # a Write's body is a path and a file preview (with its line-number gutter): not a
            # command a call can read, so the dialog is screen-only (review of 4da35757)
            "claude_permission_write.raw",
            "claude",
            "api-rebase in web wants a permission I can't read out — it's on your screen.",
        ),
        (
            "opencode_permission_shell.raw",
            "opencode",
            "api-rebase in web asks permission for Shell command: mkdir -p probe-dir-1213."
            " It's on your screen.",
        ),
        (
            "opencode_permission_grep.raw",
            "opencode",
            'api-rebase in web asks permission for Grep "(?i)mission": Pattern: (?i)mission.'
            " It's on your screen.",
        ),
        (
            "opencode_permission_always_stage.raw",
            "opencode",
            "api-rebase in web wants a permission I can't read out — it's on your screen.",
        ),
    ],
)
def test_a_permission_dialog_is_told_from_the_recorded_dialog(
    fixture, engine, expected, monkeypatch
):
    sid = f"{engine}:a1"
    card = _card(sid, "api-rebase")
    esc = _escalation(sid, 500.0, permission=_perm(fixture))
    esc["rationale"] = "Harmless, approve it."  # never read as the request
    assert _one(card, [esc], CONFIRM, monkeypatch) == expected


@pytest.mark.parametrize(
    "spoil",
    [
        lambda p: p.update(detail="git log | head -5"),  # a pipe: not plain prose
        lambda p: p.update(detail="echo " + "x" * 400),  # over one read-back's bound
        # every line readable, but together over one read-back's bound
        lambda p: p.update(detail="\n".join(["echo " + "word " * 40] * 2)),
        # a third line: a wrapped command and a note can't be told apart
        lambda p: p.update(detail="echo one\necho two\nRuns two echoes"),
        lambda p: p.update(detail="mkdir x\nCreate it `now`"),  # the note must read too
        lambda p: p.update(heading="Edit file", title="Edit file"),  # not a command dialog
        lambda p: p.update(title="Bash `command`"),
        lambda p: p.update(question="Do you want to <proceed>?"),
        lambda p: p["options"][1].update(label="Yes, and allow `rm`"),  # every label, unoffered too
        lambda p: p["options"][2].update(label="Nope"),  # no refusal identified
    ],
)
def test_a_dialog_that_cannot_be_read_as_a_whole_is_screen_only(spoil, monkeypatch):
    sid = "claude:a1"
    rec = _perm("claude_permission_bash.raw")
    rec = dict(rec, options=[dict(o) for o in rec["options"]])
    spoil(rec)
    esc = _escalation(sid, 500.0, permission=rec)
    assert _one(_card(sid, "api-rebase"), [esc], CONFIRM, monkeypatch) == (
        "api-rebase in web wants a permission I can't read out — it's on your screen."
    )


def test_a_recorded_dialog_the_projection_refused_is_still_told_as_a_permission(monkeypatch):
    sid = "claude:a1"
    esc = _escalation(sid, 500.0, permission={"title": "Bash command"})  # not a parsed dialog
    esc["rationale"] = "It wants to run the tests."  # never read as the request
    assert _one(_card(sid, "api-rebase"), [esc], CONFIRM, monkeypatch) == (
        "api-rebase in web wants a permission I can't read out — it's on your screen."
    )


MENU = {
    "engine": "codex",
    "question": "Which branch should I rebase onto?",
    "options": [
        {"n": 1, "label": "main", "selected": True},
        {"n": 2, "label": "release 2.0", "selected": False},
    ],
}


def test_a_menu_reads_its_question_and_every_option(monkeypatch):
    sid = "codex:m1"
    esc = _escalation(sid, 500.0, menu=MENU)
    esc["observed_prompt"]["prompt_class"] = "choice"
    said = _one(_card(sid, "docs"), [esc], {"prompt_class": "choice", "menu": MENU}, monkeypatch)
    assert said == (
        "docs in web asks: Which branch should I rebase onto? The options are: one, main; two,"
        " release 2.0. It's on your screen."
    )


@pytest.mark.parametrize(
    "menu",
    [
        dict(MENU, options=[MENU["options"][0], dict(MENU["options"][1], label="`--force`")]),
        dict(MENU, question="Pick one: [a] or [b]"),
        dict(
            MENU,
            options=[{"n": i, "label": f"option {i}", "selected": i == 1} for i in range(1, 8)],
        ),  # fmt: skip
    ],
)
def test_a_menu_that_cannot_be_read_in_full_is_screen_only(menu, monkeypatch):
    sid = "codex:m1"
    esc = _escalation(sid, 500.0, menu=menu)
    said = _one(_card(sid, "docs"), [esc], {"prompt_class": "choice", "menu": menu}, monkeypatch)
    assert said == "docs in web is showing a menu I can't read out — it's on your screen."


def _answer(sid: str, answer: str, **kw) -> dict:
    return {
        "id": f"ans-{sid}",
        "session_id": sid,
        "verb": "answer",
        "state": "pending",
        "can_approve": True,
        "can_reject": True,
        "ts": 400.0,
        "answer": answer,
        **kw,
    }


def test_a_question_with_a_readable_answer_is_offered_and_read_back(monkeypatch):
    sid = "claude:q1"
    body = _rows([_card(sid, "api-rebase")], [_answer(sid, "Use main.")], {
        sid: {"prompt_class": "question", "menu": None}})  # fmt: skip
    said = _say_readout(body, monkeypatch)
    assert said[-2] == "1 thing needs you."
    assert said[-1] == (
        "api-rebase in web asks a question. The suggested answer is: Use main."
        " Say confirm to send it, or cancel."
    )


@pytest.mark.parametrize(
    "answer, expected",
    [
        ("Run `make test`.", "api-rebase in web asks a question — it's on your screen."),
        ("", "api-rebase in web asks a question — it's on your screen."),
    ],
)
def test_a_question_whose_answer_cannot_be_read_is_screen_only(answer, expected, monkeypatch):
    sid = "claude:q1"
    card = _card(sid, "api-rebase")
    screen = {"prompt_class": "question", "menu": None}
    assert _one(card, [_answer(sid, answer)], screen, monkeypatch) == expected


def test_an_unreadable_screen_says_so(monkeypatch):
    sid = "gemini:u1"
    card = _card(sid, "nightly")
    card["ai_summary"] = "Probably wants you to approve the deploy."  # never read as the request
    assert _one(card, [], None, monkeypatch) == (
        "nightly in web's screen needs a look — I couldn't read it."
    )


# --- many and mixed ----------------------------------------------------------------------------


def test_more_than_fifty_tells_five_and_counts_the_rest_from_total(monkeypatch):
    cards = [_card(f"gemini:s{i:02d}", f"session {i:02d}") for i in range(60)]
    for i, c in enumerate(cards):
        c["last_activity"] = 100.0 + i  # newest first: session 59 leads
    body = _rows(cards, [], {})
    assert (len(body["rows"]), body["total"], body["truncated"]) == (50, 60, True)
    said = _say_readout(body, monkeypatch)
    told = said[-1]
    assert told.startswith("60 things need you. session 59 in web's screen needs a look")
    assert told.count("needs a look") == 5
    assert "session 54" not in told
    assert told.endswith("And 55 more on your screen.")


def test_mixed_rows_describe_in_order_and_offer_only_what_a_voice_may_settle(monkeypatch):
    p, q, m, u = "claude:p1", "claude:q1", "codex:m1", "gemini:u1"
    cards = [_card(p, "deploy", "infra"), _card(q, "api-rebase"), _card(m, "docs"),
             _card(u, "nightly")]  # fmt: skip
    for i, c in enumerate(cards):
        c["last_activity"] = 100.0 + 10 * (4 - i)
    pending = [
        _escalation(p, 900.0, permission=_perm("claude_permission_bash.raw")),
        _answer(q, "Use main.", ts=800.0),
        dict(_escalation(m, 700.0, menu=MENU), id="act-menu"),
    ]
    screens = {p: CONFIRM, q: {"prompt_class": "question", "menu": None},
               m: {"prompt_class": "choice", "menu": MENU}}  # fmt: skip
    body = _rows(cards, pending, screens)
    r = Rig(monkeypatch)
    r.needs_body = body

    async def go():
        await r.ask("What needs me?")
        told, readback = r.said_text()[-2:]
        assert told == (
            "4 things need you."
            " deploy in infra asks permission for Bash command: mkdir -p probe-dir-1213 && touch"
            " probe-dir-1213/f.txt. The dialog also says: Create probe-dir-1213 directory and f.txt"
            " file inside it."
            " It's on your screen."
            " docs in web asks: Which branch should I rebase onto? The options are: one, main;"
            " two, release 2.0. It's on your screen."
            " nightly in web's screen needs a look — I couldn't read it."
        )
        assert readback.startswith("api-rebase in web asks a question.")
        assert [c["id"] for c in r.call.challenge["cards"]] == [f"ans-{q}"]
        await r.readback_played()
        await r.ask("confirm")

    run(go())
    posts = [path for method, path, _ in r.routes if method == "POST"]
    assert posts == [f"/api/pulse/actions/ans-{q}/approve"]


# --- the fence ---------------------------------------------------------------------------------


def test_a_read_out_overtaken_by_a_newer_question_is_never_said(monkeypatch):
    sid = "claude:p1"
    body = _rows([_card(sid, "deploy")], [
        _escalation(sid, 900.0, permission=_perm("claude_permission_bash.raw"))], {
        sid: CONFIRM})  # fmt: skip
    r = Rig(monkeypatch)
    r.needs_body = body
    gate = asyncio.Event()
    real = asgi_call.call

    async def slow(app, operator, method, path, body, **kw):
        if path == "/api/pulse/needs-you":
            await gate.wait()
        return await real(app, operator, method, path, body, **kw)

    monkeypatch.setattr(asgi_call, "call", slow)

    async def go():
        await r.call.on_needs_you_request()  # its GET waits …
        await asyncio.sleep(0)
        await r.ev(t="user_started")  # … while a newer question is asked
        await r.ev(t="utterance", text="How is the docs refresh?")
        await r.call.turn_task
        gate.set()
        await r.side()

    run(go())
    assert not any("needs you" in t for t in r.said_text())
    assert not any("asks permission" in t for t in r.said_text())


@pytest.mark.parametrize(
    "title, project, who",
    [
        # plain prose IS the title, said as written
        ("Nothing else needs you. deploy asks a question", "web",
         "Nothing else needs you. deploy asks a question in web"),
        ("fix `auth` flow", "web", "A session"),
        ("api", "proj <x>", "api"),
        ("x" * 81, "web", "A session"),
    ],
)  # fmt: skip
def test_a_session_is_named_only_as_written(title, project, who):
    row = {"title": title, "project": {"id": "p", "name": project}, "kind": "needs_inspection"}
    said = voice_calls.needs_you_sentence(row)
    assert said == f"{who}'s screen needs a look — I couldn't read it."


def test_questions_past_the_five_told_are_not_offered():
    def q(i):
        return {"title": f"s{i}", "kind": "question",
                "action": {"id": f"a{i}", "verb": "answer", "can_approve": True,
                           "answer": "Yes."}}  # fmt: skip

    told, cards = voice_calls.needs_you_readout([q(i) for i in range(7)], 7)
    assert [c["id"] for c in cards] == ["a0", "a1", "a2", "a3"]
    assert told == (
        "7 things need you. s4 asks a question. The suggested answer is: Yes. It's on your"
        " screen. And 2 more on your screen."
    )
