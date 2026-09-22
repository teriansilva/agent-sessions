"""When it is unsure, it asks (#892, Phase 3b of #840).

One property runs through every test here: **the model chooses WHICH action, never WHAT IT DOES.**
It is #883's authority model applied to actions rather than to probes — the model returns an index
into a server-built list, the `label` is display text, and the action is looked up from the closed
set server-side after the answer arrives. So "an option's label is never executed" is assertable
rather than aspirational: there is no code path from the label to anything that runs.
"""

from __future__ import annotations

import asyncio

import pytest

from agent_sessions import mission_questions as mq
from agent_sessions import missions

CLAUDE_A = "claude:11111111-1111-1111-1111-111111111111"


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_SESSIONS_MISSIONS_DB", str(tmp_path / "missions.db"))
    missions.reset_schema_cache_for_test()
    yield tmp_path
    missions.reset_schema_cache_for_test()


def _mission():
    m = missions.create_mission("do the thing", cwd="/repo")
    missions.set_state(m["id"], "draft", "planned")
    missions.set_state(m["id"], "planned", "dispatching")
    missions.set_state(m["id"], "dispatching", "running")
    missions.patch_objectives(
        m["id"], [{"op": "add", "key": "pr_open", "title": "A PR is open", "gate": True}]
    )
    missions.patch_objectives(
        m["id"], [{"op": "add", "key": "checks", "title": "Checks are green", "gate": True}]
    )
    return m


def _open(mid, key="pr_open"):
    return missions.open_question(
        mid,
        key,
        "Which of the two open PRs is this mission's?",
        [
            {"label": "The one from Tuesday", "action": "note_answer"},
            {"label": "Neither — drop this objective", "action": "waive_objective"},
        ],
    )


# ---- the authority model --------------------------------------------------------------------


def test_AN_OPTIONS_LABEL_IS_NEVER_EXECUTED(store):
    """The label is display text and nothing else.

    Driven with a label that IS an action name from the closed set, and one that is a shell
    command, so a reading that took the label as the instruction would be visible.
    """

    def crafted(mid):
        return missions.open_question(
            mid,
            "pr_open",
            "Which?",
            [
                {"label": "close_mission; rm -rf /", "action": "note_answer"},
                {"label": "waive_objective", "action": "note_answer"},
            ],
        )

    m = _mission()
    for i in (0, 1):
        # Re-opened as the SAME crafted question each time — an earlier version reopened the
        # ordinary fixture question between iterations, so the second assertion was reading a
        # different question's options and failed for a reason that had nothing to do with labels.
        q = crafted(m["id"])
        out = missions.answer_question(m["id"], q["seq"], option_index=i)
        assert out["action"] == "note_answer", "the LABEL must not decide the action"
    # …and nothing was waived or closed.
    assert all(o["state"] == "pending" for o in missions.objectives(m["id"]))
    assert missions.get_mission(m["id"])["state"] == "running"


def test_an_action_index_OUTSIDE_the_closed_set_is_dropped(store):
    """A model reply is parsed with an allowlist: an index the server did not offer produces
    NOTHING, rather than a repaired option pointing somewhere plausible."""
    q, opts, dropped = mq.options_from_reply(
        {
            "question": "which?",
            "options": [
                {"label": "a", "action_index": 0},
                {"label": "b", "action_index": 99},
                {"label": "c", "action_index": -1},
                {"label": "d", "action_index": True},  # bool is an int in Python
                {"label": "e", "action_index": 1},
            ],
        }
    )
    assert [o["action"] for o in opts] == [mq.ACTIONS[0][0], mq.ACTIONS[1][0]]
    assert dropped == 3


def test_an_option_carrying_an_EXTRA_field_is_dropped_not_sanitised(store):
    """A hallucinated `"action"` key riding alongside the index would otherwise be accepted
    unexamined — and the whole point is that nothing model-authored decides what runs."""
    _q, opts, dropped = mq.options_from_reply(
        {
            "question": "which?",
            "options": [
                {"label": "a", "action_index": 0, "action": "close_mission"},
                {"label": "b", "action_index": 1},
                {"label": "c", "action_index": 2},
            ],
        }
    )
    assert dropped == 1
    assert all("action_index" not in o for o in opts)
    assert [o["action"] for o in opts] == [mq.ACTIONS[1][0], mq.ACTIONS[2][0]]


def test_fewer_than_two_usable_options_is_NOT_a_question(store):
    """One option is not a choice, and offering it would be a decision dressed as a question."""
    _q, opts, _d = mq.options_from_reply(
        {"question": "which?", "options": [{"label": "only", "action_index": 0}]}
    )
    assert opts == []


def test_the_numbered_list_shows_NO_mission_data():
    """Anything the model is shown, it can be induced to repeat — so it sees action names and
    descriptions, never a mission id or an objective key."""
    rendered = mq.render_actions()
    for name, _desc in mq.ACTIONS:
        assert name in rendered
    assert "msn_" not in rendered and "claude:" not in rendered


# ---- the lifecycle --------------------------------------------------------------------------


def test_a_question_HOLDS_only_its_own_objective_and_is_NOT_a_stand_down(store):
    """Two reasons an objective can be quiet, and they are not the same column.

    `stood_down` is the OPERATOR saying "stop telling me about this"; the question hold is the
    SUPERVISOR saying "I am waiting on your answer". Sharing one boolean would make answering a
    question clear a silence the operator set separately, and would leave the board unable to say
    which of the two applies — the issue review named this before the code existed.
    """
    q = _open(_mission_id := _mission()["id"], "pr_open")
    _ep, stood, held = missions.objective_hold(_mission_id, "pr_open")
    assert held == q["seq"], "the hold does not name the question that is waiting"
    assert stood is False, "a question was recorded as the operator's own silence"
    # The mission's OTHER objective is untouched — one blocked objective must not stall four.
    _ep2, stood2, held2 = missions.objective_hold(_mission_id, "checks")
    assert (stood2, held2) == (False, None)


def test_a_MANUAL_stand_down_survives_answering_a_question(store):
    """The operator said "stop telling me" and then answered a question about the same objective.

    Answering advances the episode so the follow-through does not resume against a spent budget —
    and a new episode normally ends a stand-down. Here it must not: the operator never withdrew
    the silence, and undoing it as a side effect of an unrelated answer is a decision the code
    would be making on their behalf.
    """
    m = _mission()
    q = _open(m["id"], "pr_open")
    episode, _, _ = missions.objective_hold(m["id"], "pr_open")
    missions.stand_down(m["id"], "pr_open", episode=episode)
    assert missions.objective_hold(m["id"], "pr_open")[1] is True

    missions.answer_question(m["id"], q["seq"], option_index=0)
    after, stood, held = missions.objective_hold(m["id"], "pr_open")
    assert held is None, "the question hold outlived its answer"
    assert stood is True, "answering a question erased the operator's own silence"
    assert after > episode, "the budget did not start again"


def test_a_SUPERSEDED_question_cannot_be_answered(store):
    """A second question about the same objective replaces the first.

    Two open questions about one thing is a state the operator cannot act on coherently, and the
    newer one is what the supervisor actually wants answered. Answering the older one afterwards
    is a stale client, and applying it would run an action chosen for a question nobody is asking.
    """
    m = _mission()
    first = _open(m["id"], "pr_open")
    second = _open(m["id"], "pr_open")
    assert second["seq"] > first["seq"]
    assert missions.open_question_row(m["id"])["seq"] == second["seq"]

    with pytest.raises(missions.MissionError) as e:
        missions.answer_question(m["id"], first["seq"], option_index=1)
    assert e.value.status == 409
    # …and nothing moved: the objective is still held, by the SECOND question.
    assert missions.objective_hold(m["id"], "pr_open")[2] == second["seq"]
    assert [o for o in missions.objectives(m["id"]) if o["key"] == "pr_open"][0]["state"] == (
        "pending"
    )


