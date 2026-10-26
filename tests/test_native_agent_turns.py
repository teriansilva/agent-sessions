"""Turns the agent runs for itself (#1438): journaled, projected beside operator turns, and never
shaped like a message someone sent."""

import json
import uuid
from dataclasses import replace

import pytest

import test_native_journal as journal_tests
from agent_sessions import native_journal as journal
from agent_sessions import native_runtime, share_api, structured_runtime
from test_native_journal import event, request

conversation = journal_tests.conversation
T0 = 1_791_790_000.0

# The agent turn's key set, operator snapshot and public link (the issue's turn schema table).
AGENT_KEYS = {
    "kind",
    "turn_id",
    "state",
    "ts",
    "reply",
    "reply_ts",
    "reply_truncated",
    "tools",
    "tools_truncated",
}
# Message boundaries for the long-turn fold (#1643): operator snapshot, never the public link.
FOLD_KEYS = {"reply_breaks", "first_message_partial", "reply_omitted"}
SUBMISSION_KEYS = {
    "operation_id",
    "text",
    "text_truncated",
    "attachments",
    "delivery",
    "delivery_turn_id",
    "delivery_reason",
    "context",
    "requires_authority",
    "usage",
    "author",
}


def raw(root, sid, binding, *, max_turns=None, live="self"):
    return native_runtime.project(
        journal.read(root, sid),
        {"request": {"model": None}, "adapter": binding.adapter},
        binding.worker_id if live == "self" else live,
        max_turns=max_turns,
    )


def snapshot(root, sid, binding, **kwargs):
    return structured_runtime._snapshot(binding.session_key, raw(root, sid, binding, **kwargs))


def at(monkeypatch, t):
    monkeypatch.setattr(journal.time, "time", lambda: t)


def operator(root, sid, binding, monkeypatch, t, text):
    at(monkeypatch, t)
    claim = request(binding, revision=journal.read(root, sid).revision)
    operation_id = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.record_handoff(root, binding, operation_id, "sent")
    journal.append_events(root, binding, [event(operation_id, text)])
    journal.append_events(
        root,
        binding,
        [
            {
                "kind": "turn_completed",
                "data": {
                    "operation_id": operation_id,
                    "native_turn_id": "native-turn-1",
                    "state": "completed",
                    "error": "",
                },
            }
        ],
    )
    return operation_id


def background(target, state="running"):
    return {
        "kind": "background",
        "data": {"state": state, "active": target is not None, "background_turn": target},
    }


def own(target, text=None, tool=None):
    if tool is not None:
        item_id, name, summary, state = tool
        data = {"agent_turn": target, "item_id": item_id, "state": state}
        # A call names its tool and input; its result only the call it answers (as Claude's do).
        data.update({"tool": name, "summary": summary} if name else {})
        return {"kind": "tool", "data": data}
    return {
        "kind": "text",
        "data": {"agent_turn": target, "text": text, "partial": False, "truncated": False},
    }


def agent(root, sid, binding, monkeypatch, t, text, *, end="completed", target=None):
    """One turn the agent ran for itself: start, a tool call, what it said, its end."""
    target = target or str(uuid.uuid4())
    at(monkeypatch, t)
    events = [
        background(target),
        own(target, tool=("toolu_1", "Bash", json.dumps({"command": "gh pr checks"}), "running")),
        own(target, tool=("toolu_1", "", "", "completed")),
        own(target, text),
    ]
    if end is not None:
        events.append(background(None, end))
    journal.append_events(root, binding, events)
    return target


