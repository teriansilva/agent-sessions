"""When it is unsure, it ASKS — a bounded choice, never a guess (#892, Phase 3b of #840).

#840 lists this as a goal in its own right: *"When it is unsure, it asks — a bounded choice with
concrete options, never a guess dressed up as a decision."* Without it a mission that hits an
ambiguity has exactly two outcomes: the supervisor spends its nudge budget and escalates, or the
model guesses. Escalation says *something* is wrong without saying what would fix it; a guess is
worse, because it looks like a decision.

**The model chooses WHICH action, never WHAT IT DOES.** That sentence is this module, and it is
#883's authority model applied to actions rather than to probes:

* the model receives a NUMBERED LIST of the actions the server can take, and returns indices;
* the `label` is display text the operator reads, and carries no authority whatsoever;
* the action is looked up from :data:`ACTIONS` **by index, server-side, after the answer arrives**;
* an index outside the list, or an action name that is not in the closed set, is refused at the
  route rather than clamped.

So "an option's label is never executed" is an assertable test rather than a hope: there is no
code path from the label to anything that runs. A regression drives a label containing an action
name and asserts nothing happens.

**Free text is an ANSWER, not an instruction.** Answering in prose records the operator's words
and lets the next supervisor pass read them. It does not become delivered agent input — that is
what the composer's fenced path is for, and conflating them would make an answer box a second,
unfenced way to type at a session.

**A question stands the OBJECTIVE down, not the mission.** A mission may have five objectives and
be stuck on one; silencing the whole mission would stall follow-through on the other four.
"""

from __future__ import annotations

import contextlib
import logging

from . import aitasks, mission_fence, missions, prompts, review

#: Re-exported: the roster lock name now lives with the protocol that uses it (`mission_fence`),
#: because the ANSWER path needs exactly the same one and a second definition beside it would be
#: a second lock domain wearing the same name (#900 review 7, finding 1).
roster_key = mission_fence.roster_key


log = logging.getLogger(__name__)

#: Bounds. Two is not a choice and five is a menu nobody reads.
MIN_OPTIONS = 2
MAX_OPTIONS = 4
LABEL_MAX = 120
QUESTION_MAX = 300

#: A question is ONE question (#900 review 8, finding 4). #892's contract is "one sentence, and
#: 2-4 concrete options", and only the length half was enforced — so a reply asking *"Which
#: release is this? Also, should I waive the gate?"* was accepted with one option set covering two
#: ambiguities. Whichever the operator answers, the other is answered too, by an option that was
#: never about it. That is a guess wearing a decision's clothes, which is the exact thing this
#: phase exists to replace.
#:
#: **A question may carry ONE sentence terminator, and it must be the last thing in it** (#900
#: reviews 8-10). #892's contract is "one sentence, and 2-4 concrete options", and only the length
#: half was enforced — so a reply asking *"Which release is this? Also, should I waive the gate?"*
#: was accepted with one option set covering two ambiguities. Whichever the operator answers, the
#: other is answered too, by an option that was never about it.
#:
#: Two earlier rules were both context-free guesses dressed as grammar, and each failed OPEN:
#:
#: * requiring a capital after the terminator let *"Which release is this? or should I waive the
#:   gate?"* through — a lowercase second question is still a second question;
#: * exempting abbreviations let *"Should I say no. then deploy?"* and *"Wait vs. should I
#:   continue?"* through, because `no` and `vs` are on any such list, and no list can tell an
#:   abbreviation from a sentence that happens to end in one.
#:
#: So the rule stops trying to parse English. A terminator COUNTS when the text ends there or
#: continues with whitespace (after any closing quotes or brackets) — which is what makes `v1.2`
#: and `127.0.0.1` ordinary characters rather than sentence ends, since their dots are followed by
#: digits. One counting terminator, at the end, is one sentence. Anything else is refused.
#:
#: The cost is real and is the right way round: a question carrying `Dr. ` or `e.g. ` is dropped.
#: That is not silent — an `error` notice lands on the timeline, the escalation still flags the
#: mission, and the owed ask is retried on the next pass — whereas an accepted double question is
#: half-answered by an option that was never about that half.
_TERMINATORS = ".?!…。？！"
#: What may follow the FINAL terminator and still leave the text one sentence — a closing quote or
#: bracket. Only used for that tail check now (#900 review 11): deciding whether a terminator
#: COUNTS by looking for an allowlisted closer was the fail-open half, because punctuation is
#: open-ended and an unfamiliar mark read as "not a sentence end". A generous list is safe here,
#: where the question is "is there anything of substance after the terminator".
_CLOSERS = "\"'’”')]»›}"