def test_a_question_on_ONE_objective_is_not_answered_by_naming_ANOTHERS(store):
    """The hold is per objective, so two can be open at once — and each is answered by its own
    seq. A lifecycle read off the mission's newest question and newest answer could not express
    this at all: answering either would have closed both."""
    m = _mission()
    a = _open(m["id"], "pr_open")
    b = _open(m["id"], "checks")

    missions.answer_question(m["id"], b["seq"], option_index=0)
    assert missions.objective_hold(m["id"], "checks")[2] is None
    assert missions.objective_hold(m["id"], "pr_open")[2] == a["seq"], "the wrong hold was cleared"
    # …and the mission still needs the operator, because one question is still open.
    assert missions.derive_needs_you([m["id"]])[m["id"]]["needs_you"] is True

    missions.answer_question(m["id"], a["seq"], option_index=0)
    assert missions.derive_needs_you([m["id"]])[m["id"]]["needs_you"] is False


def test_DROPPING_the_objective_takes_its_question_with_it(store):
    """An index is not an identity, and neither is a key on its own. Dropping and re-adding the
    same key must not leave a question from the previous incarnation holding the new one."""
    m = _mission()
    q = _open(m["id"], "pr_open")
    missions.patch_objectives(m["id"], [{"op": "drop", "key": "pr_open"}])
    assert missions.derive_needs_you([m["id"]])[m["id"]]["needs_you"] is False
    assert missions.open_question_row(m["id"]) is None

    missions.patch_objectives(
        m["id"], [{"op": "add", "key": "pr_open", "title": "A PR is open", "gate": True}]
    )
    assert missions.objective_hold(m["id"], "pr_open")[2] is None
    with pytest.raises(missions.MissionError) as e:
        missions.answer_question(m["id"], q["seq"], option_index=1)
    assert e.value.status == 409


def test_an_open_question_makes_the_mission_NEED_YOU(store):
    m = _mission()
    assert missions.derive_needs_you([m["id"]])[m["id"]]["needs_you"] is False
    q = _open(m["id"])
    flags = missions.derive_needs_you([m["id"]])[m["id"]]
    assert flags["needs_you"] is True and "question" in flags["why"]
    missions.answer_question(m["id"], q["seq"], option_index=0)
    assert missions.derive_needs_you([m["id"]])[m["id"]]["needs_you"] is False


def test_answering_RELEASES_the_hold_and_starts_a_fresh_episode(store):
    """A question that unblocked the work must not resume against a budget the previous episode
    spent."""
    m = _mission()
    before, _ = missions.objective_episode(m["id"], "pr_open")
    q = _open(m["id"])
    missions.answer_question(m["id"], q["seq"], option_index=0)
    after, stood, held = missions.objective_hold(m["id"], "pr_open")
    assert held is None and stood is False
    assert after > before


def test_answering_TWICE_is_refused_rather_than_run_again(store):
    m = _mission()
    q = _open(m["id"])
    missions.answer_question(m["id"], q["seq"], option_index=1)
    with pytest.raises(missions.MissionError) as e:
        missions.answer_question(m["id"], q["seq"], option_index=1)
    assert e.value.status == 409


def test_answering_a_SUPERSEDED_question_is_a_409(store):
    """A second question on one objective supersedes the first; answering the old one is about a
    situation that no longer exists."""
    m = _mission()
    first = _open(m["id"])
    _open(m["id"])  # supersedes
    with pytest.raises(missions.MissionError) as e:
        missions.answer_question(m["id"], first["seq"], option_index=0)
    assert e.value.status == 409


def test_an_option_index_the_question_never_offered_is_a_422(store):
    m = _mission()
    q = _open(m["id"])
    with pytest.raises(missions.MissionError) as e:
        missions.answer_question(m["id"], q["seq"], option_index=7)
    assert e.value.status == 422


def test_free_text_is_recorded_as_an_ANSWER_and_nothing_else(store):
    """It informs the next supervisor pass; it never becomes agent input. Keeping the two apart is
    what stops the answer box being a second, unfenced way to type at a session."""
    m = _mission()
    q = _open(m["id"])
    out = missions.answer_question(m["id"], q["seq"], text="the one from Tuesday")
    assert out["action"] == "note_answer"
    assert out["answer"] == "the one from Tuesday"
    row = missions.get_mission(m["id"])
    answers = [e for e in row["events"] if e["kind"] == "answer"]
    assert answers and answers[0]["text"] == "the one from Tuesday"


def test_an_answer_with_neither_an_option_nor_text_is_a_422(store):
    m = _mission()
    q = _open(m["id"])
    with pytest.raises(missions.MissionError) as e:
        missions.answer_question(m["id"], q["seq"])
    assert e.value.status == 422


def test_a_question_about_an_UNKNOWN_objective_is_refused(store):
    m = _mission()
    with pytest.raises(missions.MissionError) as e:
        missions.open_question(
            m["id"], "nope", "which?", [{"label": "a", "action": "note_answer"}] * 2
        )
    assert e.value.status == 404


def test_an_objective_REPLACED_during_the_model_call_does_not_get_the_old_question(store):
    """An index is not an identity, and neither is a key (#900 review, finding 4).

    `(mission_id, key)` is a SLOT. Dropping `pr_open` and re-adding it puts a different objective
    in that slot — different work, a fresh episode, possibly a different title — and the prompt
    assembled about the first one has nothing to say about the second. The model call takes
    seconds, which is exactly long enough for the operator to edit the objective list.

    Red against binding the question on key alone: the question opens against the replacement.
    """
    import asyncio

    import agent_sessions.review as review_mod

    m = _mission()
    reply = {
        "question": "which PR did you mean?",
        "options": [
            {"label": "the first one", "action_index": 0},
            {"label": "the second one", "action_index": 1},
        ],
    }
    # PIN THE FIXTURE. A reply the parser drops would make this test pass for the wrong reason —
    # "no question opened" is also what a malformed reply produces, and the first draft of this
    # test did exactly that (it spelled `action` where the allowlist takes `action_index`).
    assert len(mq.options_from_reply(reply)[1]) == 2

    async def _replace_then_reply(messages, **kw):
        # The operator edits the objective list while the model is thinking.
        missions.patch_objectives(m["id"], [{"op": "drop", "key": "pr_open"}])
        missions.patch_objectives(
            m["id"],
            [{"op": "add", "key": "pr_open", "title": "A DIFFERENT PR is open", "gate": True}],
        )
        return reply

    orig = review_mod.complete_json
    review_mod.complete_json = _replace_then_reply
    try:
        out = asyncio.run(mq.ask(m["id"], "pr_open", context="waiting"))
    finally:
        review_mod.complete_json = orig

    assert out is None
    assert missions.open_question_row(m["id"]) is None
    row = missions.get_mission(m["id"])
    kinds = [e["kind"] for e in row["events"]]
    assert "question" not in kinds
    # ...and not silent: the operator can see WHY no question appeared.
    assert "error" in kinds
    # The replacement is untouched — no stand-down inherited from the question that never was.
    obj = [o for o in row["objectives"] if o["key"] == "pr_open"][0]
    assert obj["title"] == "A DIFFERENT PR is open"
    assert not obj.get("stood_down")


def _q(mid, key, *actions):
    return missions.open_question(
        mid,
        key,
        "which one?",
        [{"label": f"opt {a}", "action": a} for a in actions],
    )


