"""Turn a mission instruction into its objectives (#1088; checklists removed, #1653).

**The model may write WHAT done looks like, never HOW it is checked.** That sentence is the module.

Checklists (operator-authored objective templates, #883) were removed completely (#1653), so every
mission now works the way one that declined a checklist did: with an AI endpoint configured, each
outcome the model writes becomes a REAL objective — `supervisor_judged`, gating, source
`instruction` — judged by the supervisor against what the agents produce. The guards that make
that safe:

* the only kind such a row can carry is `supervisor_judged` — no arguments, no direction, so there
  is no code path from model text to a probe target (the SSRF rule `model` rows already keep);
* at most :data:`missions.INSTRUCTION_GATES_MAX` of them, titles checked like every title;
* a reply that breaks the contract DROPS rows rather than degrading them;
* a judgment can at most PROPOSE completion.

If the model writes none, one judged gate — "Done as you instructed", whose criterion is the
instruction itself — keeps the mission measured. Without an endpoint there is nothing to write or
judge them, and the model's outcomes would only be notes (`source = "model"`, no probe, never a
gate) — but `propose_for_new_mission` stops before that path.
"""

from __future__ import annotations

import contextlib
import logging

from . import aitasks, missions, prompts, review

log = logging.getLogger(__name__)

#: Bound on one instantiation's notes (no endpoint). An objective list nobody reads is worse than
#: three that matter, and this is also the blast radius of a model that decides everything matters.
MAX_NOTES = 6
#: A model-adapted title is still a title. Capped here as well as at the store, because the cap
#: is part of what keeps a "title" from becoming a payload.
TITLE_MAX = missions.OBJECTIVE_TITLE_MAX


def _goal_title(item: object) -> str | None:
    """A declined mission's AI-built objective title, or `None` to DROP it (#1088).

    The same shape a note has — `{"title": str}` and nothing else — and the same title rules the
    store enforces (non-empty, bounded, no control characters), applied HERE so one bad title drops
    one row instead of failing the whole atomic batch."""
    if not isinstance(item, dict) or set(item) - {"title"}:
        return None
    title = item.get("title")
    if not isinstance(title, str):
        return None
    title = title.strip()
    if not title or len(title) > TITLE_MAX or any(ch < " " or ch == "\x7f" for ch in title):
        return None
    return title


def _rows_from_reply(obj: dict, *, ai_built: bool) -> tuple[list[dict], int]:
    """`(rows, dropped)` — the objective rows to write, and how many of the reply's were refused.

    Every refusal is a DROP, never a repair. The contract has one list, `notes`, of
    `{"title": str}`; anything else in the reply — a template selection from the old contract, a
    probe, a target — is not part of it and writes nothing.
    """
    rows: list[dict] = []
    dropped = 0
    # CONTAINERS ARE CHECKED, not assumed. `{"notes": {...}}` is a plausible thing for a model to
    # emit, and slicing a dict raises `TypeError` — a parse failure escaping as a crash, which is
    # the opposite of the stated contract that drift DROPS rows (review on #884).
    notes = obj.get("notes")
    if not isinstance(notes, list):
        dropped += 1 if notes is not None else 0
        notes = []
    if isinstance(obj.get("objectives"), list):
        dropped += len(obj["objectives"])  # the removed template-selection contract (#1653)

    if ai_built:
        # The model's outcomes ARE the objectives (#1088): each a gating `supervisor_judged` row,
        # capped, with nothing the model could turn into a target.
        goals = 0
        for item in notes[: missions.INSTRUCTION_GATES_MAX * 2]:
            title = _goal_title(item)
            if title is None:
                dropped += 1
                continue
            if goals >= missions.INSTRUCTION_GATES_MAX:
                dropped += 1
                continue
            goals += 1
            rows.append(
                {
                    "key": f"{missions.GOAL_KEY_PREFIX}{goals}",
                    "title": title,
                    "probe": "supervisor_judged",
                    "gate": True,
                    "source": "instruction",
                }
            )
        return rows, dropped

    for n, item in enumerate(notes[:MAX_NOTES]):
        if not isinstance(item, dict) or set(item) - {"title"}:
            # A note is `{"title": ...}` and nothing else: a field the contract does not have
            # refuses the row rather than being dropped from it.
            dropped += 1
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            dropped += 1
            continue
        rows.append(
            {
                # RESERVED namespace, so a note can never collide with another objective's key.
                "key": f"{missions.NOTE_KEY_PREFIX}{n + 1}",
                "title": title[:TITLE_MAX],
                # A note checks nothing and gates nothing. Both are structural, not defaults:
                # `_op_add` refuses a `model` row that carries a probe at all.
                "probe": "none",
                "gate": False,
                "source": "model",
            }
        )
    return rows, dropped


