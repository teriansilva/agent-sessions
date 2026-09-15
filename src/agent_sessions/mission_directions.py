"""Objective DIRECTIONS: operator text, filled only with that objective's own checked facts (#983).

The supervisor's model decides WHICH objective to nudge and WHEN. It has never decided WHAT is
typed, and this module keeps it that way while making the nudge concrete. A direction is text the
operator wrote on a playbook template (copied onto the mission's objective when the objective is
created) or on the mission itself. Its only variable parts are the placeholders in
:data:`PLACEHOLDERS`, and each one is filled from exactly one place:

* the objective's **own latest observation**, written by the probe runner from a forge read the
  server made itself — a typed int or a closed enum, never the probe's `detail` prose; or
* the objective's **operator-configured** `probe_args`.

Nothing else is a source. Not a sibling objective's observation (a checks objective says `{pr}`
about the PR IT resolved, never "the mission's PR"), not a title (a model may adapt one), not
`git_local`'s observed branch (the agent controls it), and nothing from session content.

**Not deliverable, never half-filled.** A placeholder whose fact is missing, stale, taken for a
different target, for different probe arguments, or of the wrong shape makes the whole direction
unrenderable (:class:`NotRenderable`). The caller holds it and escalates; it is never sent with a
blank and never silently replaced by the default nudge.

**One renderer, and it binds identity as well as text.** :func:`render` returns
``{text, source, facts, provenance, digest}``. The proposal persists all of it; delivery renders
again and types only when the text AND the identity it depends on are unchanged (:func:`matches`,
:func:`identity`). Text alone is not enough: a PR head can move while `{pr}` and `{checks}` still
read the same. But a re-probe that merely CONFIRMS the same facts is not a change, so the run
counter and the observation time are recorded, not compared; freshness is enforced at render time
instead. Without a direction the text is the operator's actual current `nudge_template` — the same
bytes an ordinary `continue` sends — so an edit to that template stales a proposal too.

This module is a leaf on purpose: `prefs` and `missions` call :func:`validate` at write time, so
everything it needs from the rest of the package is imported where it is used.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

#: The longest direction an operator may save. A direction is one instruction, not a brief.
DIRECTION_MAX = 1000

#: How old an observation may be and still fill a placeholder. The probe runner refreshes every
#: mutable objective on each supervisor pass, so a fact older than this means the probes stopped
#: reaching the row, and a claim about a PR from an hour ago is not a claim about now.
FACT_MAX_AGE_S = 3600.0

#: `render()["source"]`: the operator's direction, or the global nudge because there is none.
SOURCE_DIRECTION = "direction"
SOURCE_DEFAULT = "default_nudge"

#: `mission_objectives.direction_source`: copied from the playbook, or written for this mission.
DIRECTION_SOURCES: frozenset[str] = frozenset({"template", "operator"})

#: A placeholder is `{name}` with a name made of letters, digits and `_`. Anything else in braces
#: (`{a: 1}`, a lone `{`) is literal text. An empty or unknown name is refused at save.
_TOKEN_RE = re.compile(r"\{([A-Za-z0-9_]*)\}")

#: The bytes `handoff.sanitize_seed` strips. Refused at save rather than silently removed, so what
#: the operator typed is what they stored; the renderer still sanitizes, for a hand-edited row.
#: A test pins this equal to `handoff._CTRL_RE`.
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

PR_PROBES: frozenset[str] = frozenset({"forge_pr", "forge_checks", "forge_review", "forge_merged"})
PR_STATES: frozenset[str] = frozenset({"open", "closed", "merged"})
CHECK_STATES: frozenset[str] = frozenset({"success", "pending", "failure", "error", "warning"})
REVIEW_STATES: frozenset[str] = frozenset({"APPROVED", "REQUEST_CHANGES"})

#: Which probes TAKE a `repo` / `branch` argument. A test pins these to `PROBE_ARG_SCHEMA`.
REPO_ARG_PROBES: frozenset[str] = PR_PROBES | {"forge_run"}
BRANCH_ARG_PROBES: frozenset[str] = PR_PROBES | {"forge_run", "git_local"}

_HEAD_RE = re.compile(r"^[0-9A-Fa-f]{7,64}$")
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}$")
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


class DirectionError(ValueError):
    """A direction a WRITE must refuse (422)."""


class NotRenderable(Exception):
    """The direction cannot be filled from checked facts right now. The message says why."""


def _now() -> float:
    """The clock a fact's age is measured against; a seam so a test never has to sleep."""
    return time.time()