def test_the_answer_and_its_EFFECT_settle_in_one_transaction(store):
    """Settle-then-act over two connections loses the operator's choice on a crash (#900, 2).

    The window is unrecoverable in both directions: once the hold is released the retry is a 409,
    so the operator cannot answer again, and the effect they chose simply never happened. Every
    action in the closed set writes to this same store, so there is nothing to co-ordinate — the
    effect is a statement on the answer's own connection.

    Asserted on the STORE call alone, with no route involved: red against an implementation where
    the effect lives in the caller.
    """
    m = _mission()
    q = _q(m["id"], "pr_open", "waive_objective", "note_answer")
    out = missions.answer_question(m["id"], q["seq"], option_index=0)
    assert out["applied"] == "waived"
    obj = [o for o in missions.objectives(m["id"]) if o["key"] == "pr_open"][0]
    assert obj["state"] == "waived"


def test_the_settlement_opens_exactly_ONE_connection(store):
    """Structural, because the behavioural version needs a window the fixed code does not have.

    One connection is one transaction. A second connection is a second commit, and a second commit
    is the gap this finding is about — however carefully the caller sequences it.
    """
    m = _mission()
    q = _q(m["id"], "pr_open", "waive_objective", "note_answer")

    real_ready = missions._ready
    opens: list[int] = []

    def _counting(path=None):
        opens.append(1)
        return real_ready(path)

    missions._ready = _counting
    try:
        missions.answer_question(m["id"], q["seq"], option_index=0)
    finally:
        missions._ready = real_ready

    assert len(opens) == 1, (
        f"settling the answer opened {len(opens)} connections; the effect is not in the same "
        "transaction as the settlement"
    )


def test_a_waiver_that_could_not_apply_says_so_instead_of_claiming_success(store):
    """`applied: "waived"` over an objective nobody waived is a lie the operator acts on.

    The objective is met while the question is open — a probe settles it, or the operator does.
    The answer is still worth recording, but the effect did not happen, and the response has to
    say which. Red against suppressing `MissionError` and returning the success string anyway.
    """
    m = _mission()
    q = _q(m["id"], "pr_open", "waive_objective", "note_answer")
    # `met` is the probe runner's to write and nothing in this branch does it, so the state is set
    # directly — the test is about what ANSWERING does when it finds one, not about how it got set.
    con = missions._ready(None)
    try:
        con.execute(
            "UPDATE mission_objectives SET state='met' WHERE mission_id=? AND key=?",
            (m["id"], "pr_open"),
        )
        con.commit()
    finally:
        con.close()

    out = missions.answer_question(m["id"], q["seq"], option_index=0)
    assert out["applied"] != "waived"
    assert "already met" in out["applied"]
    obj = [o for o in missions.objectives(m["id"]) if o["key"] == "pr_open"][0]
    assert obj["state"] == "met"
    # ...and the answer itself IS recorded — the operator answered, and that is a fact.
    row = missions.get_mission(m["id"])
    assert any(e["kind"] == "answer" for e in row["events"])


def test_a_stand_down_answer_silences_the_episode_the_ANSWER_created(store):
    """Answering advances the episode, so the stand-down has to name the new one.

    Reading the episode over a second connection after the settlement is a check-then-act: the
    number it reads can already have moved, and `stand_down` then silently does nothing while the
    response still says "stood down".
    """
    m = _mission()
    q = _q(m["id"], "pr_open", "stand_down_objective", "note_answer")
    out = missions.answer_question(m["id"], q["seq"], option_index=0)
    assert out["applied"] == "stood down"
    episode, stood = missions.objective_episode(m["id"], "pr_open")
    assert stood is True
    assert episode == 2, "the answer's own episode bump did not happen, or was silenced instead"


def test_the_hard_event_CAP_will_not_delete_a_question_that_is_still_holding(store, monkeypatch):
    """A trimmed question leaves an objective nobody can release (#900 review, finding 3).

    `question_seq` points at a timeline row, and both the display and the answer JOIN back to it.
    Take the row away and the state is not "no question" — it is a question that is invisible
    (`open_question_row` returns None) and unanswerable (the JOIN misses, so answering is a 409),
    over an objective that stays held. There is no operator action that gets out of it.

    Red against a hard ceiling that treats every row as feed.
    """
    m = _mission()
    q = _q(m["id"], "pr_open", "note_answer", "waive_objective")

    # The question has to be OLD, not just present: the hard ceiling drops by ascending seq, so a
    # question that is still the newest row survives it for free and the test would pass against
    # the unfixed code. These push it back down the feed — and they are a PRESERVED kind, because
    # the soft cap already spares `question` and a cap the soft trim can satisfy never reaches
    # the hard one at all.
    for i in range(5):
        missions.append_event(m["id"], "completion", text=f"later {i}")
    monkeypatch.setattr(missions, "MISSION_EVENTS_MAX", 3)
    monkeypatch.setattr(missions, "MISSION_EVENTS_HARD_MAX", 3)
    missions.append_event(m["id"], "completion", text="the row that trips the ceiling")

    still = missions.open_question_row(m["id"])
    assert still is not None, "the hard cap deleted the question that is holding the objective"
    assert still["seq"] == q["seq"]
    assert len(still["options"]) == 2, "the options went with the row the JOIN needed"

    # ...and it is still answerable, which is the property that actually matters.
    out = missions.answer_question(m["id"], q["seq"], option_index=0)
    assert out["action"] == "note_answer"
    assert missions.open_question_row(m["id"]) is None


# ---- the label is not the authorization UI (#900 review 2, finding 1) --------------------


def test_EVERY_OPTION_CARRIES_THE_SERVERS_OWN_WORDS_for_what_it_does():
    """The model writes the label AND picks the action, and the operator sees only the label.

    "The label is never executed" was the property the closed set was built for, and it is not
    the whole threat: a label reading "Keep working; leave this required" over a hidden
    `waive_objective` obtains a human confirmation under false pretences. A button that lies
    still gets pressed.

    So every option carries the SERVER's sentence about the action, keyed on the action the
    server will actually run — and it cannot be model-authored, because the model has no field
    for it.
    """
    question, options, _ = mq.options_from_reply(
        {
            "question": "which?",
            "options": [
                # The exact deception: a reassuring label over the settling action.
                {"label": "Keep working; leave this required", "action_index": 1},
                {"label": "Something else", "action_index": 0},
            ],
        },
    )
    waive = next(o for o in options if o["action"] == "waive_objective")
    assert waive["consequence"] == mq.CONSEQUENCE["waive_objective"]
    assert "NOT REQUIRED" in waive["consequence"]
    assert waive["settling"] is True
    # …and the harmless one is not marked settling, so the card does not ask twice for nothing.
    note = next(o for o in options if o["action"] == "note_answer")
    assert note["settling"] is False


def test_a_MODEL_SUPPLIED_consequence_is_refused_like_any_other_unknown_field():
    """The allowlist is what stops a model writing its own reassurance beside its own label."""
    _q, options, dropped = mq.options_from_reply(
        {
            "question": "which?",
            "options": [
                {
                    "label": "harmless",
                    "action_index": 1,
                    "consequence": "Nothing happens, honestly.",
                },
                {"label": "a", "action_index": 0},
                {"label": "b", "action_index": 2},
            ],
        },
    )
    assert dropped >= 1
    assert all(o["consequence"] == mq.CONSEQUENCE[o["action"]] for o in options)


def test_the_CONSEQUENCE_TABLE_covers_the_closed_set_exactly():
    """An action with no operator-facing sentence would render a bare model label — which is the
    defect. Asserted here as well as at import, because the import assert is easy to delete."""
    assert set(mq.CONSEQUENCE) == mq.ACTION_NAMES
    assert mq.SETTLING <= mq.ACTION_NAMES
    assert "note_answer" not in mq.SETTLING


def test_the_STORE_and_the_QUESTION_MODULE_agree_on_the_closed_set():
    """The store validates the action before it mutates, and cannot import the question module —
    so the set is spelled twice and pinned equal here rather than left to drift."""
    assert missions.ANSWER_ACTIONS == mq.ACTION_NAMES