def _counting_terminators(s: str) -> list[int]:
    """Indexes of the terminators that actually END something.

    **Fail closed** (#900 review 11). The first version only counted a terminator when what
    followed it was whitespace, once an allowlisted closer had been skipped — so it missed the two
    shapes an allowlist can never cover:

    * `Which release?Also, should I waive?` — no space at all. A missing space does not make two
      questions one;
    * `「どのリリース？」 それとも待つ？」` — a closer this list had never heard of. Punctuation
      is open-ended, and a grammar that only recognises the marks it was told about answers "one
      sentence" to everything it does not understand.

    So a terminator counts unless it is demonstrably inside a NUMBER: a `.` with a digit on each
    side, which is what `v1.2`, `127.0.0.1` and `3.5 GB` are. There is exactly one exemption and
    it is the narrowest one that keeps the values these questions actually carry.

    **"Glued to an alphanumeric" was too wide** (#900 review 12). It exempted `release.Then`,
    `stable.Then` and `No.Then` alongside `v1.2` — a model that omits the space after a full stop
    was writing two questions, and the rule read them as one. Nothing about the characters around
    a dot separates an abbreviation from a sentence boundary (`vs.` and `Dr.` were already the
    same problem one round earlier, and the answer there was the same: refuse). A rule that
    cannot tell must refuse, because the failure it prevents — one option set answering two
    ambiguities — is worse than dropping a question that is asked again on the next pass.
    """
    out: list[int] = []
    for i, ch in enumerate(s):
        if ch not in _TERMINATORS:
            continue
        if ch == ".":
            prev = s[i - 1] if i else ""
            nxt = s[i + 1] if i + 1 < len(s) else ""
            if prev.isdigit() and nxt.isdigit():
                continue  # inside a version or an address: `v1.2`, `127.0.0.1`, `3.5`
        out.append(i)
    return out


def is_one_sentence(text: str) -> bool:
    """Is this a single sentence? The producer's half of #892's bounded-question contract."""
    s = text.strip()
    if not s:
        return False
    ends = _counting_terminators(s)
    if not ends:
        return True  # no terminator at all is one (unpunctuated) question
    if len(ends) > 1:
        return False
    # …and the one it has must be the END: only closers may follow it.
    return all(c in _CLOSERS for c in s[ends[0] + 1 :])


ACTIONS: tuple[tuple[str, str], ...] = (
    (
        "note_answer",
        "Record the operator's choice on the mission and carry on. Use when the answer only "
        "needs to inform what you do next.",
    ),
    (
        "waive_objective",
        "Mark the objective this question is about as NOT REQUIRED. Use when the answer is that "
        "the objective does not apply to this mission.",
    ),
    (
        "stand_down_objective",
        "Stop following up on the objective this question is about, without settling it. Use "
        "when the answer is that it is being handled elsewhere.",
    ),
    (
        "close_mission",
        "Propose that the mission is finished. Use when the answer is that there is nothing "
        "further to do. This does not close it — the operator still confirms.",
    ),
)

ACTION_NAMES: frozenset[str] = frozenset(name for name, _ in ACTIONS)

#: WHAT EACH ACTION DOES, IN THE SERVER'S OWN WORDS, for the operator rather than for the model.
#:
#: The model authors the label AND picks the action, and the operator sees only the label — so a
#: label reading "Keep working; leave this required" over a hidden `waive_objective` obtains a
#: human confirmation under false pretences (#900 review 2, finding 1). The label never *executes*
#: anything, which was the property the closed set was built for; it turns out that is not the
#: whole threat, because a button that lies still gets pressed.
#:
#: So the card renders THIS beside the label. It is not model-authored, it cannot be, and it is
#: keyed on the action the server will actually run.
CONSEQUENCE: dict[str, str] = {
    "note_answer": "Records your answer. Nothing else changes.",
    "waive_objective": "Marks this objective NOT REQUIRED. The mission can finish without it.",
    "stand_down_objective": "Stops following up on this objective. It stays unmet.",
    "close_mission": "Proposes that the mission is finished. You still confirm.",
}

#: The actions that CHANGE something beyond the timeline, and therefore ask twice. An operator
#: pressing a button whose label the model wrote should not be able to settle an objective in one
#: tap on the strength of that label alone.
SETTLING: frozenset[str] = frozenset({"waive_objective", "stand_down_objective", "close_mission"})

assert set(CONSEQUENCE) == ACTION_NAMES, (
    "every action needs operator-facing consequence text; missing: "
    f"{sorted(ACTION_NAMES - set(CONSEQUENCE))}"
)


