"""Turn a mission instruction into a checklist, by SELECTING operator-authored templates (#883).

**The model may choose WHICH objective, never WHAT IT DOES.** That sentence is the whole module.

`http_status` and `http_revision` make a request, so if model output could reach a URL, then
untrusted text — an issue body, a transcript the planner read — could choose an address the server
fetches on a schedule. Server-side request forgery with a cadence attached.

The answer is not to validate a model-authored URL; an allowlist of hosts is a blocklist problem
in disguise, and every round of it is another "did we think of this scheme / redirect / rebind".
The answer is that **there is no code path from model text to a request target at all**:

* the model receives a NUMBERED LIST of templates and returns indices into it;
* `probe` is read from the selected template and refused as model input, and an HTTP probe's
  arguments — its `url` — are never model input either;
* anything with no template becomes a NOTE — no probe, never a gate;
* so the only way a new request target can exist is a human typing it into a playbook.

**One bounded exception, and exactly one (#1061):** a selection may fill `repo` or `branch` on a
forge or git probe — the arguments that let "it is merged" name WHICH branch. They are not request
targets: forge probes resolve against the configured forge, git probes against the mission's own
checkout. And a value is accepted only if the OPERATOR wrote it: it must be a whole token of the
mission's instruction, may not overwrite a key the template sets, and still passes
`missions.validate_probe_args`. The instruction arrives only through the authenticated create route,
so it is the operator's text — including text they chose to paste from an issue. The residual case,
a token planted in pasted text, can at most aim a forge probe at another branch or repository ON THE
CONFIGURED FORGE; it can never name a host.

That is what makes the acceptance test assertable on the HTTP client rather than on a stored
row: there is nothing to fail open.
"""

from __future__ import annotations

import contextlib
import logging

from . import aitasks, missions, prompts, review

log = logging.getLogger(__name__)

#: Bound on one instantiation. An objective list nobody reads is worse than three that matter,
#: and this is also the blast radius of a model that decides everything is important.
MAX_SELECTED = 12
MAX_NOTES = 6
#: A model-adapted title is still a title. Capped here as well as at the store, because the cap
#: is part of what keeps a "title" from becoming a payload.
TITLE_MAX = missions.OBJECTIVE_TITLE_MAX


def _render_templates(templates: list[dict]) -> str:
    """The numbered list the model selects from.

    Deliberately shows the TITLE and whether it can gate — never `probe_args`. The model does not
    need the target to choose the objective, and anything it is shown it can be induced to echo.
    """
    if not templates:
        return "(no templates are configured for this mission)"
    return "\n".join(
        f"{i}. {t['title']}"
        + ("" if t["probe"] == "none" else f"  [checks: {t['probe']}]")
        # Which templates may be fitted to a repo/branch the instruction names (#1061). The model
        # is told the KIND of argument, never shown an operator-authored value.
        + ("  [may set: repo, branch]" if t["probe"] in PARAMETERISABLE else "")
        for i, t in enumerate(templates)
    )


#: The probes a selection may PARAMETERISE (#1061), and the only arguments it may set. Forge and git
#: probes address a repository and a branch on the configured forge / the mission's own checkout.
#: The HTTP probes are deliberately absent: their `url` is a request target, and a model-written
#: target is exactly the SSRF surface the playbook boundary exists to prevent — those stay
#: operator-authored in the playbook.
PARAMETERISABLE: frozenset[str] = frozenset(
    {"git_local", "forge_pr", "forge_checks", "forge_review", "forge_merged", "forge_run"}
)
PARAM_KEYS: frozenset[str] = frozenset({"repo", "branch"})
PARAM_VALUE_MAX = 200
_PROSE_WRAP_CHARS = "\"'`()[]{}<>,.;:!?"


def _operator_authored(value: object, instruction: str) -> str | None:
    """`value` if the operator wrote it — a single token that occurs verbatim in the instruction.

    The model proposes objectives from the instruction text alone; it has no forge access, so any
    branch or repository it names that the instruction does not is a guess. A value the operator
    typed is a target the operator authored. Whitespace is refused so a phrase ("merge and") can
    never pass for a target merely because it occurs in prose, and the match is a whole token.
    """
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not v or len(v) > PARAM_VALUE_MAX or any(c.isspace() for c in v):
        return None
    # A WHOLE TOKEN, never a substring: `devopsagent/alph` occurs inside `devopsagent/alpha`, and a
    # prefix of what the operator typed is not what they typed. Tokens are split on whitespace and
    # shed the punctuation prose wraps them in (quotes, backticks, brackets, a trailing comma).
    tokens = {t.strip(_PROSE_WRAP_CHARS) for t in instruction.split()}
    return v if v in tokens else None