def test_an_action_OUTSIDE_the_closed_set_is_refused_BEFORE_the_question_is_consumed(store):
    """A validation that runs after the commit is a validation of the past: the route's check ran
    once the hold was released and the effect had run, so a malformed stored option consumed the
    question AND returned a 422 — the operator left with neither."""
    m = _mission()
    q = missions.open_question(
        m["id"],
        "pr_open",
        "which?",
        [{"label": "a", "action": "rm_minus_rf"}, {"label": "b", "action": "note_answer"}],
    )
    with pytest.raises(missions.MissionError) as e:
        missions.answer_question(m["id"], q["seq"], option_index=0)
    assert e.value.status == 422
    # …and the question is STILL THERE to answer properly.
    assert missions.open_question_row(m["id"]) is not None
    out = missions.answer_question(m["id"], q["seq"], option_index=1)
    assert out["action"] == "note_answer"


def test_OPENING_A_QUESTION_TAKES_THE_WRITE_FENCE(store, monkeypatch):
    """A question hold WITHDRAWS AUTHORITY, so it commits under the same lock the write fence holds.

    Every other authority withdrawal — detach, a terminal transition, an objective edit — takes
    `session_input.sessions_transaction()` before it commits, for one reason: without it the
    withdrawal lands between the fence's final comparison and `os.write()`, and the supervisor
    types into a session the console is about to show a question about. Reproduced on the previous
    head: the question committed after the final fingerprint callback returned and the write still
    settled `delivered` (#900 review 2, finding 2).

    Asserted structurally, on the ORDER — the lock is taken and the commit happens inside it —
    because a behavioural probe would need a window the fixed code does not have. Red against an
    `open_question` called outside the fence.
    """
    import asyncio

    from agent_sessions import session_input

    m = _mission()
    missions.adopt(m["id"], CLAUDE_A)
    order: list[str] = []

    real_tx = session_input.sessions_transaction

    class _Tx:
        def __init__(self, keys):
            self.keys = keys

        def __enter__(self):
            order.append(f"fence-enter:{sorted(self.keys)}")
            return self

        def __exit__(self, *a):
            order.append("fence-exit")
            return False

    real_open = missions.open_question

    def _watched(*a, **kw):
        order.append("open_question")
        return real_open(*a, **kw)

    monkeypatch.setattr(session_input, "sessions_transaction", _Tx)
    monkeypatch.setattr(missions, "open_question", _watched)
    try:
        asyncio.run(
            mq._fenced_open(
                m["id"],
                "pr_open",
                "which?",
                [
                    {"label": "a", "action": "note_answer"},
                    {"label": "b", "action": "note_answer"},
                ],
                missions.objective_incarnation(m["id"], "pr_open"),
                None,  # any episode
                "",  # any content
                None,  # the default store path
            )
        )
    finally:
        session_input.sessions_transaction = real_tx

    assert order[0].startswith("fence-enter:"), order
    assert CLAUDE_A in order[0], "the fence did not cover the session the mission holds"
    assert order[1] == "open_question", "the question committed outside the fence"
    assert order[2] == "fence-exit"


def test_a_BUSY_FENCE_does_not_abort_the_pass_and_lose_the_episodes_only_ask(store, monkeypatch):
    """`ask()` promises never to raise, and it has to mean it (#900 review 3, finding 1).

    The question opens under the write fence, and the fence can be held — `AuthorityFenceBusy` is
    not a `MissionError`, so it escaped, aborted the supervisor pass, and took this episode's only
    chance to ask with it: the ask happens once, after `escalate_once` wins, and a later pass in
    the same episode does not get another. A transient lock is the wrong reason to lose that
    permanently.

    Red against catching only `MissionError`.
    """
    import asyncio

    import agent_sessions.review as review_mod
    from agent_sessions import session_input

    m = _mission()
    reply = {
        "question": "which one?",
        "options": [
            {"label": "the first", "action_index": 0},
            {"label": "the second", "action_index": 1},
        ],
    }

    async def _reply(messages, **kw):
        return reply

    def _busy(keys):
        raise session_input.AuthorityFenceBusy("the fence is held")

    monkeypatch.setattr(session_input, "sessions_transaction", _busy)
    orig = review_mod.complete_json
    review_mod.complete_json = _reply
    try:
        out = asyncio.run(mq.ask(m["id"], "pr_open", context="waiting"))
    finally:
        review_mod.complete_json = orig

    assert out is None
    assert missions.open_question_row(m["id"]) is None
    kinds = [e["kind"] for e in missions.get_mission(m["id"])["events"]]
    # …and NOT silent: an absent question and a broken feature must not look the same.
    assert "error" in kinds
    assert "question" not in kinds


def test_CLOSE_MISSION_takes_the_same_gate_the_supervisors_own_proposal_takes(store):
    """Appending the event alone was a claim the lifecycle had not accepted (#900 review 3, 2).

    A mission with an unmet required objective answered `close_mission` and got a completion
    event on its timeline while its state stayed `running` and the gate stayed unmet — a document
    about a decision nobody made.

    Red against an `_apply_answer_con` that only appends.
    """
    m = _mission()  # two GATING objectives, both pending
    q = _q(m["id"], "pr_open", "close_mission", "note_answer")
    out = missions.answer_question(m["id"], q["seq"], option_index=0)

    assert out["applied"] != "proposed completion"
    assert "still unmet" in out["applied"]
    row = missions.get_mission(m["id"])
    assert row["state"] == "running"
    assert not [e for e in row["events"] if e["kind"] == "completion"]
    # …and the ANSWER is still recorded: the operator said what they think, and the effect
    # honestly did not happen.
    assert any(e["kind"] == "answer" for e in row["events"])


def test_CLOSE_MISSION_proposes_when_every_gate_IS_met(store):
    """The mirror — the gate is a gate, not a refusal to ever act."""
    m = _mission()
    missions.patch_objectives(m["id"], [{"op": "waive", "key": "pr_open"}])
    missions.patch_objectives(m["id"], [{"op": "waive", "key": "checks"}])
    # The question is about an UNSETTLED objective — a settled one gets no question at all now
    # (#900 review 5, finding 3). Non-gating, so every GATE is still met and `close_mission` is
    # legitimately proposable, which is what this test is about.
    missions.patch_objectives(
        m["id"], [{"op": "add", "key": "tidy", "title": "Tidy up", "gate": False}]
    )

    q = _q(m["id"], "tidy", "close_mission", "note_answer")
    out = missions.answer_question(m["id"], q["seq"], option_index=0)

    assert out["applied"] == "proposed completion"
    row = missions.get_mission(m["id"])
    assert row["state"] == "review", "the proposal was posted without the state it describes"
    assert [e for e in row["events"] if e["kind"] == "completion"]


def test_the_MODEL_IS_TOLD_WHAT_THE_OBJECTIVE_IS_not_just_its_key(store, monkeypatch):
    """A key is an internal handle (#900 review 3, finding 4).

    `custom_7` says nothing about the work, so a model given only that cannot form a concrete
    question about it — and the prompt already claimed it would receive the checklist. The title
    comes from the SAME snapshot as the incarnation, so the question is about the objective whose
    identity is compared when it opens.

    Red against a prompt carrying only the key.
    """
    import asyncio

    import agent_sessions.review as review_mod

    m = missions.create_mission("ship it", cwd="/repo")
    missions.set_state(m["id"], "draft", "planned")
    missions.set_state(m["id"], "planned", "dispatching")
    missions.set_state(m["id"], "dispatching", "running")
    missions.patch_objectives(
        m["id"],
        [
            {
                "op": "add",
                "key": "custom_7",
                "title": "Verify the release artifact signature",
                "gate": True,
            }
        ],
    )

    seen: list[str] = []

    async def _capture(messages, **kw):
        seen.append("\n".join(str(msg.get("content") or "") for msg in messages))
        return {
            "question": "which artifact?",
            "options": [
                {"label": "a", "action_index": 0},
                {"label": "b", "action_index": 1},
            ],
        }

    orig = review_mod.complete_json
    review_mod.complete_json = _capture
    try:
        asyncio.run(mq.ask(m["id"], "custom_7", context="nothing yet"))
    finally:
        review_mod.complete_json = orig

    assert seen, "the model was never called"
    assert "Verify the release artifact signature" in seen[0]
    # …and the fact that it GATES, because that is what makes the question consequential.
    assert "required for done" in seen[0]