def offered_actions_for(rows) -> tuple[tuple[str, str], ...]:
    """:func:`offered_actions`, asked about a real objective set — the production entry point.

    The rule itself is NOT here: `missions.gates_settled` owns it, and this is the one place the
    question path asks. Splitting it this way is deliberate (#1063) — `offered_actions` maps a
    settled/not-settled answer to a list and decides nothing, so there is no second copy of the
    completion rule to drift from the one the transaction commits on.
    """
    from . import missions

    return offered_actions(settled=missions.gates_settled(rows))


def offered_actions(*, settled: bool) -> tuple[tuple[str, str], ...]:
    """The subset of :data:`ACTIONS` this question may actually produce (#900 review 4, finding 4).

    **An option that cannot do what its consequence says is worse than one option fewer.** The
    card tells the operator, in the server's own words, that `close_mission` "proposes that the
    mission is finished" — and `_apply_answer_con` re-reads the completion gates inside the
    answer's transaction, so with any required objective unmet it returns `not proposed`. The
    operator confirmed a settling action twice and nothing happened.

    Which is not a hypothetical: the supervisor returns through its `likely_done` branch as soon
    as every gate is met, BEFORE anything escalates, so every question it generates today occurs
    with at least one gate unmet. That made the option dead on arrival everywhere it could appear.
    Rather than delete it — the action is right, and a caller with met gates would want it — the
    list is built from the state the question is being asked in, so the offer and the effect
    cannot disagree.

    Applicability lives HERE, in the server-built list, rather than in a filter over what the
    model returned: the model picks an INDEX into this list, so an action that is not in it
    cannot be selected at all.

    **`settled`, never an unmet COUNT (#1063).** The paragraph above reasons that a question is
    only ever generated with a gate unmet — true of every mission that HAS gates, and false of
    one that has none, whose unmet count is `0` from the moment it is created. So a notes-only
    mission was offered `close_mission`, and because the answer path counted the same way, the
    operator could confirm it and finish a mission that had checked nothing. The parameter is now
    the store's own predicate: an empty gate set is not settled.
    """
    if not settled:
        return tuple((n, d) for n, d in ACTIONS if n != "close_mission")
    return ACTIONS


def render_actions(actions: tuple[tuple[str, str], ...] = ACTIONS) -> str:
    """The numbered list the model selects from.

    Shows the NAME and what it does — never a mission id, an objective key or anything else the
    model could echo back as if it had chosen it. Same rule as the objective templates: anything
    the model is shown, it can be induced to repeat.
    """
    return "\n".join(f"{i}. {name} — {desc}" for i, (name, desc) in enumerate(actions))


def options_from_reply(
    obj: object, actions: tuple[tuple[str, str], ...] = ACTIONS
) -> tuple[str, list[dict], int]:
    """`(question, options, dropped)` from a model reply. Every refusal DROPS, never repairs.

    An option whose index is out of range cannot be salvaged: the label alone says what the
    operator would be choosing and nothing about what would happen, so a "repaired" option is one
    the server invented. #883 established that rule for objectives and it holds here for the same
    reason.

    `actions` is THE SAME LIST THE MODEL WAS SHOWN, passed in rather than re-derived: an index
    resolved against a different list is the "an index is not an identity" failure with the
    server picking the identity.
    """
    dropped = 0
    if not isinstance(obj, dict):
        return "", [], 1
    question = obj.get("question")
    if not isinstance(question, str) or not question.strip():
        return "", [], 1
    # ONE QUESTION, not two joined by a full stop (#900 review 8, finding 4). Dropped rather than
    # truncated to the first sentence: keeping half of what the model asked and pairing it with
    # options written for both halves would be the server inventing the question, which is the
    # rule every other refusal in this module follows.
    if not is_one_sentence(question):
        return "", [], 1
    raw = obj.get("options")
    if not isinstance(raw, list):
        return "", [], 1

    out: list[dict] = []
    seen: set[int] = set()
    for item in raw[: MAX_OPTIONS * 2]:
        if not isinstance(item, dict):
            dropped += 1
            continue
        # AN ALLOWLIST. Naming only the fields that refuse a row leaves every field the contract
        # does not have silently accepted — a hallucinated `"action": "rm -rf"` would ride along
        # unexamined, and the whole point is that nothing model-authored decides what runs.
        if set(item) - {"label", "action_index"}:
            dropped += 1
            continue
        idx = item.get("action_index")
        # `bool` is an `int` in Python and `True` would select element 1. Refuse the type rather
        # than let a truthy value pick an action nobody asked for.
        if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < len(actions):
            dropped += 1
            continue
        label = item.get("label")
        if not isinstance(label, str) or not label.strip():
            dropped += 1
            continue
        if idx in seen:
            continue  # a repeated action is a restatement, not a refusal
        seen.add(idx)
        action = actions[idx][0]
        # The consequence rides WITH the option, from the server's table, so the card cannot
        # render a label without it and a stored question carries its own truth.
        out.append(
            {
                "label": label.strip()[:LABEL_MAX],
                "action": action,
                "consequence": CONSEQUENCE[action],
                "settling": action in SETTLING,
            }
        )
        if len(out) >= MAX_OPTIONS:
            break
    if len(out) < MIN_OPTIONS:
        return "", [], dropped + 1
    return question.strip()[:QUESTION_MAX], out, dropped