def _positive_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _one_of(allowed: frozenset[str]) -> Callable[[object], bool]:
    return lambda v: isinstance(v, str) and v in allowed


def _shaped(pattern: re.Pattern[str]) -> Callable[[object], bool]:
    return lambda v: isinstance(v, str) and bool(pattern.match(v))


@dataclass(frozen=True)
class Placeholder:
    """One row of the closed table: where the value comes from, and what it must look like."""

    name: str
    #: `"observed"` — the objective's latest observation; `"probe_args"` — its operator config.
    source: str
    field: str
    #: The probe kinds whose objective can fill this. Anything else is refused at save.
    probes: frozenset[str]
    valid: Callable[[object], bool]
    #: A fact about a PR, so the observation must carry that PR's full identity (number + head).
    pr_bound: bool


#: THE table. It drives save validation, the renderer and (in P2) the editor's chips, so adding a
#: fact means adding a row here, its target binding, and a test.
PLACEHOLDERS: dict[str, Placeholder] = {
    p.name: p
    for p in (
        Placeholder("pr", "observed", "number", PR_PROBES, _positive_int, True),
        Placeholder(
            "pr_state", "observed", "pr_state", frozenset({"forge_pr"}), _one_of(PR_STATES), True
        ),
        Placeholder(
            "checks", "observed", "state", frozenset({"forge_checks"}), _one_of(CHECK_STATES), True
        ),
        Placeholder(
            "review", "observed", "state", frozenset({"forge_review"}), _one_of(REVIEW_STATES), True
        ),
        Placeholder("repo", "probe_args", "repo", REPO_ARG_PROBES, _shaped(_REPO_RE), False),
        Placeholder(
            "branch", "probe_args", "branch", BRANCH_ARG_PROBES, _shaped(_BRANCH_RE), False
        ),
    )
}


def placeholders_in(text: str) -> list[str]:
    """The placeholder names in `text`, first occurrence order, each once."""
    seen: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def validate(direction: object, probe: object) -> str | None:
    """Normalize a direction for storage against the objective's probe. Raises `DirectionError`.

    `None`, and text that is only whitespace, mean "no direction" and return `None`. Everything
    else must be at most :data:`DIRECTION_MAX` characters, free of the control bytes a paste would
    strip, and use only placeholders this objective's probe can fill.
    """
    if direction is None:
        return None
    if not isinstance(direction, str):
        raise DirectionError("direction must be a string")
    if not direction.strip():
        return None
    if len(direction) > DIRECTION_MAX:
        raise DirectionError(f"direction is longer than {DIRECTION_MAX} characters")
    if _CTRL_RE.search(direction):
        raise DirectionError("direction may not contain control characters")
    kind = probe if isinstance(probe, str) else ""
    for name in placeholders_in(direction):
        ph = PLACEHOLDERS.get(name)
        if ph is None:
            allowed = ", ".join(f"{{{n}}}" for n in PLACEHOLDERS)
            raise DirectionError(f"unknown placeholder {{{name}}}; the placeholders are {allowed}")
        if kind not in ph.probes:
            raise DirectionError(f"{{{name}}} cannot be filled for a {kind or 'none'} objective")
    return direction