# ==============================================================================================
# #900 review round 4
# ==============================================================================================


def test_a_question_cannot_OPEN_on_a_mission_that_ended_during_the_model_call(store):
    """#900 review 4, finding 2. Producing a question takes a model call, and a mission can be
    closed, abandoned or carried into review inside it.

    The objective's incarnation cannot see that — the row is still there, unchanged, on a mission
    that has finished — so the question opened and `needs_you` then flagged work that is over,
    asking the operator to decide something about a mission nobody can act on any more.

    Red against an opening CAS that compares only the objective.
    """
    m = _mission()
    missions.set_state(m["id"], "running", "failed", outcome="failed")
    with pytest.raises(missions.MissionError) as e:
        _open(m["id"])
    assert "nothing to ask about" in str(e.value)
    assert missions.open_question_row(m["id"]) is None


def test_a_question_cannot_open_on_a_mission_in_REVIEW_either(store):
    """`review` is a mission proposing that it is done. A question there asks the operator to
    decide about work that is already waiting on a different decision from them."""
    m = _mission()
    missions.set_state(m["id"], "running", "review")
    with pytest.raises(missions.MissionError):
        _open(m["id"])


def test_close_mission_is_NOT_OFFERED_while_a_required_objective_is_unmet(store):
    """#900 review 4, finding 4, at the only place that can fix it: the list the model chooses
    from.

    `_apply_answer_con` re-reads the completion gates inside the answer's transaction, so with any
    gate unmet `close_mission` returns `not proposed` — while the card told the operator, in the
    server's own words, that it "proposes that the mission is finished". They confirmed a settling
    action twice and nothing happened. And it was not an edge case: the supervisor returns through
    `likely_done` as soon as every gate is met, BEFORE anything escalates, so every question it
    generates occurs with a gate unmet and the option was dead everywhere it could appear.

    Red against a list that is the same whatever the mission's gates say.
    """
    offered = mq.offered_actions(settled=False)
    names = [n for n, _ in offered]
    assert "close_mission" not in names
    assert "note_answer" in names and "waive_objective" in names
    # …and it IS offered where it would do something, so the action is gated rather than deleted.
    assert "close_mission" in [n for n, _ in mq.offered_actions(settled=True)]


def test_an_INDEX_resolves_against_the_list_the_model_was_actually_SHOWN(store):
    """The index discipline, one level up: a shortened list and a full one give the same index
    different meanings, so resolving against `ACTIONS` while showing a subset would hand the
    operator an action nobody offered — the "an index is not an identity" failure with the server
    picking the identity."""
    offered = mq.offered_actions(settled=False)
    rendered = mq.render_actions(offered)
    # The rendering and the resolution agree, whatever the list is.
    assert "close_mission" not in rendered
    _, options, _ = mq.options_from_reply(
        {
            "question": "which one?",
            "options": [
                {"label": "a", "action_index": 0},
                {"label": "b", "action_index": len(offered) - 1},
            ],
        },
        offered,
    )
    assert [o["action"] for o in options] == [offered[0][0], offered[-1][0]]
    # An index that only exists in the FULL list is out of range for this one, and dropped.
    _, few, dropped = mq.options_from_reply(
        {
            "question": "which one?",
            "options": [
                {"label": "a", "action_index": 0},
                {"label": "b", "action_index": len(mq.ACTIONS) - 1},
            ],
        },
        offered,
    )
    assert dropped >= 1


# ==============================================================================================
# #900 review round 5
# ==============================================================================================


def test_the_FENCE_FAILS_CLOSED_when_the_sessions_cannot_be_enumerated(store, monkeypatch):
    """#900 review 5, finding 1. A fence that cannot list what it is meant to lock does not
    become a fence by locking nothing.

    The earlier version swallowed the failure into `keys = []` on the reasoning that a fence
    should not block a question — the same reasoning `_authority_fence` already rejected. A
    session missing from the list is one a nudge can be writing into while this transaction
    commits, and the objective then visibly waits for an answer the supervisor is talking over.

    Red against a fence that converts an enumeration failure to an empty lock set.
    """
    m = _mission()

    def boom(*a, **k):
        raise OSError("the roster could not be read")

    monkeypatch.setattr(missions, "active_session_keys", boom)
    with pytest.raises(missions.MissionError) as e:
        asyncio.run(
            mq._fenced_open(
                m["id"],
                "pr_open",
                "which?",
                [
                    {"label": "a", "action": "note_answer"},
                    {"label": "b", "action": "note_answer"},
                ],
                missions.objective_incarnation(m["id"], "pr_open"),
                None,  # any episode
                "",  # any content
                None,  # the default store path
            )
        )
    assert "could not be read" in str(e.value)
    assert missions.open_question_row(m["id"]) is None


def test_a_session_ADOPTED_during_the_model_call_refuses_the_commit(store, monkeypatch):
    """The other half: the set is enumerated before the lock is taken, so a session adopted in
    between is one this transaction is not holding. Compared inside the lock and refused."""
    m = _mission()
    calls = {"n": 0}
    real = missions.active_session_keys

    def growing(mission_id, **kw):
        calls["n"] += 1
        # The FIRST read (building the lock set) sees nothing; the read inside the lock sees a
        # session that has since been adopted.
        return [] if calls["n"] == 1 else ["claude:11111111-1111-1111-1111-111111111111"]

    monkeypatch.setattr(missions, "active_session_keys", growing)
    with pytest.raises(missions.MissionError) as e:
        asyncio.run(
            mq._fenced_open(
                m["id"],
                "pr_open",
                "which?",
                [
                    {"label": "a", "action": "note_answer"},
                    {"label": "b", "action": "note_answer"},
                ],
                missions.objective_incarnation(m["id"], "pr_open"),
                None,  # any episode
                "",  # any content
                None,  # the default store path
            )
        )
    assert "sessions changed" in str(e.value)
    monkeypatch.setattr(missions, "active_session_keys", real)
    assert missions.open_question_row(m["id"]) is None


def test_a_TERMINAL_transition_clears_the_question_and_refuses_a_late_answer(store):
    """#900 review 5, finding 2. A terminal state releases the roster, and it has to release the
    ATTENTION for the same reason.

    Reproduced on the previous head: open a question, move the mission to `failed`, answer
    `waive_objective` — the closed mission still reported `needs_you: ['question']` and its
    objective became `waived`. Two defects in one: a hold that outlived the mission, and an
    answer that ran an action against work that was over.

    Red against a `set_state` that releases sessions and leaves holds, and an `answer_question`
    whose only comparand is the hold.
    """
    m = _mission()
    q = _q(m["id"], "pr_open", "waive_objective", "note_answer")
    assert missions.open_question_row(m["id"]) is not None

    missions.set_state(m["id"], "running", "failed", outcome="failed")

    # 1. The hold is gone, so the mission does not ask for a decision it cannot act on.
    assert missions.open_question_row(m["id"]) is None
    why = missions.derive_needs_you([m["id"]])[m["id"]]
    assert "question" not in (why.get("why") or [])

    # 2. …and the answer is refused rather than silently applied.
    with pytest.raises(missions.MissionError):
        missions.answer_question(m["id"], q["seq"], option_index=0)
    obj = next(o for o in missions.objectives(m["id"]) if o["key"] == "pr_open")
    assert obj["state"] != "waived", "a closed mission's objective was changed by a late answer"