def _rows_from_reply(
    obj: dict,
    templates: list[dict],
    *,
    instruction: str = "",
    stats: dict | None = None,
) -> tuple[list[dict], int]:
    """`(rows, dropped)` — the objective rows to write, and how many selections were refused.

    ``stats``, when given, is filled with ``{"parameterised": n}`` — how many rows carry
    model-supplied `probe_args` — which is one of the facts that decides whether the mission asks
    the operator to confirm its checklist before it runs (#1061 Phase 3).

    Every refusal is a DROP, never a repair. A selection carries no title of its own beyond an
    optional adapted one, so an invalid `template_index` cannot become a note: there would be
    nothing to name it. An earlier draft said it should, which was a graceful-degradation rule
    with no data to degrade into (#883 review).
    """
    rows: list[dict] = []
    dropped = 0
    seen: set[tuple] = set()
    used_keys: set[str] = set()
    parameterised = 0

    # CONTAINERS ARE CHECKED, not assumed. `{"objectives": {...}}` is a perfectly plausible thing
    # for a model to emit, and slicing a dict raises `TypeError` — a parse failure escaping as a
    # crash, which is the opposite of the stated contract that drift DROPS rows (review on #884).
    selections = obj.get("objectives")
    notes = obj.get("notes")
    if not isinstance(selections, list):
        dropped += 1 if selections is not None else 0
        selections = []
    if not isinstance(notes, list):
        dropped += 1 if notes is not None else 0
        notes = []

    for item in selections[: MAX_SELECTED * 2]:
        if not isinstance(item, dict):
            dropped += 1
            continue
        idx = item.get("template_index")
        # `bool` is an `int` in Python, and `True` would index element 1. Refuse the type rather
        # than let a truthy value select an objective nobody asked for.
        if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < len(templates):
            dropped += 1
            continue
        # AN ALLOWLIST, not a denylist. Naming the fields that refuse a row left every field the
        # contract does NOT have — a typo, a hallucinated key, a future field — silently accepted,
        # so the row survived while nobody had checked what it said. The contract has exactly
        # three fields; anything else means the reply stopped matching it, which #883 says DROPS
        # the row rather than repairing it (review on #884).
        #
        # It is also the stronger SSRF answer: a selection that tried to author a target produces
        # NOTHING rather than a sanitised objective, which is what the prompt already promised
        # ("any you add is ignored and the row is refused").
        if set(item) - {"template_index", "gate", "title", "probe_args"}:
            dropped += 1
            continue
        # An ACTUAL boolean or nothing. `bool("true")`/`bool(1)`/`bool({})` are all coercions,
        # and #883's contract is that drift DROPS a row rather than degrading it into something
        # with a different meaning — keeping it as non-gating turns an intended required outcome
        # into an optional one, and completion then proceeds without it.
        if "gate" in item and not isinstance(item["gate"], bool):
            dropped += 1
            continue
        t = templates[idx]
        # PARAMETERS (#1061 Phase 2), under three rules, each a DROP when broken — never a repair:
        # only a parameterisable probe, only `repo`/`branch`, and only values the operator typed.
        # A key the operator's template already sets is theirs; the model may fill gaps, never
        # overwrite. Whatever survives still goes through the one shared validator.
        model_args: dict[str, str] = {}
        if "probe_args" in item:
            raw = item["probe_args"]
            base = t.get("probe_args") or {}
            ok = (
                isinstance(raw, dict)
                and bool(raw)
                and t["probe"] in PARAMETERISABLE
                and not (set(raw) - PARAM_KEYS)
                and not (set(raw) & set(base))
            )
            if ok:
                for k, v in raw.items():
                    authored = _operator_authored(v, instruction)
                    if authored is None:
                        ok = False
                        break
                    model_args[k] = authored
            if ok:
                try:
                    missions.validate_probe_args(t["probe"], {**base, **model_args})
                except missions.MissionError:
                    ok = False
            if not ok:
                dropped += 1
                continue
        sig = (idx, tuple(sorted(model_args.items())))
        if sig in seen:
            # A repeated selection means the same objective twice, which is a benign restatement
            # rather than an incoherent plan. Deduplicated, not counted as a refusal. With
            # parameters the identity is (template, arguments): the same template for two branches
            # is two objectives, not one said twice.
            continue
        seen.add(sig)
        # A PRESENT title must be the contract's shape. `title: 7` was being repaired into the
        # template's own title, which produces an executable row from a reply that did not match
        # the contract — the same "degrade into a different meaning" the gate rule forbids.
        if "title" in item and not isinstance(item["title"], str):
            dropped += 1
            continue
        title = item.get("title")
        # A MINTED KEY for a template's second and later use (#1061): keys are unique per mission,
        # and `merged` for two branches must be two rows. `missions.minted_key` keeps the template's
        # key as the prefix (so it never enters the reserved note namespace) and uses a separator
        # playbook keys cannot contain, so the panel can always find the row's template again.
        key = t["key"]
        if key in used_keys:
            n = 2
            while missions.minted_key(t["key"], n) in used_keys:
                n += 1
            key = missions.minted_key(t["key"], n)
        used_keys.add(key)
        if model_args:
            parameterised += 1
        row = {
            "key": key,
            # The model may ADAPT the title and nothing else. Everything below comes from the
            # template, which the operator wrote.
            "title": (
                title[:TITLE_MAX] if isinstance(title, str) and title.strip() else t["title"]
            ),
            "probe": t["probe"],
            "probe_args": ({**(t.get("probe_args") or {}), **model_args} or None)
            if model_args
            else t.get("probe_args"),
            # By here `gate` is absent or a real bool — a non-bool dropped the whole row above.
            "gate": item.get("gate") is True and t["probe"] != "none",
            "source": "playbook",
        }
        # THE OPERATOR'S DIRECTION, copied from the template (#983) — never from the reply, whose
        # allowlist above has no field for one, so a selection that tries to author a direction
        # refuses its whole row like any other extra field.
        if t.get("direction"):
            row["direction"] = t["direction"]
        rows.append(row)
        if len(rows) >= MAX_SELECTED:
            break

    for n, item in enumerate(notes[:MAX_NOTES]):
        if not isinstance(item, dict) or set(item) - {"title"}:
            # A note is `{"title": ...}` and nothing else. Same rule as a selection: a field the
            # contract does not have refuses the row rather than being dropped from it.
            dropped += 1
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            dropped += 1
            continue
        rows.append(
            {
                # RESERVED namespace, so a note can never collide with a template key — the
                # collision is unreachable rather than detected. `prefs` rejects a playbook key
                # using this prefix at write time.
                "key": f"{missions.NOTE_KEY_PREFIX}{n + 1}",
                "title": title[:TITLE_MAX],
                # A note checks nothing and gates nothing. Both are structural, not defaults:
                # `_op_add` refuses a `model` row that carries a probe at all.
                "probe": "none",
                "gate": False,
                "source": "model",
            }
        )
    if stats is not None:
        stats["parameterised"] = parameterised
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
    # The binding is captured WITH the templates, so what is written later is checked against
    # exactly what was offered to the model rather than against a second read that could already
    # have drifted.
    # ONE resolution for both. Two awaited reads would let prefs change between them, producing
    # a digest of the NEW config beside the OLD templates — the write-boundary check would then
    # compare new against new, pass, and write the stale targets anyway.
    status, templates, binding = await missions.run_admitted(
        lambda: missions.templates_and_binding(mission_id)
    )
    if status != "ok":
        # The operator's config changed under the mission (or they never chose). Recorded on the
        # timeline naming the id, so it is fixable rather than mysterious — and NOT substituted
        # with another playbook, which would arm gating objectives nobody picked here.
        await missions.run_admitted(
            lambda: missions.append_event(
                mission_id,
                "objective",
                text=(
                    # A declined checklist is the operator's choice, said as one (#1061) — not
                    # "no objective templates: declined (:none)", which reads as a fault.
                    "checklist declined for this mission — objectives are notes only"
                    if status == "declined"
                    else f"no objective templates: {status}"
                    + (f" ({row.get('playbook_id')})" if row.get("playbook_id") else "")
                ),
            )
        )

    obj = await review.complete_json(
        [
            {"role": "system", "content": prompts.effective("mission_objectives")},
            {
                "role": "user",
                "content": (
                    f"Instruction:\n{row.get('instruction') or row.get('title') or ''}\n\n"
                    f"Templates:\n{_render_templates(templates)}"
                ),
            },
        ]
    )
    stats: dict = {}
    rows, dropped = _rows_from_reply(
        obj if isinstance(obj, dict) else {},
        templates,
        instruction=str(row.get("instruction") or ""),
        stats=stats,
    )
    if not rows:
        return {"objectives": [], "dropped": dropped, "templates": status}
    try:
        written = await missions.run_admitted(
            lambda: missions.instantiate_objectives(mission_id, rows, expect_binding=binding)
        )
    except missions.MissionError as e:
        # The playbook was revoked or edited, or the mission closed, while the model was
        # thinking. Both are 409s and both mean the same thing here: this proposal is stale and
        # writes nothing. Recorded on the timeline so an empty checklist has a reason attached.
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
    # NOTHING HERE GATES — said once, where the operator will look for it (#1063).
    #
    # A checklist of notes checks nothing and blocks nothing, so the mission can never confirm
    # itself finished; completion becomes the operator's to press. That used to be invisible,
    # and the surfaces that should have said so said the opposite — "every gate is met", over an
    # empty gate set. The row sits beside the `no objective templates: …` one this function
    # already writes, at the same moment and for the same reason: an unusual checklist should
    # explain itself rather than be discovered later.
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
                        "nothing on this checklist gates completion — this mission cannot "
                        "confirm itself finished; close it yourself when you are satisfied"
                    ),
                )
            )
    return {
        "objectives": written,
        "dropped": dropped,
        "templates": status,
        # How many rows carry model-supplied arguments — with `dropped` and whether anything gates,
        # the facts that decide whether the operator is asked to confirm the set (#1061 Phase 3).
        "parameterised": stats.get("parameterised", 0),
    }


async def propose_for_new_mission(mission_id: str) -> dict:
    """The lifecycle call site: fill a NEWLY CREATED mission's checklist (#883).

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