def test_an_own_turn_is_projected_as_its_own_turn_with_its_tools_and_reply(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    first = operator(root, sid, binding, monkeypatch, T0, "Filed #1436.")
    target = agent(root, sid, binding, monkeypatch, T0 + 60, "Hermes' review is in.")
    view = snapshot(root, sid, binding)
    assert [t["turn_id"] for t in view["turns"]] == [first, target]
    own_turn = view["turns"][1]
    assert own_turn["kind"] == "agent" and own_turn["state"] == "completed"
    assert own_turn["reply"] == "Hermes' review is in." and own_turn["ts"] == T0 + 60
    (tool,) = own_turn["tools"]
    assert tool["name"] == "Bash" and tool["outcome"] == "completed"
    assert "gh pr checks" in tool["summary"]
    # The operator's conversation state is the operator's: an own turn moves none of it.
    assert view["state"] == "idle" and view["active_turn"] is None
    assert view["turns"][0]["reply"] == "Filed #1436."


def test_the_agent_turn_key_set_is_exact_and_carries_nothing_of_a_submission(
    conversation, monkeypatch
):
    root, sid, binding = conversation
    operator(root, sid, binding, monkeypatch, T0, "one")
    agent(root, sid, binding, monkeypatch, T0 + 1, "two", end="failed")
    op_turn, own_turn = snapshot(root, sid, binding)["turns"]
    # reason: it failed. The fold's message shape (#1643) rides on the operator snapshot only.
    assert set(own_turn) == AGENT_KEYS | {"reason"} | FOLD_KEYS
    assert not set(own_turn) & SUBMISSION_KEYS  # absent, not null
    assert op_turn["kind"] == "operator" and "operation_id" in op_turn and "text" in op_turn
    # …and once the attribution overlay ran, still no author, never "sender unknown".
    overlaid = share_api.overlay_authors(snapshot(root, sid, binding), None)
    assert "author" not in overlaid["turns"][1] and "author" in overlaid["turns"][0]
    # The public link: the same keys redacted, minus `reason`.
    public = share_api.project(overlaid, {}, share_api._Redactor([]), "share-1")
    assert set(public["turns"][1]) == AGENT_KEYS


@pytest.mark.parametrize(
    ("end", "state"),
    [("completed", "completed"), ("interrupted", "interrupted"), ("failed", "failed")],
)
def test_an_own_turn_ends_as_its_end_says(conversation, monkeypatch, end, state):
    root, sid, binding = conversation
    agent(root, sid, binding, monkeypatch, T0, "x", end=end)
    (own_turn,) = snapshot(root, sid, binding)["turns"]
    assert own_turn["state"] == state


def test_a_running_own_turn_settles_when_its_worker_is_gone(conversation, monkeypatch):
    root, sid, binding = conversation
    agent(root, sid, binding, monkeypatch, T0, "x", end=None)
    (running,) = snapshot(root, sid, binding)["turns"]
    assert running["state"] == "running"
    (gone,) = snapshot(root, sid, binding, live=str(uuid.uuid4()))["turns"]
    # What it did is unknown once its worker died mid-turn (#1643), as for an operator turn.
    assert gone["state"] == "uncertain" and "worker ended" in gone["reason"]


def test_a_new_own_turn_ends_one_whose_end_never_came(conversation, monkeypatch):
    root, sid, binding = conversation
    agent(root, sid, binding, monkeypatch, T0, "first", end=None)
    agent(root, sid, binding, monkeypatch, T0 + 1, "second", end=None)
    states = [t["state"] for t in snapshot(root, sid, binding)["turns"]]
    assert states == ["completed", "running"]


@pytest.mark.parametrize("max_turns", [1, 2, 3, 4, 5])
def test_bounded_and_full_projections_agree_across_both_kinds(conversation, monkeypatch, max_turns):
    """One display window for both kinds, newest first; the cut may fall between them. Read
    again from disk (a restart) the answer is the same."""
    root, sid, binding = conversation
    operator(root, sid, binding, monkeypatch, T0, "op 1")
    operator(root, sid, binding, monkeypatch, T0 + 1, "op 2")
    agent(root, sid, binding, monkeypatch, T0 + 2, "own 3")
    operator(root, sid, binding, monkeypatch, T0 + 3, "op 4")
    full = raw(root, sid, binding)
    assert [t["reply"] for t in full["turns"]] == ["op 1", "op 2", "own 3", "op 4"]
    for _ in range(2):
        bounded = raw(root, sid, binding, max_turns=max_turns)
        assert bounded["turns"] == full["turns"][-max_turns:]
        assert bounded["omitted_turns"] == max(0, 4 - max_turns)
        journal._CACHE.clear()  # replayed from the file


def test_output_is_attributed_by_field_even_when_ids_collide(conversation, monkeypatch):
    """An operation id is caller-chosen: one equal to an agent turn's target still never moves
    output between the two."""
    root, sid, binding = conversation
    shared = operator(root, sid, binding, monkeypatch, T0, "operator words")
    agent(root, sid, binding, monkeypatch, T0 + 1, "own words", target=shared)
    op_turn, own_turn = snapshot(root, sid, binding)["turns"]
    assert (op_turn["kind"], op_turn["reply"]) == ("operator", "operator words")
    assert (own_turn["kind"], own_turn["reply"]) == ("agent", "own words")
    assert op_turn["tools"] == []


def test_a_secret_in_an_own_turn_is_redacted_for_the_visitor(conversation, monkeypatch):
    root, sid, binding = conversation
    secret = "s3cret-value-123"
    target = str(uuid.uuid4())
    at(monkeypatch, T0)
    journal.append_events(
        root,
        binding,
        [
            background(target),
            own(
                target,
                tool=("toolu_1", "Bash", json.dumps({"command": f"echo {secret}"}), "running"),
            ),
            own(target, f"the token is {secret}"),
            background(None, "completed"),
        ],
    )
    operator_view = share_api.overlay_authors(snapshot(root, sid, binding), {})
    assert secret in operator_view["turns"][0]["reply"]  # the operator sees their own session
    public = share_api.project(operator_view, {}, share_api._Redactor([secret]), "share-1")
    dumped = json.dumps(public)
    assert secret not in dumped
    assert public["turns"][0]["kind"] == "agent"


def test_a_tool_result_keeps_its_calls_name(conversation):
    """Claude's results name only the call they answer: the row keeps "Bash", never becomes the
    "tool" placeholder (it did, for every completed Claude call, before #1438)."""
    root, sid, binding = conversation
    claim = request(binding)
    op = claim["params"]["operation_id"]
    journal.claim(root, claim)
    journal.record_handoff(root, binding, op, "sent")
    common = {"operation_id": op, "native_turn_id": "native-turn-1", "item_id": "toolu_1"}
    call = {"tool": "Bash", "state": "running", "summary": json.dumps({"command": "ls"})}
    journal.append_events(
        root,
        binding,
        [
            {"kind": "tool", "data": {**common, **call}},
            {"kind": "tool", "data": {**common, "state": "completed", "output": "x"}},
        ],
    )
    (tool,) = raw(root, sid, binding)["turns"][0]["tools"]
    assert (tool["name"], tool["outcome"]) == ("Bash", "completed")


def test_turns_keep_journal_order_when_the_clock_steps_back(conversation, monkeypatch):
    root, sid, binding = conversation
    first = operator(root, sid, binding, monkeypatch, T0 + 100, "earlier, by the journal")
    target = agent(root, sid, binding, monkeypatch, T0, "later, by the journal")
    for max_turns in (None, 2):
        turns = raw(root, sid, binding, max_turns=max_turns)["turns"]
        assert [t["turn_id"] for t in turns] == [first, target]


@pytest.mark.parametrize("then", ["queued", "running"])
def test_live_work_stays_visible_behind_any_number_of_own_turns(conversation, monkeypatch, then):
    """Hermes on #1439: a message waiting (or running after waiting) behind 60 turns the agent ran
    for itself is never cut by the 50-turn window; settled history still is."""
    root, sid, binding = conversation
    old = operator(root, sid, binding, monkeypatch, T0, "old history")
    at(monkeypatch, T0 + 1)
    claim = request(binding, revision=journal.read(root, sid).revision)
    waiting = claim["params"]["operation_id"]
    journal.claim(root, claim, queued=True)
    for i in range(60):
        agent(root, sid, binding, monkeypatch, T0 + 2 + i, f"own {i}")
    if then == "running":
        journal.record_handoff(root, binding, waiting, "uncertain")
        journal.record_handoff(root, binding, waiting, "sent")
    for _ in range(2):
        view = snapshot(root, sid, binding, max_turns=50)
        ids = [t["turn_id"] for t in view["turns"]]
        assert waiting in ids and old not in ids
        assert next(t for t in view["turns"] if t["turn_id"] == waiting)["state"] == (
            "queued" if then == "queued" else "running"
        )
        assert view["omitted_turns"] == 62 - len(ids)
        journal._CACHE.clear()


def test_an_own_turn_cut_off_by_a_worker_crash_is_failed_not_done(conversation, monkeypatch):
    """Hermes on #1439: the next generation's first own turn must not settle the dead one's
    unfinished turn as completed."""
    root, sid, binding = conversation
    dead = agent(root, sid, binding, monkeypatch, T0, "cut off", end=None)
    successor = replace(binding, worker_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()))
    agent(root, sid, successor, monkeypatch, T0 + 1, "new generation", end=None)
    for live in (successor.worker_id, None):
        turns = native_runtime.project(
            journal.read(root, sid),
            {"request": {"model": None}, "adapter": binding.adapter},
            live,
        )["turns"]
        crashed = next(t for t in turns if t["turn_id"] == dead)
        assert crashed["status"] == "failed" and "worker ended" in crashed["reason"]