def test_an_objective_SETTLED_during_the_model_call_does_not_get_the_question(store):
    """#900 review 5, finding 3. The incarnation is unchanged — it is the same row — so the CAS
    passed and the operator was asked to decide about work that was already done."""
    m = _mission()
    inc = missions.objective_incarnation(m["id"], "pr_open")
    missions.patch_objectives(m["id"], [{"op": "waive", "key": "pr_open"}])
    with pytest.raises(missions.MissionError) as e:
        missions.open_question(
            m["id"],
            "pr_open",
            "which?",
            [
                {"label": "a", "action": "note_answer"},
                {"label": "b", "action": "note_answer"},
            ],
            expect_incarnation=inc,
        )
    assert "settled" in str(e.value)


def test_an_EPISODE_that_advanced_during_the_model_call_refuses_the_question(store):
    """The same family. An episode advances when the objective is stood down or re-opened, so a
    question written about one and landing in the next is about a situation already closed out."""
    m = _mission()
    inc = missions.objective_incarnation(m["id"], "pr_open")
    episode, _, _ = missions.objective_hold(m["id"], "pr_open")
    missions.bump_episode(m["id"], "pr_open")
    with pytest.raises(missions.MissionError) as e:
        missions.open_question(
            m["id"],
            "pr_open",
            "which?",
            [
                {"label": "a", "action": "note_answer"},
                {"label": "b", "action": "note_answer"},
            ],
            expect_incarnation=inc,
            expect_episode=episode,
        )
    assert "moved on" in str(e.value)


def test_a_gate_whose_OBSERVATION_went_red_neither_offers_nor_applies_close_mission(store):
    """#900 review 5, finding 4. `state == 'met'` is the stored settlement; the latest look is a
    different fact, and `assess()` / `propose_completion()` already read both.

    Counting only the stored state made two answers to one question: a gate stored `met` whose
    check had since gone red exposed `close_mission`, and answering it carried the mission into
    review while the supervisor correctly said it was not done.

    Red against a count that stops at the stored state.
    """
    m = _mission()
    missions.patch_objectives(m["id"], [{"op": "waive", "key": "checks"}])
    # A PROBED gate, so an observation can settle it and then contradict it.
    missions.patch_objectives(m["id"], [{"op": "drop", "key": "pr_open"}])
    missions.patch_objectives(
        m["id"],
        [
            {
                "op": "add",
                "key": "pr_open",
                "title": "A PR is open",
                "probe": "forge_pr",
                "gate": True,
            }
        ],
    )
    missions.observe_objective(m["id"], "pr_open", observed=True, value=True, detail="a PR is open")
    rows = missions.objectives(m["id"])
    assert missions.unmet_gate_count(rows) == 0, "the fixture did not reach the all-met state"

    # THE CHECK GOES RED. The settlement is deliberately not moved backwards — that is #897's
    # rule — but the observation no longer supports it.
    missions.observe_objective(
        m["id"], "pr_open", observed=True, value=False, detail="the PR was closed"
    )
    rows = missions.objectives(m["id"])
    assert missions.unmet_gate_count(rows) == 1

    # 1. It is not OFFERED …
    assert "close_mission" not in [n for n, _ in mq.offered_actions_for(rows)]
    # 2. … and if one were answered anyway, it does not propose.
    # Asked about an UNSETTLED objective — a settled one gets no question at all (finding 3).
    missions.patch_objectives(
        m["id"], [{"op": "add", "key": "tidy", "title": "Tidy up", "gate": False}]
    )
    q = _q(m["id"], "tidy", "close_mission", "note_answer")
    out = missions.answer_question(m["id"], q["seq"], option_index=0)
    assert "not proposed" in out["applied"]
    assert missions.get_mission(m["id"])["state"] == "running"


# ==============================================================================================
# #900 review round 6
# ==============================================================================================


def test_ADOPTION_and_a_question_share_ONE_fence(store):
    """#900 review 6, finding 1. Locking the sessions a mission already holds cannot order the
    commit against an ADOPTION: the session being adopted is by definition not in that set, so a
    check in one lock domain was followed by a write in another, and the newly adopted session's
    already-authorized nudge could still write bytes while the objective became visibly held.

    Asserted on the LOCK the two take, because that is the mechanism — a timing test would pass
    against the unfixed code most of the time, which is the worst kind of green.
    """
    from agent_sessions import session_input

    m = _mission()
    key = mq.roster_key(m["id"])
    assert key.startswith("mission-roster:")

    taken: list[list[str]] = []
    real = session_input.sessions_transaction

    import contextlib as _c

    @_c.contextmanager
    def watched(keys):
        taken.append(list(keys))
        with real(keys):
            yield

    orig = session_input.sessions_transaction
    session_input.sessions_transaction = watched
    try:
        asyncio.run(
            mq._fenced_open(
                m["id"],
                "pr_open",
                "which?",
                [
                    {"label": "a", "action": "note_answer"},
                    {"label": "b", "action": "note_answer"},
                ],
                missions.objective_incarnation(m["id"], "pr_open"),
                None,
                "",
                None,
            )
        )
    finally:
        session_input.sessions_transaction = orig

    assert (
        taken and key in taken[0]
    ), "the question did not take the roster's own lock, so an adoption can still interleave"


def test_a_RETITLED_objective_does_not_receive_the_old_question(store):
    """#900 review 6, finding 4. A retitle changes neither the incarnation, nor the episode, nor
    the state — so a question written about "a PR is open" opened unchanged against an objective
    that now reads as unrelated work, and a settling answer then acted on the new wording.

    Red against a CAS that compares only identity and settlement.
    """
    m = _mission()
    target = next(o for o in missions.objectives(m["id"]) if o["key"] == "pr_open")
    inc = str(target["incarnation"])
    content = missions.objective_content(target)

    missions.patch_objectives(
        m["id"],
        [{"op": "retitle", "key": "pr_open", "title": "Write the release notes"}],
    )
    with pytest.raises(missions.MissionError) as e:
        missions.open_question(
            m["id"],
            "pr_open",
            "which of the two open PRs is this mission's?",
            [
                {"label": "a", "action": "note_answer"},
                {"label": "b", "action": "note_answer"},
            ],
            expect_incarnation=inc,
            expect_content=content,
        )
    assert "rewritten" in str(e.value)
    assert missions.open_question_row(m["id"]) is None
    # The IDENTITY is unchanged, which is the whole point: nothing else could have caught it.
    after = next(o for o in missions.objectives(m["id"]) if o["key"] == "pr_open")
    assert str(after["incarnation"]) == inc


def test_entering_REVIEW_clears_a_hold_that_could_no_longer_be_answered(store):
    """#900 review 6, finding 3. Holds were cleared only for TERMINAL states, so `review` stranded
    them: `needs_you` kept reporting a question while `_question_answerable` rejected every
    answer — flagged for a decision the server refuses to take."""
    m = _mission()
    missions.patch_objectives(m["id"], [{"op": "waive", "key": "checks"}])
    _q(m["id"], "pr_open", "note_answer", "waive_objective")
    assert missions.open_question_row(m["id"]) is not None

    missions.set_state(m["id"], "running", "review")
    assert missions.open_question_row(m["id"]) is None
    why = missions.derive_needs_you([m["id"]])[m["id"]]
    assert "question" not in (why.get("why") or [])