async def _notice(mission_id: str, text: str, *, meta: object = None) -> None:
    """Say that no question could be asked — under `error`, never `question`.

    Silence would be the real failure: an absent question and a broken feature look identical from
    the outside, which is exactly the complaint #772 recorded about the orchestrator. But the
    notice must not itself read as an open question — see the callers.
    """

    with contextlib.suppress(Exception):
        await missions.run_admitted(
            lambda: missions.append_event(mission_id, "error", text=text, meta=meta)
        )


async def _fenced_open(
    mission_id, objective_key, question, options, incarnation, episode, content, path
):
    """Open the question INSIDE the write fence (#900 review 2, finding 2).

    A question hold WITHDRAWS AUTHORITY: it is the supervisor saying "I am waiting on you", and
    `supervisor_authority_verdict` refuses a write while one is open. Every other authority
    withdrawal in this app — detach, a terminal transition, an objective edit — takes
    `session_input.sessions_transaction()` before it commits, for one reason: without it the
    withdrawal can land between the write fence's final comparison and `os.write()`, and the
    supervisor types into a session the console is about to show a question about.

    Reproduced on the previous head: the question committed after the final fingerprint callback
    returned and the write still settled `delivered`. Being in the tuple is not enough; the
    commit has to take the same lock the fence holds.

    The protocol itself — fail-closed enumeration, the roster pseudo-key, the re-read inside the
    lock, and the whole thing on a worker thread — is `mission_fence.fenced_write`, which the
    ANSWER route now shares (#900 review 7, finding 1). It used to be written out here and
    approximated there, and the approximation was the hole.
    """

    def _open():
        return missions.open_question(
            mission_id,
            objective_key,
            question,
            options,
            expect_incarnation=incarnation,
            expect_episode=episode,
            expect_content=content,
            path=path,
        )

    return await mission_fence.fenced_write(mission_id, _open, path=path)