async def propose(mission_id: str) -> dict:
    """Instantiate objectives for one mission. Returns a small report for the caller.

    Never raises for a model that answers badly: a reply that does not match the contract drops
    rows rather than degrading them into something with a different meaning. It DOES propagate a
    store error, because a mission whose objectives could not be written must not look as though
    it has none.
    """
    row = await missions.run_admitted(lambda: missions.get_mission(mission_id))
    if row is None:
        raise missions.MissionNotFound(mission_id)
    # The model writes the objectives and the supervisor judges them (#1088) — for every mission,
    # since checklists were removed (#1653). Without an endpoint there is nothing to write them or
    # to judge them; `propose_for_new_mission` stops before this, and it is asked here as well so
    # a direct caller cannot arm judged gates nobody can settle.
    try:
        review._require_config()
        ai_built = True
    except review.NotConfiguredError:
        ai_built = False
    try:
        obj = await review.complete_json(
            [
                {"role": "system", "content": prompts.effective("mission_objectives")},
                {
                    "role": "user",
                    "content": (
                        f"Instruction:\n{row.get('instruction') or row.get('title') or ''}"
                    ),
                },
            ]
        )
    except review.ReviewError as e:
        if not ai_built:
            raise
        # A mission is never left unmeasured because the model call failed: it gets the fallback
        # gate below, and the timeline says why the model wrote nothing.
        why = aitasks.clamp_error(e)
        with contextlib.suppress(Exception):
            await missions.run_admitted(
                lambda: missions.append_event(
                    mission_id,
                    "objective",
                    text=f"the orchestrator could not write the objectives: {why}",
                )
            )
        obj = {}
    rows, dropped = _rows_from_reply(obj if isinstance(obj, dict) else {}, ai_built=ai_built)
    if ai_built and not rows:
        # ZERO usable objectives: one judged gate whose criterion is the instruction itself, so a
        # mission with an AI endpoint is never unmeasured.
        rows = [
            {
                "key": missions.DONE_AS_INSTRUCTED_KEY,
                "title": missions.DONE_AS_INSTRUCTED_TITLE,
                "probe": "supervisor_judged",
                "gate": True,
                "source": "instruction",
            }
        ]
    if not rows:
        return {"objectives": [], "dropped": dropped, "templates": "none"}
    try:
        written = await missions.run_admitted(
            lambda: missions.instantiate_objectives(mission_id, rows, as_proposal=True)
        )
    except missions.MissionError as e:
        # The mission closed (or already has objectives) while the model was thinking. A 409 means
        # this proposal is stale and writes nothing. Recorded on the timeline so an empty
        # objective list has a reason attached.
        if e.status != 409:
            raise
        # Bound out of the `except` clause — Python unbinds `e` at its end, so a lambda closing
        # over it is a latent NameError rather than a message.
        why = str(e)
        with contextlib.suppress(Exception):
            await missions.run_admitted(
                lambda: missions.append_event(
                    mission_id, "objective", text=f"no objectives proposed: {why}"
                )
            )
        return {"objectives": [], "dropped": dropped, "templates": "stale"}
    # SUGGESTIONS THAT DID NOT FIT (#1061 Phase 3): said once, on the timeline, and carried as
    # `meta` so the launch confirmation can say why this set is worth a look before anything runs.
    if written and dropped:
        with contextlib.suppress(Exception):
            await missions.run_admitted(
                lambda: missions.append_event(
                    mission_id,
                    "objective",
                    text=(
                        f"{dropped} suggestion{'s' if dropped != 1 else ''} did not fit and "
                        f"{'were' if dropped != 1 else 'was'} dropped"
                    ),
                    meta={"fit": {"dropped": dropped, "parameterised": 0}},
                )
            )
    # NOTHING HERE GATES — said once, where the operator will look for it (#1063).
    #
    # A list of notes checks nothing and blocks nothing, so the mission can never confirm itself
    # finished; completion becomes the operator's to press. That used to be invisible, and the
    # surfaces that should have said so said the opposite — "every gate is met", over an empty
    # gate set. An unusual objective list should explain itself rather than be discovered later.
    #
    # Written HERE rather than in the sweep, so it appears exactly once per instantiation
    # instead of once per pass, and needs no dedupe state to stay that way.
    if written and not any(o.get("gate") for o in written):
        with contextlib.suppress(Exception):
            await missions.run_admitted(
                lambda: missions.append_event(
                    mission_id,
                    "objective",
                    text=(
                        "no objective gates completion — this mission cannot "
                        "confirm itself finished; close it yourself when you are satisfied"
                    ),
                )
            )
    return {
        "objectives": written,
        "dropped": dropped,
        "templates": "none",
        # No row carries model-supplied arguments any more (#1653); kept so the confirmation
        # inputs (#1061 Phase 3) keep their shape.
        "parameterised": 0,
    }