def test_PROPOSE_COMPLETION_clears_the_hold_it_would_otherwise_strand(store):
    """The same rule on the other door into `review` — the supervisor's own proposal, which does
    not go through `set_state`."""
    m = _mission()
    missions.patch_objectives(m["id"], [{"op": "waive", "key": "pr_open"}])
    missions.patch_objectives(m["id"], [{"op": "waive", "key": "checks"}])
    missions.patch_objectives(
        m["id"], [{"op": "add", "key": "tidy", "title": "Tidy up", "gate": False}]
    )
    _q(m["id"], "tidy", "note_answer", "waive_objective")
    assert missions.open_question_row(m["id"]) is not None

    assert missions.propose_completion(
        m["id"], from_state="running", render=lambda rows: ("every gate is met", {})
    )
    assert missions.get_mission(m["id"])["state"] == "review"
    assert missions.open_question_row(m["id"]) is None


# ==============================================================================================
# #900 review round 7
# ==============================================================================================


def test_a_RETITLE_DURING_THE_MODEL_CALL_is_caught_through_ask(store, monkeypatch):
    """#900 review 7, finding 12. The retitle rule was asserted by calling `open_question`
    directly with a comparand the TEST computed — so dropping `expect_content` from `ask()`, which
    is the only production caller, left the suite green.

    The same interleaving through the real producer: the retitle lands inside the model call, and
    `ask` has to have captured the content from the snapshot it built the prompt from.
    """
    from agent_sessions import review as review_mod

    m = _mission()
    reply = {
        "question": "which PR did you mean?",
        "options": [
            {"label": "the first one", "action_index": 0},
            {"label": "the second one", "action_index": 1},
        ],
    }
    assert len(mq.options_from_reply(reply)[1]) == 2

    async def _retitle_then_reply(messages, **kw):
        # The operator rewords the objective while the model is thinking. Neither the incarnation
        # nor the episode nor the state changes — a retitle is invisible to every other comparand.
        missions.patch_objectives(
            m["id"], [{"op": "retitle", "key": "pr_open", "title": "Write the release notes"}]
        )
        return reply

    monkeypatch.setattr(review_mod, "complete_json", _retitle_then_reply)
    out = asyncio.run(mq.ask(m["id"], "pr_open", context="waiting"))

    assert out is None
    assert missions.open_question_row(m["id"]) is None
    kinds = [e["kind"] for e in missions.get_mission(m["id"])["events"]]
    assert "question" not in kinds
    # …and not silent, like every other refusal in this module.
    assert "error" in kinds


def test_ANSWERING_close_mission_clears_the_hold_it_would_otherwise_strand(store):
    """#900 review 7, finding 12, on the THIRD door into `review`.

    `review` is unanswerable, so a hold surviving into it flags the mission for a decision every
    later answer is refused. Two of the three entrances were asserted — `set_state` and
    `propose_completion` — and the answer path's own `close_mission` was not, so removing its
    cleanup left the suite green.
    """
    m = _mission()
    missions.patch_objectives(m["id"], [{"op": "waive", "key": "pr_open"}])
    missions.patch_objectives(m["id"], [{"op": "waive", "key": "checks"}])
    missions.patch_objectives(
        m["id"], [{"op": "add", "key": "tidy", "title": "Tidy up", "gate": False}]
    )
    # A SECOND objective carries its own hold, and that is the one that used to strand: answering
    # releases the hold it ANSWERS (the `question_seq=?` predicate), so a test with one question
    # open cannot see this at all.
    missions.patch_objectives(
        m["id"], [{"op": "add", "key": "notes", "title": "Write the notes", "gate": False}]
    )
    _q(m["id"], "notes", "note_answer", "waive_objective")
    q = _q(m["id"], "tidy", "close_mission", "note_answer")
    out = missions.answer_question(m["id"], int(q["seq"]), option_index=0)

    assert out["applied"] == "proposed completion"
    assert out["applied_ok"] is True
    assert missions.get_mission(m["id"])["state"] == "review"
    # THE OTHER OBJECTIVE'S HOLD went with the transition — `review` is unanswerable, so a hold
    # surviving into it flags the mission for a decision every later answer is refused.
    assert missions.open_question_row(m["id"]) is None
    assert "question" not in (missions.derive_needs_you([m["id"]])[m["id"]]["why"] or [])


def test_an_ABANDONING_ARCHIVE_clears_the_hold_it_would_otherwise_strand(store):
    """#900 review 7, finding 3. `begin_archive(abandon=True)` writes `state='abandoned'` itself
    rather than going through the transition helper, so it inherited none of that path's hold
    cleanup: the mission ended up `abandoned` with `question_open`, `needs_you: ["question"]`, and
    every answer refused by `_question_answerable`.

    Red against a direct state write with no hold cleanup.
    """
    m = _mission()
    q = _q(m["id"], "pr_open", "note_answer", "waive_objective")
    assert missions.open_question_row(m["id"]) is not None

    missions.begin_archive(m["id"], abandon=True)

    assert missions.get_mission(m["id"])["state"] == "abandoned"
    assert missions.open_question_row(m["id"]) is None
    assert "question" not in (missions.derive_needs_you([m["id"]])[m["id"]]["why"] or [])
    # …and the answer that is now impossible says so, rather than being silently accepted.
    with pytest.raises(missions.MissionError):
        missions.answer_question(m["id"], int(q["seq"]), option_index=0)


def test_a_REFUSED_EFFECT_says_so_as_a_FLAG_not_as_a_sentence(store):
    """#900 review 7, finding 9. The client has to tell "waived" from "not waived — the objective
    was already met", and it was left to do that by reading English: every refusal happens to
    begin with "not ", and a phrasing change would silently turn a refusal into a success on
    screen.

    Red against a store that returns only the prose.
    """
    m = _mission()
    q = _q(m["id"], "pr_open", "waive_objective", "note_answer")
    # …and the objective is MET while the question sits on screen, so waiving it is refused: a
    # valid answer whose effect does not happen. Through the PROBE path, which is the only one
    # that may write `met` from evidence (#891).
    missions.observe_objective(m["id"], "pr_open", observed=True, value=True, detail="a PR is open")
    out = missions.answer_question(m["id"], int(q["seq"]), option_index=0)
    assert out["applied_ok"] is False
    assert out["applied"].startswith("not waived")
    # …and the answer itself is still recorded: the operator said what they think.
    assert [e for e in missions.get_mission(m["id"])["events"] if e["kind"] == "answer"]

    # …while an effect that DID happen says so with the same flag.
    m2 = _mission()
    q2 = _q(m2["id"], "pr_open", "waive_objective", "note_answer")
    out2 = missions.answer_question(m2["id"], int(q2["seq"]), option_index=0)
    assert out2["applied_ok"] is True and out2["applied"] == "waived"


def test_the_ATTENTION_FLAG_and_the_QUESTION_come_from_ONE_snapshot(store):
    """#900 review 7, finding 4. They were two reads on two connections, so an answer or an
    opening between them returned a page that contradicted itself.

    Asserted on the MECHANISM — one snapshot — because a timing test passes against the unfixed
    code most of the time, which is the worst kind of green: the store is patched so the question
    read observes a DIFFERENT state from the flag read unless they share a transaction.
    """
    m = _mission()
    _q(m["id"], "pr_open", "note_answer", "waive_objective")

    snap = missions.get_mission(m["id"], attention=True)
    assert snap["needs_you"] is True
    assert "question" in snap["needs_you_why"]
    assert snap["question"] is not None
    assert snap["question"]["objective"] == "pr_open"
    # …AND THE TIMELINE AGREES WITH BOTH (#900 review 8, finding 1). The flag and the question
    # agreeing with each other was the property the previous version had, and it was not enough:
    # they came from a different transaction than the events beside them, so the console could be
    # handed a question whose own event it had not been sent.
    assert snap["question"]["seq"] in {e["seq"] for e in snap["events"]}

    # …and once answered, all three move together.
    missions.answer_question(m["id"], int(snap["question"]["seq"]), text="the first one")
    after = missions.get_mission(m["id"], attention=True)
    assert after["question"] is None
    assert "question" not in after["needs_you_why"]