def test_background_work_belongs_to_the_worker_that_reported_it(conversation, monkeypatch):
    """Hermes on #1460: a replaced generation's last `background(active)` must not show the idle
    replacement busy — not in the operator's snapshot, not as the public link's `background`."""
    root, sid, binding = conversation
    at(monkeypatch, T0)
    task = {"kind": "background", "data": {"task_id": "task-1", "state": "running", "active": True}}
    journal.append_events(root, binding, [task])
    successor = replace(binding, worker_id=str(uuid.uuid4()), connection_id=str(uuid.uuid4()))

    def views(live):
        view = snapshot(root, sid, binding, live=live)
        public = share_api.project(view, {}, share_api._Redactor([]), "share-1")
        return view["native"]["background_active"], public["background"]

    assert views("self") == (True, True)
    assert views(successor.worker_id) == (False, False)
    assert views(None) == (False, False)
    # The replacement's own report counts.
    journal.append_events(root, successor, [task])
    assert views(successor.worker_id) == (True, True)
    # `_live_worker` selects the current generation. Passing the replaced worker as live
    # contradicts that caller contract; report arrival order cannot choose a replacement.
    inactive = {
        "kind": "background",
        "data": {"task_id": "task-1", "state": "completed", "active": False},
    }
    journal.append_events(root, binding, [inactive])
    assert views(successor.worker_id) == (True, True)
    journal.append_events(root, successor, [inactive])
    assert views(successor.worker_id) == (False, False)
    assert views(None) == (False, False)