async def propose_for_new_mission(mission_id: str) -> dict:
    """The lifecycle call site: fill a NEWLY CREATED mission's objectives (#883).

    Everything here is about not letting a proposal damage the thing it decorates.

    * **A mission is created whether or not this succeeds.** It runs after the 201 rather than
      inside it, because an operator's mission must not fail to exist because a model endpoint is
      down — and objectives are a *proposal*, which nothing downstream requires to be present.
    * **Silence would be the real failure**, so every outcome that is not "objectives appeared"
      writes a timeline event saying which. An empty checklist with no explanation is
      indistinguishable from a feature that does not work; that exact ambiguity is what the
      operator reported about the orchestrator (#772).
    * **Not configured is not an error.** A fresh install has no AI endpoint, and a red task
      counter on it would be reporting a fault that is really an unmade choice.
    * **Single-flighted per mission**, so a double-submitted create cannot instantiate the list
      twice — the second call finds the first in flight and declines rather than racing it into
      duplicate-key rejections halfway through a batch.
    """
    try:
        review._require_config()
    except review.NotConfiguredError:
        with contextlib.suppress(Exception):
            await missions.run_admitted(
                lambda: missions.append_event(
                    mission_id,
                    "objective",
                    text="no objectives proposed: no AI endpoint is configured",
                )
            )
        # `skipped`, NOT left pending. An unmade choice is a terminal outcome for this attempt:
        # on a fresh install — the case this branch exists to treat as normal — leaving it
        # pending made EVERY mission a permanent entry on the recovery worklist, reconsidered on
        # every boot for ever (review on #884).
        await _settle(mission_id, "skipped")
        return {"objectives": [], "dropped": 0, "templates": "not_configured"}

    # A STABLE kind with the mission as the exclusivity SCOPE. Spelling the per-mission
    # single-flight as `mission-objectives:<id>` put one permanent entry per mission into
    # `aitasks._last`, which `GET /api/ai/activity` serializes whole — unbounded growth in both
    # memory and every activity response (review on #884).
    try:
        async with aitasks.single_flight("mission-objectives", detail=mission_id, scope=mission_id):
            out = await propose(mission_id)
        await _settle(mission_id, "done")
        return out
    except aitasks.AlreadyRunning:
        # Another producer holds it. It owns the outcome — settling here would mark the intent
        # finished while the real attempt is still running, and a crash of THAT attempt would
        # then never be recovered.
        return {"objectives": [], "dropped": 0, "templates": "already_running"}
    except Exception as e:  # noqa: BLE001 — recorded on the mission, then swallowed
        log.warning("mission %s: objective proposal failed: %s", mission_id, e)
        # Bound out of the `except` clause: Python unbinds `e` when the clause ends, so a lambda
        # closing over it is a latent NameError rather than a message.
        why = aitasks.clamp_error(e)
        with contextlib.suppress(Exception):
            await missions.run_admitted(
                lambda: missions.append_event(
                    mission_id,
                    "objective",
                    text=f"no objectives proposed: {why}",
                )
            )
        # `failed` is TERMINAL for this attempt and keeps the mission off the retry list — a model
        # endpoint that answers badly would otherwise be retried on every boot for ever. The
        # timeline event above is the record; the operator re-runs it deliberately.
        await _settle(mission_id, "failed")
        return {"objectives": [], "dropped": 0, "templates": "error"}