def test_a_reply_that_asks_TWO_QUESTIONS_is_dropped(store):
    """#900 review 8, finding 4. #892's contract is "one sentence, and 2-4 concrete options", and
    only the length half was enforced.

    A reply asking *"Which release is this? Also, should I waive the gate?"* was accepted with one
    option set covering two ambiguities — so whichever the operator answers, the other is answered
    too, by an option that was never about it. That is a guess wearing a decision's clothes, which
    is the thing this phase exists to replace.

    Dropped rather than truncated to the first sentence: keeping half of what the model asked and
    pairing it with options written for both halves would be the server inventing the question.
    """
    two = {
        "question": "Which release is this? Also, should I waive the gate?",
        "options": [
            {"label": "the first one", "action_index": 0},
            {"label": "the second one", "action_index": 1},
        ],
    }
    q, opts, dropped = mq.options_from_reply(two)
    assert q == "" and opts == [] and dropped >= 1

    # …and the SAME options with one question are accepted, so this refuses the second question
    # rather than the reply.
    one = {**two, "question": "Which of the two open PRs is this mission's?"}
    q, opts, dropped = mq.options_from_reply(one)
    assert q == "Which of the two open PRs is this mission's?"
    assert len(opts) == 2 and dropped == 0


def test_the_PRODUCTION_PARSER_drops_a_second_question_glued_to_a_full_stop(store):
    """#900 review 12, at the boundary the route actually calls.

    `is_one_sentence` is the rule; `options_from_reply` is the door, and a rule that is right in
    isolation proves nothing about a door that might not consult it. These three replies each
    carry two questions with no space after the full stop — the shape the previous exemption
    (`.` glued to any alphanumeric) read as one sentence — and each arrives with a plausible
    two-option set, so accepting one would let a single choice settle both halves.

    Red against exempting every dot that is followed by an alphanumeric.
    """
    options = [
        {"label": "the stable branch", "action_index": 0},
        {"label": "wait for the next cut", "action_index": 1},
    ]
    for question in (
        "Which release.Then should I waive?",
        "Use stable.Then deploy?",
        "No.Then continue?",
        "Ship A.Then wait?",
    ):
        q, opts, dropped = mq.options_from_reply({"question": question, "options": options})
        assert (q, opts) == ("", []), question
        assert dropped >= 1, question

    # …and the version number the exemption exists for still gets through the same door, so this
    # refuses the second question rather than refusing dots.
    q, opts, dropped = mq.options_from_reply(
        {"question": "Is v1.2.3 the release you meant?", "options": options}
    )
    assert q == "Is v1.2.3 the release you meant?"
    assert len(opts) == 2 and dropped == 0


def test_ONE_SENTENCE_is_ONE_TERMINATOR_at_the_END(store):
    """#900 reviews 8-10. Two earlier rules were context-free guesses dressed as grammar, and each
    failed OPEN: requiring a capital after the terminator let a lowercase second question through,
    and exempting abbreviations let `no.` and `vs.` through — because no list can tell an
    abbreviation from a sentence that happens to end in one.

    So the rule stops trying to parse English: a terminator counts when the text ends there or
    continues with whitespace, and one counting terminator at the end is one sentence.

    Red against either of the earlier rules.
    """
    for text in (
        "Which of the two open PRs is this mission's?",
        # A DOT FOLLOWED BY A DIGIT IS NOT A SENTENCE END, which is what keeps versions and
        # addresses — the things these questions actually carry — from being refused.
        "Is v1.2 the release you meant?",
        "Should I target 127.0.0.1 or the LAN address?",
        "Is 3.5 GB of context enough?",
        "Which PR is this mission's — #12 or #14?",
        "Deploy now!",
        # No terminator at all is one (unpunctuated) question.
        "Which branch",
    ):
        assert mq.is_one_sentence(text), text
    for text in (
        "The build is red. Should I retry?",
        "Which release is this? Also, should I waive the gate?",
        # …and LOWERCASE does not rescue a second question (review 9).
        "Which release is this? or should I waive the gate?",
        "Use the stable branch. then should I continue?",
        "Deploy now! or wait?",
        # …nor does an ABBREVIATION exemption, which is what let these two through (review 10).
        "Should I say no. then deploy?",
        "Wait vs. should I continue?",
        # …nor does typography: a Unicode closing quote between the terminator and the next
        # sentence is still a sentence boundary.
        "Which release?” Then should I waive the gate?",
        "これでいい？ それとも待つ？",
        # …nor does a MISSING SPACE, nor an unfamiliar closer (#900 review 11). An allowlist of
        # closers followed by whitespace answered "one sentence" to both of these: the first has
        # no space at all, and the second closes with a bracket the list had never heard of.
        # Punctuation is open-ended, so the rule fails closed instead — a terminator ends
        # something unless it is demonstrably inside a token.
        "Which release?Also, should I waive?",
        "「どのリリース？」 それとも待つ？」",
        # …and A MISSING SPACE AFTER A FULL STOP is the same shape (#900 review 12). Exempting
        # every `.` glued to an alphanumeric kept `v1.2` at the price of reading these as one
        # sentence — nothing about the characters around a dot separates an abbreviation from a
        # boundary, which is the lesson `vs.` and `Dr.` taught one round earlier. The exemption is
        # now DIGIT-to-DIGIT only.
        "Which release.Then should I waive?",
        "Use stable.Then deploy?",
        "No.Then continue?",
        # …including a single-letter token before the dot, which any "abbreviations are short"
        # heuristic would have let through.
        "Ship A.Then wait?",
        # THE ACCEPTED COST, asserted rather than left implicit: an abbreviation followed by a
        # space reads as a sentence end, so a question carrying `Dr. ` is dropped. Not silent —
        # an `error` notice lands, the escalation still flags the mission, and the owed ask is
        # retried — which is the right way round against a double question being half-answered.
        "Is Dr. Smith the approver?",
    ):
        assert not mq.is_one_sentence(text), text


def test_a_STAND_DOWN_during_the_model_call_stops_the_question(store, monkeypatch):
    """#900 review 9, finding 1. A stand-down deliberately does NOT advance the episode — it
    silences the episode it is in — so every comparand the open compares stayed satisfied while
    it committed: same incarnation, same content, same episode, still unsettled.

    A question written before it and landing after it therefore opened on an objective the
    operator had just silenced, and `needs_you` flagged the mission for exactly the thing they had
    asked to stop hearing about.

    Red against an open that compares identity, content, settlement and episode but never asks
    whether the operator has said stop.
    """
    from agent_sessions import review as review_mod

    m = _mission()
    reply = {
        "question": "which PR did you mean?",
        "options": [
            {"label": "the first one", "action_index": 0},
            {"label": "the second one", "action_index": 1},
        ],
    }
    assert len(mq.options_from_reply(reply)[1]) == 2

    async def _stand_down_then_reply(messages, **kw):
        episode, _stood, _q = missions.objective_hold(m["id"], "pr_open")
        assert missions.stand_down(m["id"], "pr_open", episode=episode)
        return reply

    monkeypatch.setattr(review_mod, "complete_json", _stand_down_then_reply)
    out = asyncio.run(mq.ask(m["id"], "pr_open", context="waiting"))

    assert out is None
    assert missions.open_question_row(m["id"]) is None
    row = missions.get_mission(m["id"])
    kinds = [e["kind"] for e in row["events"]]
    assert "question" not in kinds
    # …and not silent, like every other refusal here.
    assert "error" in kinds
    # THE SILENCE HOLDS. The operator asked to stop hearing about this objective, and nothing the
    # producer did while they were asking changed that.
    _episode, stood, held = missions.objective_hold(m["id"], "pr_open")
    assert stood is True and not held
    assert "question" not in (missions.derive_needs_you([m["id"]])[m["id"]]["why"] or [])