def test_settled_history_still_ages_out_behind_own_turns(conversation, monkeypatch):
    """Only live work is kept past the window: inputs that settled without a completion (a
    steer, a refusal) are history and age out like any other turn."""
    root, sid, binding = conversation
    steered = []
    for i in range(3):
        at(monkeypatch, T0 + i)
        claim = request(binding, revision=journal.read(root, sid).revision)
        journal.claim(root, claim)
        journal.record_handoff(root, binding, claim["params"]["operation_id"], "sent")
        steered.append(claim["params"]["operation_id"])
    last = operator(root, sid, binding, monkeypatch, T0 + 5, "settled")
    for i in range(60):
        agent(root, sid, binding, monkeypatch, T0 + 10 + i, f"own {i}")
    ids = [t["turn_id"] for t in snapshot(root, sid, binding, max_turns=50)["turns"]]
    assert len(ids) == 50 and not ({*steered, last} & set(ids))


def test_a_waiting_message_reads_in_full_when_history_exhausts_the_text_budget(
    conversation, monkeypatch
):
    """Hermes on #1439: kept is not enough — what is waiting must read in full. 60 own turns of
    5,000-character replies spend the snapshot's text budget; the older queued message still
    shows every character the operator sent, through the native projection AND the facade."""
    root, sid, binding = conversation
    sent = "Please also keep the old breakpoint " * 40
    at(monkeypatch, T0)
    claim = request(binding, revision=journal.read(root, sid).revision, text=sent)
    waiting = claim["params"]["operation_id"]
    journal.claim(root, claim, queued=True)
    for i in range(60):
        agent(root, sid, binding, monkeypatch, T0 + 1 + i, f"{i:02d}" + "x" * 4998)
    for _ in range(2):
        view = snapshot(root, sid, binding, max_turns=structured_runtime.SNAPSHOT_TURNS)
        turn = next(t for t in view["turns"] if t["turn_id"] == waiting)
        assert turn["state"] == "queued"
        assert turn["text"] == sent and turn["text_truncated"] is False
        # History still gets what is left, newest first.
        assert view["turns"][-1]["reply"].startswith("59")
        journal._CACHE.clear()