def probe_args_digest(args: object) -> str:
    """A short digest of an objective's probe arguments. `{}` and `None` are the same arguments.

    The probe runner stamps it on every observation it settles, so a fact can be proven to belong
    to the arguments the objective has NOW.
    """
    blob = json.dumps(args or None, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _observed_fact(ph: Placeholder, obj: dict, now: float) -> tuple[object, dict]:
    """`(value, provenance)` for an observation-backed placeholder, or :class:`NotRenderable`."""
    name = f"{{{ph.name}}}"
    obs = obj.get("observed")
    if not isinstance(obs, dict):
        raise NotRenderable(f"{name} has no observation to come from yet")
    if obs.get("stale") is True:
        raise NotRenderable(f"{name} is stale: the latest look at this objective could not be made")
    at = obs.get("at")
    if isinstance(at, bool) or not isinstance(at, int | float):
        raise NotRenderable(f"{name} comes from an observation with no time")
    if now - float(at) > FACT_MAX_AGE_S:
        raise NotRenderable(f"{name} is stale: this objective was last observed too long ago")
    # THE TARGET BINDING. `probe_target` is the destination the row is bound to now; the
    # observation records the destination it was answered for. Different means the probe was
    # rebound (a new forge, checkout, branch or head) and this answer describes somewhere else.
    target = obs.get("target")
    if not isinstance(target, str) or not target or target != str(obj.get("probe_target") or ""):
        raise NotRenderable(f"{name} was observed for a different target than this objective's")
    if obs.get("args_sha") != probe_args_digest(obj.get("probe_args")):
        raise NotRenderable(f"{name} was observed for different probe arguments than it has now")
    value = obs.get(ph.field)
    if value is None:
        raise NotRenderable(f"{name} is missing from this objective's latest observation")
    if not ph.valid(value):
        raise NotRenderable(f"{name} is not a value that can be typed")
    number = obs.get("number")
    head = obs.get("head_sha")
    if ph.pr_bound and not (
        _positive_int(number) and isinstance(head, str) and _HEAD_RE.match(head)
    ):
        raise NotRenderable(f"{name} comes from an observation without a complete PR identity")
    args = obj.get("probe_args") if isinstance(obj.get("probe_args"), dict) else {}
    return value, {
        "target": {
            "forge_target": target,
            "repo": str(args.get("repo") or ""),
            "branch": str(args.get("branch") or ""),
            "pr": number,
            "head": head,
        },
        "observed_at": float(at),
    }


def _config_fact(ph: Placeholder, obj: dict) -> tuple[object, dict]:
    """`(value, provenance)` for a placeholder filled from the operator's `probe_args`."""
    args = obj.get("probe_args") if isinstance(obj.get("probe_args"), dict) else {}
    value = args.get(ph.field)
    if value is None or value == "":
        raise NotRenderable(f"{{{ph.name}}} is not configured on this objective's probe")
    if not ph.valid(value):
        raise NotRenderable(f"{{{ph.name}}} is not a value that can be typed")
    return value, {
        "target": {"probe_args": probe_args_digest(obj.get("probe_args"))},
        "observed_at": None,
    }


#: What a fact is ABOUT, and therefore compared: where it was answered from, and for which PR and
#: commit. `observed_at` is recorded beside it but is not here — see :func:`identity`.
_FACT_TARGET_FIELDS: tuple[str, ...] = (
    "forge_target",
    "repo",
    "branch",
    "pr",
    "head",
    "probe_args",
)
#: The objective a render belongs to, for every source.
_OBJECTIVE_FIELDS: tuple[str, ...] = (
    "mission_id",
    "objective_key",
    "episode",
    "incarnation",
    "direction_source",
)


def identity(rendered: object) -> dict | None:
    """The part of a render that delivery COMPARES, and that the digest covers. `None` if malformed.

    Compared: the text and its source; the objective (mission, key, episode, incarnation, where its
    direction came from); and, for a direction, the probe binding's destination (`probe_target`),
    its arguments digest, the forge-configuration revision it was bound under (`probe_rev`), the
    mission's `cwd` and `merge_sha` (server-owned target inputs, read from the store), and each
    fact's name, value, forge target, repo, branch, PR number and head SHA.

    Recorded only, for display and audit: each fact's `observed_at`, the binding's `probe_gen`, and
    the probe kind (which cannot change without a new incarnation). A re-probe that confirms the
    same facts moves exactly those, so it must not stale a proposal — every supervisor pass
    re-probes, and pinning them made a Suggest proposal die at the next pass with nothing changed.
    Freshness is still enforced, separately, by :func:`render`: a stale or too-old fact is not
    renderable at all.

    `probe_rev` stays compared because it is not a run counter: it is the forge-config revision,
    advanced only when the operator saves the forge settings, and a fact fetched under another
    forge configuration is a fact from another authority.

    The default-nudge fallback compares the text and the objective and NO probe fields, so no
    re-probe can stale it, exactly as an ordinary `continue` stays deliverable until its TTL.
    """
    if not isinstance(rendered, dict):
        return None
    prov = rendered.get("provenance")
    facts = rendered.get("facts")
    if not isinstance(prov, dict) or not isinstance(facts, list):
        return None
    source = rendered.get("source")
    out: dict = {
        "text": rendered.get("text"),
        "source": source,
        "objective": {k: prov.get(k) for k in _OBJECTIVE_FIELDS},
    }
    if source == SOURCE_DIRECTION:
        # TARGET INPUTS, FENCED VS NOT (#983 review 4871). A probe target (`mission_probes.Target`)
        # is resolved from two kinds of input, and only one kind can be ordered against byte one:
        #
        # * SERVER-OWNED — changed only by this app, through a write that takes the byte-one fence
        #   (`session_input.fact_transaction` / the mission write fence): the forge settings (via
        #   `probe_rev`), the objective's `probe` and `probe_args` (the args digest; the probe kind
        #   cannot change without a new incarnation), and the mission's `cwd` and `merge_sha`. All
        #   of them are in this identity, so the in-fence re-render refuses a change that lands
        #   after the final guard, with no git subprocess.
        # * AGENT-CONTROLLED — the checkout's origin remote, current branch and local HEAD, read
        #   with `git`. The agent changes them with its own git, which takes no fence, so they are
        #   checked against the bound `probe_target` at the pre-claim render and in the final guard
        #   (`current_authority(resolve_target=True)`) and cannot be ordered against byte one.
        out["probe"] = {
            "probe_target": prov.get("probe_target"),
            "probe_args": prov.get("probe_args"),
            "probe_rev": prov.get("probe_rev"),
            "mission_cwd": prov.get("mission_cwd"),
            "mission_merge_sha": prov.get("mission_merge_sha"),
        }
        out["facts"] = [
            {
                "name": f.get("name"),
                "value": f.get("value"),
                "target": {k: (f.get("target") or {}).get(k) for k in _FACT_TARGET_FIELDS},
            }
            if isinstance(f, dict)
            else None
            for f in facts
        ]
    return out


def _digest(ident: dict | None) -> str:
    """sha256 over exactly the compared fields — what `_final_guard` and the fence carry."""
    blob = json.dumps(ident, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def render(obj: dict | None, cfg: dict, *, now: float | None = None) -> dict:
    """The exact text a supervisor `continue` for this objective types, and what it rests on.

    `obj` is `missions.objective_snapshot(...)` — the stored row, including its binding columns
    and current episode, read in one transaction. `cfg` is the orchestrator block, for the
    fallback template. Returns ``{text, source, facts, provenance, digest}``; raises
    :class:`NotRenderable` when a direction cannot be filled.
    """
    from . import actuator, handoff

    if not obj:
        raise NotRenderable("the objective no longer exists")
    ts = _now() if now is None else now
    direction = obj.get("direction")
    has_direction = isinstance(direction, str) and bool(direction.strip())
    provenance: dict = {
        "mission_id": str(obj.get("mission_id") or ""),
        "objective_key": str(obj.get("key") or ""),
        "episode": int(obj.get("episode") or 0),
        "incarnation": str(obj.get("incarnation") or ""),
        "probe": str(obj.get("probe") or ""),
        "probe_target": str(obj.get("probe_target") or ""),
        "probe_args": probe_args_digest(obj.get("probe_args")),
        # The mission-side, SERVER-OWNED inputs of the probe target (#983 review 4871): read from
        # the missions row in the same snapshot, so the in-fence re-render compares them without a
        # git subprocess. See `identity` for which target inputs are fenced and which are not.
        "mission_cwd": str(obj.get("mission_cwd") or ""),
        "mission_merge_sha": str(obj.get("mission_merge_sha") or ""),
        "probe_gen": int(obj.get("probe_gen") or 0),
        "probe_rev": int(obj.get("probe_rev") or 0),
        "direction_source": (str(obj.get("direction_source") or "") or None)
        if has_direction
        else None,
        "facts": [],
    }
    facts: list[dict] = []
    if not has_direction:
        # The operator's CURRENT global nudge, byte for byte what an ordinary `continue` sends.
        text = actuator.default_nudge_text(cfg)
        source = SOURCE_DEFAULT
    else:
        try:
            body = handoff.sanitize_seed(str(direction))
        except handoff.HandoffError:
            raise NotRenderable("the direction has no text that can be typed") from None
        probe = provenance["probe"]
        values: dict[str, str] = {}
        for name in placeholders_in(body):
            ph = PLACEHOLDERS.get(name)
            if ph is None:
                raise NotRenderable(f"{{{name}}} is not a placeholder")
            if probe not in ph.probes:
                raise NotRenderable(
                    f"{{{name}}} cannot be filled for a {probe or 'none'} objective"
                )
            if ph.source == "observed":
                value, where = _observed_fact(ph, obj, ts)
            else:
                value, where = _config_fact(ph, obj)
            typed = str(value)
            # Shape-checked already; sanitized anyway, and a value the sanitizer would change is
            # refused rather than typed in its altered form.
            try:
                clean = handoff.sanitize_seed(typed)
            except handoff.HandoffError:
                raise NotRenderable(f"{{{name}}} has no text that can be typed") from None
            if clean != typed:
                raise NotRenderable(f"{{{name}}} carries characters that cannot be typed")
            values[name] = typed
            facts.append({"name": name, "value": value, **where})
            provenance["facts"].append({"name": name, **where})
        text = _TOKEN_RE.sub(lambda m: values[m.group(1)], body)
        source = SOURCE_DIRECTION
    if len(text) > actuator.NUDGE_MAX:
        raise NotRenderable(f"the filled direction is longer than {actuator.NUDGE_MAX} characters")
    if not text.strip():
        raise NotRenderable("there is no text to type")
    rendered = {"text": text, "source": source, "facts": facts, "provenance": provenance}
    rendered["digest"] = _digest(identity(rendered))
    return rendered


def matches(persisted: object, fresh: dict) -> tuple[bool, str]:
    """Is `fresh` still the render that was proposed? ``(ok, why_not)``.

    The text and the identity it depends on are compared SEPARATELY and both must hold. The text is
    what the operator was shown; the identity is what made it true, and it can change while the
    text does not (a head that moved under an identical `{pr}` and `{checks}`). What is compared is
    :func:`identity`, never the whole provenance: a re-probe confirming the same facts is not a
    change.
    """
    if not isinstance(persisted, dict):
        return False, "this supervisor nudge has no recorded text to compare against"
    if persisted.get("text") != fresh.get("text"):
        return False, "the text this nudge would type has changed since it was proposed"
    if persisted.get("source") != fresh.get("source"):
        return False, "this nudge's text now comes from a different source than when proposed"
    want = identity(persisted)
    if want is None:
        return False, "this supervisor nudge has no recorded provenance to compare against"
    if want != identity(fresh):
        return False, (
            "the facts behind this nudge changed since it was proposed "
            "(its objective, target, head or a fact's value)"
        )
    return True, ""


def current_authority(
    snapshot: dict | None, rendered: dict, *, resolve_target: bool = True
) -> tuple[bool, str]:
    """Were this render's facts fetched under the authority that is configured NOW? (#983 review)

    `render` checks an observation against the binding cached on the same row, and `matches`
    compares that binding with the one proposed. Both are what the row was BOUND to, and neither
    says whether that is still the current authority: after a forge-settings save a pending
    direction stayed deliverable until the next re-probe rebound the row. So this reads the
    current values and requires the row's binding to equal them:

    * the forge-configuration revision (`missions.forge_revision`, advanced by every
      `prefs.set_forge` save) must equal the row's `probe_rev`;
    * with `resolve_target`, the destination the probe runner would resolve now
      (`mission_probes.resolve_target`: forge settings, checkout remote, branch and HEAD, the
      mission's merge SHA) must equal the row's `probe_target`.

    Only a render with an OBSERVATION-backed fact is subject to it. The default nudge and a
    direction filled only from probe arguments rest on no fetched fact. Fails closed.

    `resolve_target=False` is the in-fence form. Resolving the target runs `git`, which does not
    belong under the registry lock. What a fenced mutation can change is checked there anyway: the
    revision here, and the mission's `cwd` / `merge_sha` through the render identity (`identity`),
    both read from the store. The checkout half (remote, branch, HEAD) moves under the agent's own
    `git`, which takes no fence, so the guard before the fence is where it is checked.
    """
    facts = rendered.get("facts") if isinstance(rendered, dict) else None
    if not any(isinstance(f, dict) and f.get("observed_at") is not None for f in facts or []):
        return True, ""
    if not snapshot:
        return False, "the objective no longer exists"
    from . import mission_probes, missions

    try:
        revision = int(missions.forge_revision())
    except Exception:  # noqa: BLE001 — an unreadable revision is not the current one
        return False, "the forge settings revision could not be read, so the facts are unverifiable"
    if int(snapshot.get("probe_rev") or 0) != revision:
        return False, "the forge settings changed since this objective's facts were fetched"
    if resolve_target:
        mission = {
            "cwd": snapshot.get("mission_cwd"),
            "merge_sha": snapshot.get("mission_merge_sha"),
        }
        try:
            now = mission_probes.resolve_target(mission, snapshot).digest
        except Exception:  # noqa: BLE001 — resolve_target never raises; belt and braces
            return False, "this objective's forge target could not be resolved"
        if now != str(snapshot.get("probe_target") or ""):
            return False, (
                "this objective's forge target moved since its facts were fetched "
                "(forge settings, checkout or merge)"
            )
    return True, ""