async def _settle(mission_id: str, state: str) -> None:
    """Close the durable production intent, never raising — the outcome already happened."""
    with contextlib.suppress(Exception):
        await missions.run_admitted(lambda: missions.settle_objectives_state(mission_id, state))


async def recover_pending(*, older_than: float = 0.0, limit: int = 20) -> dict:
    """Finish objective production that a crash or restart interrupted (#883 review).

    The producer runs as a `BackgroundTask`, which lives only in the process that served the
    create — so a restart between the mission's commit and the model call left a permanent empty
    checklist, with no timeline event saying why and nobody to retry it. A background task is not
    a promise. The intent is stamped WITH the mission in one transaction; this is the caller that
    discharges it, the same shape as `resume_pending_operations` for a half-finished archive.

    **It pages FORWARD on a cursor, and that single change is what makes it both complete and
    non-spinning.** Four versions got this wrong, each leaving work permanently pending:

    * `older_than=120` excluded a mission created 30 seconds before the crash — the window a
      crash is most likely to land in — and boot is the only caller.
    * One batch of 20 left the 21st mission pending for ever.
    * A `max_batches` ceiling then left the 1,001st pending for ever.
    * Remembering what it had ATTEMPTED, while the query kept serving the oldest rows, starved
      everything behind a full page of intents that could not settle: the same 20 came back, the
      set said "nothing new", and mission 21 was never reached. The two regressions each covered
      one half of that — all-stuck with nothing behind it, and all-settling — so the combination
      went unnoticed (review on #884).

    A cursor answers all four. Each row is visited at most once per pass because the cursor only
    moves forward, so a stuck row cannot spin the loop; and nothing is skipped, because the pass
    ends only when a page comes back empty. `older_than` stays for a future periodic caller,
    where the guard IS meaningful.
    """
    out: dict = {"recovered": [], "failed": []}
    cursor: tuple[float, str] | None = None
    while True:
        try:
            rows = await missions.run_admitted(
                lambda c=cursor: missions.missions_awaiting_objectives(
                    older_than=older_than, limit=limit, after=c
                )
            )
        except Exception:  # noqa: BLE001 — recovery is opportunistic; it must never fail boot
            log.debug("objective recovery: worklist unavailable", exc_info=True)
            return out
        if not rows:
            return out
        for at, mid in rows:
            cursor = (at, mid)
            try:
                await propose_for_new_mission(mid)
                out["recovered"].append(mid)
            except Exception as e:  # noqa: BLE001 — one stuck mission must not block the others
                log.warning("mission %s: objective recovery failed: %s", mid, e)
                out["failed"].append(mid)