async def ask(mission_id: str, objective_key: str, *, context: str = "", path=None) -> dict | None:
    """Produce ONE question about one objective and store it. Returns the stored row, or None.

    Never raises: a mission that cannot get a question asked is a mission that carries on being
    nudged, which is the status quo rather than a failure. Silence would be the real problem, so
    every outcome that is not "a question appeared" writes a timeline event saying which — an
    absent question and a broken feature must not look the same (#883's rule, and #772's
    complaint).
    """
    row = await missions.run_admitted(lambda: missions.get_mission(mission_id, path=path))
    if row is None:
        return None
    objectives = row.get("objectives") or []
    target = next((o for o in objectives if str(o.get("key")) == objective_key), None)
    if target is None:
        return None
    # THE IDENTITY OF THE OBJECTIVE WE ARE ASKING ABOUT, taken from the SAME SNAPSHOT as the
    # prompt's inputs (#900 review 2, finding 3).
    #
    # It is compared when the question is opened, because the model call takes seconds and the
    # objective can be dropped and re-added inside them — a key match would then open the question
    # against a different objective wearing the same name (#900 review 1, finding 4).
    #
    # Reading it with a SECOND call was a torn capture of exactly the kind this is defending
    # against: `get_mission` returns the old objective and its context, a drop-and-re-add lands,
    # and the separate read then captures the REPLACEMENT's incarnation — so the compare-and-set
    # accepts a result produced from the old objective's context. `get_mission` holds one
    # transaction for the whole row, and the incarnation is on the objective it returned.
    incarnation = str(target.get("incarnation") or "")
    # …AND WHAT IT SAID, from the same snapshot (#900 review 6, finding 4). The incarnation says
    # the ROW is the same; a retitle changes none of the identities compared at commit time, so a
    # question written about one piece of work could open against an objective that now describes
    # another — and a settling answer then acts on the new wording.
    content = missions.objective_content(target)
    # …AND THE EPISODE IT IS ABOUT (#900 review 5, finding 3). An episode advances when the
    # objective is stood down or re-opened, so a question written about one and landing in the
    # next is a question about a situation that has already been closed out.
    episode, _stood, _q = await missions.run_admitted(
        lambda: missions.objective_hold(mission_id, objective_key, path=path)
    )
    # THE ACTION LIST IS BUILT FROM THE STATE THE QUESTION IS ASKED IN (#900 review 4, finding 4),
    # and from the SAME snapshot as everything else the prompt is made of — so the list the model
    # indexes into and the gates the answer will be applied against describe one moment.
    # THROUGH THE STORE'S OWN PREDICATE (#900 review 5, finding 4), so the offer and the effect
    # count the same thing. Counting stored state alone here made two answers to one question: a
    # gate stored `met` whose latest observation had gone false exposed `close_mission`, and
    # answering it then hit `_apply_answer_con`'s check — which reads the observation — and
    # returned `not proposed`. Offered and impossible is the shape this whole finding is about.
    actions = offered_actions_for(objectives)
    try:
        async with aitasks.single_flight("mission-question", mission_id):
            obj = await review.complete_json(
                [
                    {"role": "system", "content": prompts.effective("mission_question")},
                    {
                        "role": "user",
                        "content": (
                            f"Instruction:\n{row.get('instruction') or row.get('title') or ''}\n\n"
                            # THE OBJECTIVE ITSELF, not just its key (#900 review 3, finding 4).
                            # A key is an internal handle — `custom_7` says nothing about the
                            # work — so a model given only that cannot form a concrete question
                            # about it, and the prompt already claimed it would receive the
                            # checklist. Taken from `target`, which came out of the same snapshot
                            # as the incarnation, so the question is about the objective whose
                            # identity is compared when it opens.
                            f"Objective in question: {objective_key}\n"
                            f"  what it is: {target.get('title') or '(untitled)'}\n"
                            f"  state: {target.get('state') or 'pending'}"
                            f"{' (required for done)' if target.get('gate') else ''}\n\n"
                            f"Recent activity:\n{context}\n\n"
                            f"Actions:\n{render_actions(actions)}"
                        ),
                    },
                ]
            )
    except review.NotConfiguredError:
        # A fresh install has no AI endpoint. Not silent — but NOT a `question` event either.
        #
        # `derive_needs_you` reads an open question as `MAX(seq WHERE kind='question') >
        # MAX(seq WHERE kind='answer')`, so filing a "could not ask" notice under that kind would
        # flag the mission as needing the operator FOR EVER, with nothing on screen to answer.
        # A notice about the asking is an `error`; only an actual question is a `question`.
        await _notice(mission_id, "No question could be asked — no AI endpoint is configured.")
        return None
    except Exception as e:  # noqa: BLE001
        log.debug("mission question for %s failed: %s", mission_id, type(e).__name__)
        await _notice(mission_id, f"No question could be asked ({type(e).__name__}).")
        return None

    question, options, dropped = options_from_reply(obj, actions)
    if not options:
        await _notice(mission_id, "No usable question was produced.", meta={"dropped": dropped})
        return None
    try:
        return await _fenced_open(
            mission_id, objective_key, question, options, incarnation, episode, content, path
        )
    except missions.MissionError as e:
        # The objective moved under the model call. Not silent — an absent question and a broken
        # feature must not look the same — but not a `question` either, for the same reason the
        # other notices are not: `derive_needs_you` would flag the mission for ever with nothing
        # on screen to answer.
        await _notice(mission_id, f"No question could be asked ({e}).")
        return None
    except Exception as e:  # noqa: BLE001
        # THE NEVER-RAISES BOUNDARY, and it has to mean it (#900 review 3, finding 1).
        #
        # `_fenced_open` can raise `AuthorityFenceBusy` — the fence was held and the question
        # could not commit under it. That is not a `MissionError`, so it escaped, aborted the
        # whole supervisor pass, and took this episode's only chance to ask with it: the ask
        # happens once, after `escalate_once` wins, and a later pass in the same episode does not
        # get another. A transient lock is the wrong reason to lose that permanently.
        #
        # Caught broadly on purpose. The specific exception is one of several the store and the
        # fence can produce, and the boundary's promise is about the CALLER — a mission that
        # cannot get a question asked carries on being nudged, which is the status quo rather
        # than a failure.
        log.debug("mission question for %s could not open: %s", mission_id, type(e).__name__)
        await _notice(mission_id, f"No question could be asked ({type(e).__name__}).")
        return None