def test_every_waiting_message_reads_in_full_when_the_queue_alone_exceeds_the_budget(
    conversation, monkeypatch
):
    """Hermes on #1439 (review 6238): eleven queued 20,000-character messages exceed the shared
    200k budget on their own. Each still reads in full — live input has its own per-field cap —
    through the native projection AND the facade, with history behind them too."""
    root, sid, binding = conversation
    waiting = {}
    for i in range(11):
        at(monkeypatch, T0 + i)
        text = f"{i:02d}" + "q" * (structured_runtime.SNAPSHOT_TEXT - 2)
        claim = request(binding, revision=journal.read(root, sid).revision, text=text)
        journal.claim(root, claim, queued=True)
        waiting[claim["params"]["operation_id"]] = text
    for i in range(20):
        agent(root, sid, binding, monkeypatch, T0 + 20 + i, "h" * 15_000)
    for _ in range(2):
        view = snapshot(root, sid, binding, max_turns=structured_runtime.SNAPSHOT_TURNS)
        shown = {t["turn_id"]: t for t in view["turns"]}
        for op, text in waiting.items():
            assert shown[op]["state"] == "queued"
            assert shown[op]["text"] == text and shown[op]["text_truncated"] is False
        journal._CACHE.clear()


def test_an_own_turns_messages_stay_apart_as_paragraphs(conversation, monkeypatch):
    """With #1448: an own turn's separate messages are paragraphs, as an operator turn's are."""
    root, sid, binding = conversation
    target = str(uuid.uuid4())
    at(monkeypatch, T0)
    journal.append_events(
        root,
        binding,
        [background(target), own(target, "Review is in."), own(target, "Applying it now.")],
    )
    (turn,) = snapshot(root, sid, binding)["turns"]
    assert turn["reply"] == "Review is in.\n\nApplying it now."


def quiet(root, sid, binding, monkeypatch, t, *, end="completed"):
    """An own turn that recorded nothing: its start and (optionally) its end only."""
    target = str(uuid.uuid4())
    at(monkeypatch, t)
    events = [background(target)] + ([background(None, end)] if end else [])
    journal.append_events(root, binding, events)
    return target


def test_an_own_turn_that_recorded_nothing_is_not_shown_once_it_ended(conversation, monkeypatch):
    """#1438 follow-up, found live: 200 of 202 own turns on the operator's install were empty
    (before workers recorded output, or quiet). Each was an empty header taking a window slot."""
    root, sid, binding = conversation
    first = operator(root, sid, binding, monkeypatch, T0, "one")
    quiet(root, sid, binding, monkeypatch, T0 + 1)
    spoke = agent(root, sid, binding, monkeypatch, T0 + 2, "said something")
    view = snapshot(root, sid, binding)
    assert [t["turn_id"] for t in view["turns"]] == [first, spoke]
    assert view["omitted_turns"] == 0  # not a turn at all, so nothing was "omitted"


def test_a_quiet_own_turn_still_shows_while_it_runs(conversation, monkeypatch):
    root, sid, binding = conversation
    running = quiet(root, sid, binding, monkeypatch, T0, end=None)
    (turn,) = snapshot(root, sid, binding)["turns"]
    assert turn["turn_id"] == running and turn["state"] == "running"
    # …and once its worker is gone with nothing recorded, there is nothing to show.
    assert snapshot(root, sid, binding, live=str(uuid.uuid4()))["turns"] == []


def test_quiet_own_turns_never_crowd_history_out_of_the_window(conversation, monkeypatch):
    root, sid, binding = conversation
    ops = [operator(root, sid, binding, monkeypatch, T0 + i, f"op {i}") for i in range(3)]
    for i in range(60):
        quiet(root, sid, binding, monkeypatch, T0 + 10 + i)
    view = snapshot(root, sid, binding, max_turns=structured_runtime.SNAPSHOT_TURNS)
    assert [t["turn_id"] for t in view["turns"]] == ops


@pytest.mark.parametrize("blank", ["", "   \n\t  "])
def test_an_ended_own_turn_with_only_empty_text_is_not_shown(conversation, monkeypatch, blank):
    """Hermes on #1452 (review 6284): Claude's assistant text-block conversion can emit an indexed
    `text` event with `text: ""` (or whitespace). It contributes neither a reply nor a tool, so
    once the own turn ends it must not project an empty `agent` row."""
    root, sid, binding = conversation
    first = operator(root, sid, binding, monkeypatch, T0, "one")
    target = str(uuid.uuid4())
    at(monkeypatch, T0 + 1)
    journal.append_events(
        root,
        binding,
        [background(target), own(target, blank), background(None, "completed")],
    )
    spoke = agent(root, sid, binding, monkeypatch, T0 + 2, "real reply")
    view = snapshot(root, sid, binding)
    assert [t["turn_id"] for t in view["turns"]] == [first, spoke]
    assert view["omitted_turns"] == 0


def test_a_colliding_operator_id_never_resurrects_a_quiet_own_turn(conversation, monkeypatch):
    """Hermes on #1452 (review 6284): `_operations` shares its key namespace between caller-chosen
    `operation_id` and `agent_turn`. A later operator turn reusing a completed quiet own turn's
    UUID must never resurrect the empty own turn."""
    root, sid, binding = conversation
    colliding = quiet(root, sid, binding, monkeypatch, T0)
    assert snapshot(root, sid, binding)["turns"] == []
    at(monkeypatch, T0 + 1)
    claim = request(binding, revision=journal.read(root, sid).revision)
    claim["params"]["operation_id"] = colliding
    journal.claim(root, claim)
    journal.record_handoff(root, binding, colliding, "sent")
    journal.append_events(
        root,
        binding,
        [
            event(colliding, "operator reply"),
            {
                "kind": "turn_completed",
                "data": {
                    "operation_id": colliding,
                    "native_turn_id": "native-turn-1",
                    "state": "completed",
                    "error": "",
                },
            },
        ],
    )
    (only,) = snapshot(root, sid, binding)["turns"]
    assert only["kind"] == "operator" and only["turn_id"] == colliding
    assert only["reply"] == "operator reply"
