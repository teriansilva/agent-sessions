"""The mission record — MISSION CONTROL's durable store (#846, Phase 1 of #840).

A **mission** is an instruction, a lifecycle state, an ordered objective list, 1..N owned
sessions and an append-only event stream. Everything the console does in later phases is a
client of this module, so the invariants below are the deliverable — not the row count.

**Why SQLite rather than the ledger.** ``orchestrator_ledger`` is an append-only JSONL of
*actions*, reduced on read. That shape is right for actions and wrong for missions: a mission is
long-lived, is queried by state/project/recency, accumulates an unbounded event stream, and needs
a durable compare-and-set on one lifecycle column. Reducing a growing JSONL on every poll to
answer "list my missions" is the wrong trade. This is the app's first *write* use of ``sqlite3``;
``transcript.py`` / ``opencode.py`` / ``antigravity.py`` are the read-only precedents.

**Off the event loop, on a bounded pool — not** ``asyncio.to_thread``. With
``busy_timeout=5000`` a contended write parks a thread for five seconds, and ``to_thread``
dispatches to ``run_in_executor(None, …)``: the interpreter's *default* pool, unbounded queue,
shared with every other blocking call in the process. A burst of five-second waits there starves
the file panel, ``owner``'s flock waits and the orchestrator's own off-loop work. So this module
owns its pool, exactly as :mod:`agent_sessions.files` does, and — because a bounded pool bounds
*running threads, not the queue* — admission is counted **above** the executor and equals the pool
size. Over the bound is a deterministic :class:`MissionsBusy` (503), never a queue the caller
experiences as a hang.

**Writes serialize** through one module lock, so SQLite's ``busy_timeout`` is a backstop against a
*second process*, not against this app's own concurrency.

**Reads fail soft; writes fail loudly.** A locked or corrupt DB degrades the console (empty rail
plus a stated reason) exactly as opencode's reader degrades the sidebar. A *write* that fails is
reported and leaves the mission in its prior state — a silently dropped adopt or archive is
indistinguishable from one that worked.

**Sensitive operator data.** ``instruction``, ``brief`` and recap text are verbatim operator text
and bounded model text: an operator can type a token into an instruction. They are never written
to a log line, never put in an error message, deleted (not tombstoned) with the mission, and
bounded by the retention pass. The file is 0600 **from creation** — a widened-then-narrowed
window is still a window.

**Nothing here is a transcript.** Evidence is referenced by kind and re-fetched live, exactly as
the ledger already does, so the store never becomes a place transcript content can leak from.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# ---------------------------------------------------------------- identity + bounds

log = logging.getLogger(__name__)

MISSION_ID_RE = re.compile(r"^msn_[0-9a-f]{32}$")

#: Bumped whenever the schema changes; ``PRAGMA user_version`` carries it in the file.
SCHEMA_VERSION = 29

#: How many live SUB-AGENTS one mission may hold, beyond the session it is already running.
#:
#: **A RESOURCE GUARD, NEVER A SECURITY CONTROL**, and the distinction is load-bearing rather than
#: pedantic: nothing here decides what a sub-agent may DO — that is the autonomy tier, the write
#: fence, `bypass=False` and the containment scope, none of which this number touches. What it
#: bounds is fan-out: a mission that can spawn without limit is a mission that can fill the host
#: with unattended agents by being approved repeatedly. Treating it as a safety boundary would be
#: the mistake, because an operator can raise it and it is enforced only where missions launch.
SPAWN_CAP = max(0, int(os.environ.get("AGENT_SESSIONS_MISSION_SPAWN_CAP", "2") or 2))
#: What an objective's state reads as once it no longer exists. A distinct value rather than
#: `None`, so a dropped objective and an objective whose state is unset can never look alike.
OBJECTIVE_GONE = "<dropped>"
#: The session a pre-v13 mission-wide checkpoint is carried onto. It matches no real session key
#: (engine ids never contain a space), so it is inert: it preserves the row for forensics without
#: ever suppressing a real session's first recap.
_PRE_V13_CHECKPOINT = "<pre-v13 mission-wide>"

TITLE_MAX = 200
INSTRUCTION_MAX = 8000
BRIEF_MAX = 8000
EVENT_TEXT_MAX = 4000
EVENT_META_MAX = 4000
OBJECTIVE_TITLE_MAX = 200
#: Room for a probe's facts AND a could-not-look record carrying the last good one forward. At 500
#: a long `unknown` detail (stored twice, as `detail` and `reason`) beside the carried `last` was
#: refused, so the row kept its previous FRESH observation instead of turning stale — and a
#: direction would have been filled from it (#983).
OBSERVED_MAX = 2000
OBJECTIVE_KEY_MAX = 64
PROBE_ARGS_MAX = 2000
#: Bound on one `GET /api/missions/{id}` timeline page.
EVENTS_PAGE_MAX = 200
EVENTS_PAGE_DEFAULT = 100
#: Bound on one `/api/missions` page.
LIST_LIMIT_MAX = 200
LIST_LIMIT_DEFAULT = 50
#: Objectives are an operator-sized list, not a data feed.
OBJECTIVES_MAX = 50

#: Events kept per mission. Trimming drops the oldest *droppable* kinds first (see
#: :data:`EVENT_KINDS_PRESERVED`) — never a decision, a state change or an objective edit, or
#: the growth would erase the recap stream the operator actually reads.
MISSION_EVENTS_MAX = 500
#: The ceiling that makes the cap a *cap*. Preserved kinds are protected from the soft cap above,
#: but "protected" cannot mean "unbounded": an operator retitling one objective in a loop grows
#: the timeline forever. Past this, preserved rows are dropped too.
#:
#: That is only safe because the settlement projection does **not** live on the event row (see
#: ``mission_settlements``): the timeline is a bounded *feed*, and a decision's content is a
#: separate durable fact that bounding the feed cannot reach.
MISSION_EVENTS_HARD_MAX = 2000
#: Closed missions older than this are pruned by :func:`retention_pass`.
MISSION_RETENTION_DAYS = 90
#: Rows deleted per retention pass — bounded so a long-idle install cannot stall one request.
RETENTION_BATCH = 200

#: Pool size. Small on purpose: writes serialize anyway, and this store is polled, not streamed.
MISSIONS_DB_WORKERS = 4
#: Admission equals the pool, so nothing ever queues behind a five-second wait.
MISSIONS_DB_MAX_INFLIGHT = MISSIONS_DB_WORKERS

_BUSY_TIMEOUT_MS = 5000
#: The ledger settlement hook runs inside somebody else's transition; it may never park a thread
#: for five seconds on our account, and it is best-effort, so it gets its own short wait.
_SETTLEMENT_BUSY_TIMEOUT_MS = 1000


# ---------------------------------------------------------------- the state machine

STATES: frozenset[str] = frozenset(
    {"draft", "planned", "dispatching", "running", "review", "done", "failed", "abandoned"}
)
#: States in which ``cwd`` may still be NULL. This MIRRORS the schema ``CHECK`` and must stay in
#: step with it — a draft exists before its project is resolved, which is exactly when the console
#: needs to ask which project was meant.
CWD_OPTIONAL_STATES: frozenset[str] = frozenset({"draft", "planned", "abandoned"})

#: States a mission may be PLANNED in (#893). A plan is a proposal to start work, so a mission
#: that has already started — or finished — cannot take one: re-planning a `running` mission is a
#: proposal to launch it twice, and the store refuses rather than leaving the operator a button
#: whose meaning depends on when they press it.
PLANNABLE_STATES: frozenset[str] = frozenset({"draft", "planned"})
#: A mission here is closed. ``abandoned`` alone is un-reopenable (see :data:`_ALLOWED`).
TERMINAL_STATES: frozenset[str] = frozenset({"done", "failed", "abandoned"})

#: The legal transition graph, as data rather than as scattered ``if``s. #840 names the happy
#: path and the two off-ramps but not the graph, and its §14 adds "reopen"; this is the whole
#: answer in one readable table.
_ALLOWED: dict[str, frozenset[str]] = {
    "draft": frozenset({"planned", "abandoned"}),
    # `planned -> running` is the ADOPTED path, and it is not a shortcut around `dispatching`.
    # The two say different things: `dispatching` is "we are launching a session", `running` is
    # "work is underway". A session the operator started themselves and then adopted makes the
    # second true without the first ever happening, and routing it through `dispatching` would
    # write a dispatch into the timeline that never occurred (#889).
    #
    # It is guarded rather than free: the transition requires the mission to actually HOLD an
    # active session (see `_require_a_session_to_run`). Without that guard the console could mark
    # an empty mission `running`, the supervisor would sweep it every pass, find nothing to
    # follow through on, and report a mission in flight that has no work in it.
    "planned": frozenset({"dispatching", "running", "draft", "abandoned"}),
    # `dispatching -> planned` is the NO-SPAWN retreat (#904 review 2, finding 7). A refusal
    # that happens before anything is launched — an ineligible engine, a policy withdrawal, a
    # host that cannot contain the agent — is not a mission that failed; it is a dispatch that
    # did not happen. Settling those as `failed` consumed the plan (the claim deletes it) into a
    # state only `running` leads out of, so the operator could neither re-plan nor retry: the
    # mission was stuck by a refusal whose whole point was that nothing had changed.
    "dispatching": frozenset({"running", "planned", "failed", "abandoned"}),
    # `running -> dispatching` is the SPAWN (#894), and it is the honest state: a launch really is
    # in flight for this mission. Making a spawn a dispatch is what lets it reuse the launch
    # fence, the attempt-generation CAS, the alive-is-not-started gate and the whole teardown
    # reconciliation, instead of growing a second launcher that would have to re-earn all of it.
    "running": frozenset({"review", "done", "failed", "abandoned", "dispatching"}),
    "review": frozenset({"running", "done", "failed", "abandoned"}),
    "done": frozenset({"running"}),
    # `failed -> planned` is START AGAIN (#966), and it is guarded rather than free: `set_state`
    # allows it only on the persisted evidence of a primary launch that typed nothing and whose
    # session was proved stopped, re-read inside the transaction (see `_retry_verdict`).
    "failed": frozenset({"running", "planned"}),
    # Terminal in the strong sense: abandoning is the operator saying "not this". Reopening it
    # would resurrect a mission whose sessions were already released and possibly archived.
    "abandoned": frozenset(),
}

OUTCOMES: frozenset[str] = frozenset({"done", "abandoned", "failed"})

#: What a launch typed into its session (#966), as the seed store's claim/ack record says.
#: `unknown` is the default and the answer for anything not affirmatively established.
SEED_OUTCOMES: frozenset[str] = frozenset(
    {"not_attempted", "zero_write", "partial", "delivered", "unknown"}
)
#: The only outcomes that establish NOTHING was written, and so the only ones Start again accepts.
RETRYABLE_SEED_OUTCOMES: frozenset[str] = frozenset({"not_attempted", "zero_write"})

# ---------------------------------------------------------------- events + objectives

EVENT_KINDS: frozenset[str] = frozenset(
    {
        "operator_msg",
        "assistant_msg",
        "plan",
        # An operator's edit of a stored plan (#967): the plan id and WHICH fields changed, never
        # the brief. A second full `plan` block per edit made the thread repeat the whole brief.
        "plan_edit",
        # A planning attempt that did not produce a plan, or whose result was not used: skipped
        # (no endpoint), failed (with the reason), discarded by the generation fence, or a model
        # reply that named a different project than the operator chose (#967).
        "planning",
        "dispatched",
        "recap",
        "action",
        "approval",
        "subagent",
        "state",
        "error",
        "completion",
        "objective",
        "question",
        "answer",
        "probe",
        # The supervisor's terminal "this needs you" (#885). Its own kind rather than an `error`:
        # an escalation is not a fault, it is the supervisor correctly deciding that a decision
        # is the operator's — and a timeline that files it under errors trains them to ignore it.
        "escalation",
        # Beyond #840's literal list: adopt/detach and archive are operator-visible changes that
        # are not actions, and the timeline has to be able to say them.
        "session",
        "archive",
    }
)

#: Kinds the SOFT cap may never drop — the mission's own history, as opposed to the recap stream
#: that actually grows. The hard ceiling above may still drop them; what it cannot reach is a
#: decision's content, which lives in ``mission_settlements`` rather than on the row.
EVENT_KINDS_PRESERVED: frozenset[str] = frozenset(
    {
        "action",
        "approval",
        "state",
        "objective",
        "completion",
        "plan",
        "plan_edit",
        "planning",
        "dispatched",
        "question",
        "answer",
        "session",
        "archive",
    }
)

#: The closed set of ways an objective can be settled. Phase 1 has no probe *runner* — it owns
#: the vocabulary so Phase 3 inherits a validated gate instead of inventing one.
PROBE_KINDS: frozenset[str] = frozenset(
    {
        "none",
        "git_local",
        "forge_pr",
        "forge_checks",
        "forge_review",
        "forge_merged",
        "forge_run",
        "http_status",
        "http_revision",
        "agent_judged",
    }
)
#: "the agent believes it wrote tests" is not evidence that it did, so it may never gate alone.
NON_GATING_PROBES: frozenset[str] = frozenset({"agent_judged"})

#: WHAT A WELL-FORMED ARGUMENT SET LOOKS LIKE, per probe kind (#883).
#:
#: `PROBE_KINDS` says which probes exist; this says what each one needs. Without it a probe could
#: be stored with a typo'd key, or with no target at all, and nothing would notice until the
#: Phase 5 runner met it — at which point the runner would have to invent a policy for malformed
#: data, which is how two components end up disagreeing about what "valid" means.
#:
#: `(required, optional)`. Anything outside their union is **rejected, not ignored**: an ignored
#: key is how a typo becomes a probe that silently checks the wrong thing, and how a future field
#: arrives having never been honoured with nobody noticing.
#:
#: Applied at EVERY boundary where a probe becomes durable — the operator's own
#: `PATCH /objectives`, a playbook write, playbook read-time normalization, and instantiation.
#: One schema rather than one per path, because two validity rules is how one of them ends up
#: weaker than the other.
#: The value contracts an argument may satisfy. A NAME-ONLY schema is not a schema: it accepted
#: `http_status.probe_args.url` as a LIST and `expect_status` as an object, both of which persist
#: fine and then hand the Phase 5 runner something it cannot probe (#883 review).
#:
#: `bool` is excluded from `status` deliberately — `isinstance(True, int)` is `True` in Python, so
#: an unstated int check silently admits `True` as a status code. That trap already bit this repo
#: once, in `termSize`'s prefs normalization.
PROBE_ARG_TEXT_MAX = 200
PROBE_ARG_URL_MAX = 512
#: Hostname charset, conservative on purpose: letters/digits/`-`/`.` for DNS names, plus `:` and
#: hex for an IPv6 literal (`urlsplit` strips the brackets) and `%` for a zone id. An
#: internationalized domain reaches here as punycode.
_ARG_HOST_RE = re.compile(r"^[a-z0-9._\-:%]+$", re.IGNORECASE)


def _arg_text(kind: str, name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise MissionError(f"probe {kind}: {name} must be a non-empty string", status=422)
    if len(value) > PROBE_ARG_TEXT_MAX:
        raise MissionError(
            f"probe {kind}: {name} is longer than {PROBE_ARG_TEXT_MAX} characters", status=422
        )
    if any(ch in value for ch in "\r\n\t\x00"):
        raise MissionError(f"probe {kind}: {name} may not contain control characters", status=422)


def _arg_url(kind: str, name: str, value: object) -> None:
    """A probe target is an ADDRESS THE SERVER WILL FETCH, so its shape is checked where it is
    written rather than where it is used.

    The scheme requirement is not cosmetic: `http_status` and `http_revision` are the only two
    kinds that make a request, and a stored `file:///etc/shadow` or `gopher://…` would be a
    request of a kind the operator did not think they were configuring. This is NOT a defence
    against a hostile author — the operator authoring a playbook already has the authority to
    point a probe wherever they like, exactly as they do with `ai_review.base_url`. It is the
    contract that keeps a typo from becoming a different protocol.
    """
    if not isinstance(value, str) or not value.strip():
        raise MissionError(f"probe {kind}: {name} must be a non-empty string", status=422)
    if len(value) > PROBE_ARG_URL_MAX:
        raise MissionError(
            f"probe {kind}: {name} is longer than {PROBE_ARG_URL_MAX} characters", status=422
        )
    # PARSED, not pattern-matched. The regex accepted `http://?x`, `https://#frag` and
    # `http://:80` — all hostless, all storable, and all failing only when the Phase 5 runner
    # tried to fetch them, which is exactly the "well-formed at the write boundary" contract this
    # is supposed to be (review on #884).
    try:
        parts = urlsplit(value)
    except ValueError as e:
        raise MissionError(f"probe {kind}: {name} is not a URL ({e})", status=422) from None
    if parts.scheme not in ("http", "https"):
        raise MissionError(f"probe {kind}: {name} must be an http:// or https:// URL", status=422)
    try:
        host = parts.hostname
    except ValueError as e:  # an authority the parser itself refuses
        raise MissionError(f"probe {kind}: {name} has an invalid host ({e})", status=422) from None
    if not host:
        raise MissionError(f"probe {kind}: {name} has no host", status=422)
    # A URL never legitimately carries raw whitespace or control characters, and `urlsplit` is
    # happy to hand back `'  '` as a hostname — so `https://  ` and `http:// /x` both survived
    # the scheme and non-empty checks above.
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise MissionError(
            f"probe {kind}: {name} may not contain whitespace or control characters", status=422
        )
    if not _ARG_HOST_RE.match(host):
        raise MissionError(f"probe {kind}: {name} has an invalid host {host!r}", status=422)
    # `urlsplit` parses the port LAZILY — `parts.port` is what raises, so a URL with `:abc` for a
    # port sails through everything above and only fails when `httpx.Request` refuses to build it.
    # Same write-time well-formedness contract as the hostless cases, not runner policy.
    try:
        port = parts.port
    except ValueError as e:
        raise MissionError(f"probe {kind}: {name} has an invalid port ({e})", status=422) from None
    if port is not None and not (1 <= port <= 65535):
        raise MissionError(f"probe {kind}: {name} has a port outside 1-65535", status=422)


def _arg_status(kind: str, name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MissionError(f"probe {kind}: {name} must be an integer", status=422)
    if not (100 <= value <= 599):
        raise MissionError(f"probe {kind}: {name} must be an HTTP status 100-599", status=422)


#: name -> (required?, value contract)
_ArgSpec = dict[str, tuple[bool, object]]

#: WHAT A WELL-FORMED ARGUMENT SET LOOKS LIKE, per probe kind (#883).
#:
#: `PROBE_KINDS` says which probes exist; this says what each one needs, AND what each thing it
#: needs may be. One table, used at every boundary where a probe becomes durable — the public
#: PATCH route, playbook writes, read-time normalization and instantiation — because two notions
#: of validity is how one of them ends up weaker.
PROBE_ARG_SCHEMA: dict[str, _ArgSpec] = {
    # Nothing to configure: the operator settles these by hand, or the agent claims them.
    "none": {},
    "agent_judged": {},
    # The mission's own checkout answers these; `branch` narrows it when the objective is about
    # a specific one rather than whatever the mission is on.
    "git_local": {"branch": (False, _arg_text)},
    # Forge probes address a repository — but `repo` is OPTIONAL, because the mission's own
    # checkout already names one. The server resolving it from the mission's remote is both the
    # "server resolves the entity" rule and one fewer target an operator can typo; naming it
    # explicitly is for the case where the objective is about a DIFFERENT repository than the one
    # the work happens in.
    "forge_pr": {"repo": (False, _arg_text), "branch": (False, _arg_text)},
    "forge_checks": {"repo": (False, _arg_text), "branch": (False, _arg_text)},
    "forge_review": {"repo": (False, _arg_text), "branch": (False, _arg_text)},
    "forge_merged": {"repo": (False, _arg_text), "branch": (False, _arg_text)},
    "forge_run": {
        "repo": (False, _arg_text),
        "workflow": (False, _arg_text),
        "branch": (False, _arg_text),
    },
    # The two that make a REQUEST, and the only two with a REQUIRED argument. There is no
    # server-derivable default URL — that is the entire point: a probe target exists only because
    # a human typed it. A URL-less `http_status` would be a live probe with nothing to check, so
    # it cannot be stored at all.
    "http_status": {"url": (True, _arg_url), "expect_status": (False, _arg_status)},
    # `expect` is OPTIONAL again, and this time the fallback has a producer (#897 re-review 5,
    # finding 7). #891's `change_live` contract is "is THIS revision live", and the revision is
    # normally the merge SHA — which a static playbook cannot name, because it does not exist
    # when the playbook is written. `forge_merged` observes one and `note_merge_sha` records it,
    # so the marker is `expect` when the operator supplied one and the mission's own merge SHA
    # otherwise. With neither, the probe answers `unknown` and says which is missing — it never
    # reports "live" from a status code, which is the claim it exists to refuse.
    "http_revision": {"url": (True, _arg_url), "expect": (False, _arg_text)},
}
#: Every kind must say what it takes. A kind added to `PROBE_KINDS` without a schema entry would
#: otherwise be validated by nothing at all — the silent-widening failure this table exists to
#: prevent — so the two are asserted equal at import.
assert set(PROBE_ARG_SCHEMA) == set(PROBE_KINDS), (
    "PROBE_ARG_SCHEMA must cover exactly PROBE_KINDS; missing: "
    f"{sorted(set(PROBE_KINDS) - set(PROBE_ARG_SCHEMA))}"
)

#: The WIRE NAME of each argument contract, so an editor can send the right JSON TYPE.
#:
#: Names alone are not enough to author an argument, and `http_status.expect_status` is the proof:
#: every input in an HTML form yields a string, `_arg_status` requires a real integer, and the
#: playbook that a perfectly reasonable operator typed was refused with "must be an integer" and
#: no way to comply (#900 review, finding 6). The type belongs beside the contract that enforces
#: it — one table, exported to the client, rather than a second opinion in TypeScript.
#:
#: `"text"` is the only value that means "send a string"; everything else names a JSON type the
#: client has to produce deliberately.
_ARG_TYPE_NAME: dict[object, str] = {
    _arg_text: "text",
    _arg_url: "text",
    _arg_status: "int",
}

PROBE_ARG_TYPES: dict[str, dict[str, str]] = {
    kind: {name: _ARG_TYPE_NAME.get(fn, "text") for name, (_req, fn) in spec.items()}
    for kind, spec in PROBE_ARG_SCHEMA.items()
}

#: Keys the INSTANTIATOR mints for model-proposed notes (#883). A playbook objective key may not
#: use this prefix — rejected at prefs-write time — which is what makes a note/template collision
#: UNREACHABLE rather than merely detected. It lives here, with the other objective-key rules,
#: because it is a fact about keys rather than about prefs storage.
NOTE_KEY_PREFIX = "note_"

OBJECTIVE_STATES: frozenset[str] = frozenset({"pending", "active", "met", "failed", "waived"})
OBJECTIVE_SOURCES: frozenset[str] = frozenset({"playbook", "model", "operator"})
#: Objective states that do NOT count as satisfied for the "adding a gate reopens `review`" rule.
_UNMET_OBJECTIVE_STATES: frozenset[str] = frozenset({"pending", "active", "failed"})

SESSION_ROLES: frozenset[str] = frozenset({"primary", "sub"})
#: ``skipped`` = the session was released and is now held by another OPEN mission, so tearing
#: it down would kill that mission's agent. Named rather than silently omitted.
ARCHIVE_STATES: frozenset[str] = frozenset(
    {
        "pending",
        "in_progress",
        "done",
        "already_archived",
        "failed",
        "skipped",
        "restoring",
        "restore_failed",
    }
)
#: `in_progress` is a **lease**: exactly one worker may run a session's external teardown.
#: `begin_archive` hands the same pending rows to a retry, and `prov.archive` is not idempotent
#: in general, so without a lease a concurrent (or retried) request performs the destructive
#: effect twice. A lease is re-opened only once it has gone :data:`LEASE_MAX_AGE_S` without a
#: heartbeat — never because it belongs to another process. "Another process" is not "a dead
#: process": this app supports more than one instance over the same store (its single-writer
#: session lock is explicitly cross-instance), so an owner-epoch test cannot tell a crashed worker
#: from a live sibling, and reclaiming on one put two workers inside the same irreversible effect.


# ---------------------------------------------------------------- errors


class MissionError(RuntimeError):
    """A refusal with the HTTP status the route layer passes through.

    The message is operator-facing and **never** carries mission content — it names the mission
    id and the failure kind, because ``instruction`` / ``brief`` are sensitive.
    """

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class MissionsBusy(MissionError):
    """Admission bound saturated. 503 — an honest "try again", never a silent queue."""

    def __init__(self, message: str = "the missions store is busy; try again") -> None:
        super().__init__(message, status=503)


class MissionNotFound(MissionError):
    def __init__(self, mission_id: str) -> None:
        super().__init__(f"unknown mission {mission_id}", status=404)


class SessionHeld(MissionError):
    """A session is already owned by another OPEN mission. 409, naming the holder."""

    def __init__(self, session_key: str, holder: str) -> None:
        super().__init__(f"session {session_key} is already held by mission {holder}", status=409)
        self.holder = holder


# ---------------------------------------------------------------- path + connection


def _db_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_MISSIONS_DB",
            str(Path.home() / ".config" / "agent-sessions" / "missions.db"),
        )
    )


def _retention_days() -> int:
    raw = os.environ.get("AGENT_SESSIONS_MISSIONS_RETENTION_DAYS")
    if raw is None:
        return MISSION_RETENTION_DAYS
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return MISSION_RETENTION_DAYS


def _touch_0600(p: Path) -> None:
    """Create the file 0600 **before** sqlite3 can create it under the process umask.

    ``sqlite3.connect`` creates a fresh DB with 0644 on a default umask, and a chmod afterwards
    leaves a window in which the operator's instructions were world-readable. Opening with
    ``O_CREAT|O_EXCL`` at 0600 first closes it; an existing file is left alone.
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    os.close(fd)


def _connect(path: Path | None = None, *, busy_timeout_ms: int = _BUSY_TIMEOUT_MS):
    p = path or _db_path()
    _touch_0600(p)
    con = sqlite3.connect(str(p), timeout=busy_timeout_ms / 1000.0, isolation_level=None)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=FULL")
    con.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    con.execute("PRAGMA foreign_keys=ON")
    # Deleting a mission has to actually remove the bytes, not just unlink the row: `instruction`
    # and `brief` are verbatim operator text and may hold a token. Without this, a freed page
    # keeps its old content until something reuses it.
    #
    # **Set explicitly because the default is a build option, not a promise.** Debian/Ubuntu
    # compile libsqlite3 with `SQLITE_SECURE_DELETE=1`, so on this host — and therefore in CI,
    # which runs on it — the property held by accident and no test could see it missing. On a
    # build without it the plaintext survives every delete. A security property that depends on
    # which libsqlite3 happens to be linked is not a property.
    con.execute("PRAGMA secure_delete=ON")
    return con


_SCHEMA = """
CREATE TABLE IF NOT EXISTS missions (
  id            TEXT PRIMARY KEY,
  title         TEXT NOT NULL,
  instruction   TEXT NOT NULL,
  brief         TEXT,
  project_id    TEXT,
  cwd           TEXT,
  engine        TEXT,
  engine_source TEXT,
  state         TEXT NOT NULL,
  playbook_id   TEXT,
  created_at    REAL,
  updated_at    REAL,
  closed_at     REAL,
  archived_at   REAL,
  archiving_at  REAL,
  unarchiving_at REAL,
  -- What the operator asked for, stored WITH the claim. Recovery that has to guess whether the
  -- sessions were meant to be restored is not recovering the operation, it is performing a
  -- different one — `sessions=False` crashed and came back with the sessions unarchived anyway.
  unarchive_sessions INTEGER,
  -- The OWNER of the in-flight archive/unarchive. Per-session leases stop two workers tearing
  -- down the same session; they do not stop a second caller walking in, seeing an empty pending
  -- list (because the first worker already leased every row) and finalising the operation while
  -- that worker is still inside `cleanup_runtime`. The operation needs an owner too.
  op_token      TEXT,
  -- Objective production is a DURABLE intent (#883), stamped in the same transaction that
  -- creates the mission. A background task lives only in this process; this is what lets
  -- recovery find a checklist that was never filled. NULL = predates the producer, never
  -- retried.
  objectives_state TEXT,
  objectives_at    REAL,
  outcome       TEXT,
  -- THE PROBE GENERATION SOURCE, and it is per MISSION rather than per objective (#897
  -- re-review, finding 2). A counter living on the objective row restarts at zero when that row
  -- is dropped and re-added, so an answer issued for the old incarnation matched the new one on
  -- every check — the "an index is not an identity" family, one level down: a per-row counter is
  -- an index into a row's history, and the row is a slot that can hold a different question a
  -- second later. A mission-scoped monotonic value survives row replacement, so a generation is
  -- never reused within a mission and a stale answer can never match.
  --
  -- LAST in the column list: `ALTER TABLE ... ADD COLUMN` appends, so this is where a fresh
  -- install and an upgraded one agree on the stored DDL.
  probe_gen_seq INTEGER NOT NULL DEFAULT 0,
  -- THE MERGE SHA, once something observed one. #891 asks `http_revision` to bind to it — "is
  -- THIS revision live" needs a revision to look for, and a static playbook cannot know a SHA
  -- that does not exist yet. Written by the probe runner when `forge_merged` observes a merge,
  -- and WRITE-ONCE: a merge commit does not change, so a second value would mean the row is
  -- about a different merge and the objective is about a different question.
  merge_sha     TEXT,
  -- THE PLANNING INTENT (#967), durable for the same reason `objectives_state` is: a background
  -- planner lives only in the process that started it. `pending | ready | failed | skipped`,
  -- stamped `pending` in the transaction that creates the mission.
  --
  -- `plan_generation` names the ATTEMPT. Every intent (create, Plan again, an operator's save)
  -- takes a new one, and every settlement is a compare-and-set on it, so a late planner can never
  -- write over a newer attempt or over the operator. `mission_plans.generation` records which
  -- attempt produced the stored plan, which is what lets recovery tell "the result landed" from
  -- "the previous plan is still here".
  --
  -- Appended after `merge_sha`, because `ALTER TABLE ... ADD COLUMN` appends and a fresh install
  -- and an upgraded one should agree on the column order.
  plan_state      TEXT,
  plan_generation INTEGER NOT NULL DEFAULT 0,
  plan_at         REAL,
  -- Why the latest attempt is `failed` or `skipped`, for the plan card. The timeline is a capped
  -- feed, so the reason lives on the row as well as in an event.
  plan_detail     TEXT,
  -- A draft may exist before its project is resolved (that is the whole point of asking), but
  -- nothing may LAUNCH without a server-resolved cwd. Enforced here, not in a comment.
  CHECK (state IN ('draft','planned','abandoned') OR cwd IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS mission_sessions (
  mission_id    TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  session_key   TEXT NOT NULL,
  role          TEXT NOT NULL,
  spawned_by    TEXT,
  added_at      REAL,
  removed_at    REAL,
  -- WHY the session left the roster, which archive has to be able to tell apart. Reaching a
  -- terminal state releases ownership ('closed') and those sessions are still the mission's to
  -- reap; an operator DETACHING one ('detached') is the operator saying "this is not part of
  -- this mission any more", and archiving must not cross that line. Both set `removed_at`, so
  -- the timestamp alone cannot distinguish them — and archiving the roster by timestamp
  -- terminated work the operator had deliberately removed.
  release_reason TEXT,
  archive_state TEXT,
  archive_error TEXT,
  -- WHICH PROCESS holds the lease. Recovery reopens a lease on the premise that it can only
  -- belong to a worker that died — true at boot, and false the moment the app is also serving
  -- requests, which it is: recovery runs as a background task while routes are live. Stamping
  -- the process identity makes the premise checkable instead of assumed.
  lease_owner   TEXT,
  -- WHEN the lease was taken. Ownership alone is not enough: the write that releases a lease can
  -- itself fail, and a lease stamped with the CURRENT process is one recovery refuses to touch —
  -- so a transient failure at exactly the wrong moment stranded the row until a restart.
  lease_at      REAL,
  -- The fencing token of the reservation this lease holds. Settlement must present it, so a
  -- worker whose lease was reclaimed cannot settle the operation that replaced it.
  lease_token   TEXT,
  PRIMARY KEY (mission_id, session_key)
);

-- WHERE A SESSION'S RUNTIME LIVES, when that is not its own key (#989; #994 review 3).
--
-- A late-id engine is launched under a `<engine>:new-<uuid>` placeholder and adopted under the
-- real id it revealed afterwards; its master, lock and ring stay under the placeholder for life.
--
-- **Deliberately NOT part of mission history**, which is what the first version got wrong. The
-- mapping lived on `mission_sessions`, whose rows cascade when retention deletes a closed
-- mission — so a session whose alias publication had failed lost the only record of where it
-- runs while its agent was still running, and nothing could repair it afterwards. This is a fact
-- about a RUNTIME, so its lifetime is the runtime's.
--
-- **APPEND-ONLY**: written in the adopting transaction and never deleted (#994 review 5). A proved
-- stop is not proof that a mapping may go — `cleanup_runtime` spares the socket while the physical
-- key's launch lock is HELD, because a NEW generation owns it, yet still reports the OLD master's
-- result; and callers that resolved the mapping before a teardown began sit outside its
-- transaction anyway. The alias this projects has no deleter either (`metadata.set_alias`).
-- Retiring one safely needs a generation-safe protocol: #1017.
CREATE TABLE IF NOT EXISTS session_runtime_bindings (
  logical_key  TEXT PRIMARY KEY,
  physical_key TEXT NOT NULL,
  bound_at     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_objectives (
  mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  key        TEXT NOT NULL,
  ord        INTEGER NOT NULL,
  title      TEXT NOT NULL,
  probe      TEXT NOT NULL,
  probe_args TEXT,
  gate       INTEGER NOT NULL,
  state      TEXT NOT NULL,
  met_at     REAL,
  observed   TEXT,
  source     TEXT NOT NULL,
  -- THE IN-FLIGHT PROBE BINDING (#897 re-review, finding 2). `probe_target` is the fully
  -- resolved destination digest the current request was issued against — forge config, checkout,
  -- derived repo/branch/remote and local HEAD, none of which live in `probe_args`. `probe_gen`
  -- is bumped on every bind, so two runners racing the same row settle at most one answer and a
  -- rebind invalidates an older request that is still in flight. Compared inside the settling
  -- transaction, which is what makes the fence atomic rather than a pre/post pair in a caller.
  probe_target TEXT,
  probe_gen    INTEGER NOT NULL DEFAULT 0,
  -- The forge-configuration revision the in-flight probe was bound at. Validated INSIDE the
  -- settling transaction against the store's own counter, which is what closes the window a
  -- pre-write digest comparison cannot (#897 re-review 5, finding 1).
  probe_rev    INTEGER NOT NULL DEFAULT 0,
  -- THE INCARNATION. `(mission_id, key)` is a SLOT, not an identity: drop the key and re-add it
  -- and the new row is a different question wearing the same name. Anything that spends real
  -- time deciding something ABOUT an objective — a model call, an in-flight probe — has to be
  -- able to say "the row I was asked about" rather than "a row with that key", and this is the
  -- value it compares (#900 review, finding 4).
  --
  -- Minted on insert and never rewritten. LAST in the column list, because `ALTER TABLE …
  -- ADD COLUMN` appends and a fresh install must agree with an upgraded one on the stored DDL.
  incarnation TEXT,
  -- THE OPERATOR'S DIRECTION (#983): the text a supervisor nudge for this objective types, with
  -- placeholders filled only from this row's own observation and probe arguments. COPIED from the
  -- playbook template when the objective is created (`template`) or written for this mission
  -- (`operator`), and never live-linked, so a later playbook edit changes no running mission. A
  -- model-created objective has none. NULL for every row that predates v27. Appended after
  -- `incarnation`, for the same column-order reason.
  direction        TEXT,
  direction_source TEXT,
  PRIMARY KEY (mission_id, key)
);

CREATE TABLE IF NOT EXISTS supervisor_state (
  -- Small durable key/value for the supervisor LOOP itself, as opposed to any one mission.
  -- Today it holds one row: the sweep cursor. In process memory that cursor reset on every
  -- restart, and a service that restarts before finishing a revolution re-selects the lowest ids
  -- forever while the tail is never reached — fairness that a restart silently erases is not
  -- fairness (#888 review, finding 6).
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS mission_supervisor (
  -- One checkpoint per (mission, SESSION). `input_fp` and `recap_seq` advance in ONE transaction:
  -- a recap written without moving the fingerprint is re-written on the next pass, and a
  -- fingerprint moved without the recap loses it.
  --
  -- Keyed by session, not by mission, and that is a correctness requirement rather than a
  -- refinement. A mission can hold several sessions and each is read separately, so one row per
  -- mission makes two sessions fight over it: A writes its fingerprint, B overwrites it, and A —
  -- which has not changed — is charged another model call on the next sweep. Worse in the other
  -- direction, two sessions that happen to produce the SAME fingerprint suppress the second one's
  -- first recap entirely.
  mission_id  TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  session_key TEXT NOT NULL,
  input_fp    TEXT,
  recap_seq   INTEGER,
  -- The GROWTH baseline for stall detection: a monotonic mark for how much this session had
  -- written, and when that was last true.
  --
  -- "Has written nothing at all" is the wrong test — a session that writes one startup turn and
  -- then hangs on a trust dialog is exactly the case the detector exists for, so what matters is
  -- whether the mark MOVES. And the mark is the transcript store's size where one can be located,
  -- not a rendered turn count: the renderers cap at `DEFAULT_MAX_MESSAGES`, so a busy session
  -- pinned at the cap has a count that stops moving while the session is perfectly healthy, and
  -- would read as stalled forever (#888 review).
  growth_mark INTEGER,
  growth_at   REAL,
  updated_at  REAL NOT NULL,
  PRIMARY KEY (mission_id, session_key)
);

CREATE TABLE IF NOT EXISTS mission_objective_episode (
  -- THE RESET BOUNDARY, and it is durable. The budget resets only when an objective's own state
  -- changes — never on input churn — so "delivered, then progress, then a later stall" starts
  -- counting at zero. A derived rule cannot express that: re-deriving after progress would still
  -- see the earlier nudges.
  mission_id    TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  objective_key TEXT NOT NULL,
  episode       INTEGER NOT NULL,
  -- The per-episode "Stop telling me". It silences THIS episode; it does not mark the objective
  -- met, and an objective transition starts a new episode, which ends the silence.
  stood_down    INTEGER NOT NULL DEFAULT 0,
  at            REAL NOT NULL,
  -- THE QUESTION HOLD, and it is a SEPARATE cause from `stood_down` above (#892, and the issue
  -- review said so before the code existed). `stood_down` means the OPERATOR chose silence; this
  -- means the SUPERVISOR is waiting on an answer. Folding the second into the first would make
  -- answering a question erase a manual silence the operator had also set, and would leave the
  -- board unable to say which of the two reasons an objective is quiet for.
  --
  -- It holds the `seq` of the question, not a boolean, which is what makes the lifecycle exact:
  -- a second question on the same objective SUPERSEDES by overwriting it, an answer clears it
  -- only when it names the question that is actually holding, and "is there an open question"
  -- stops being a comparison of two global MAX(seq) values that cannot express either.
  --
  -- LAST in the column list, deliberately: `ALTER TABLE ... ADD COLUMN` appends, so a fresh
  -- install and an upgraded one only agree on the stored DDL if the create says it here. That
  -- equality is asserted by a test, which is how this was caught.
  question_seq  INTEGER,
  PRIMARY KEY (mission_id, objective_key)
);

CREATE TABLE IF NOT EXISTS mission_supervisor_actions (
  -- The durable binding the budget is DERIVED from. No counter anywhere: a count incremented at
  -- send time is wrong on both sides of a crash — incremented before the append and the action
  -- never lands, incremented after and the crash loses the charge. Recording WHICH action was
  -- sent lets the charge be read back from the ledger's terminal state, so a restart after
  -- either side of the write yields the same number.
  mission_id    TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  session_key   TEXT NOT NULL,
  objective_key TEXT NOT NULL,
  episode       INTEGER NOT NULL,
  action_id     TEXT NOT NULL,
  at            REAL NOT NULL,
  PRIMARY KEY (mission_id, action_id)
);

CREATE TABLE IF NOT EXISTS mission_escalations (
  -- EXACTLY ONE terminal escalation per objective episode, enforced by the database rather than
  -- by a check. Two overlapping passes both reading "not escalated yet" and both writing is the
  -- shape this uniqueness constraint exists to make unreachable.
  mission_id    TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  session_key   TEXT NOT NULL,
  objective_key TEXT NOT NULL,
  episode       INTEGER NOT NULL,
  reason        TEXT NOT NULL,
  at            REAL NOT NULL,
  -- OBJECTIVE-level, deliberately: `session_key` is recorded but is NOT part of the key.
  --
  -- The budget this escalation reports on is objective-level — `budget_state(mission, objective)`
  -- takes no session — so an escalation keyed per session contradicts the very number it quotes.
  -- With the pass now visiting every held session, one exhausted objective on a two-session
  -- mission produced two escalation rows and two bell notifications for one situation.
  UNIQUE (mission_id, objective_key, episode)
);

CREATE TABLE IF NOT EXISTS mission_events (
  seq         INTEGER PRIMARY KEY AUTOINCREMENT,
  mission_id  TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  at          REAL NOT NULL,
  kind        TEXT NOT NULL,
  session_key TEXT,
  action_id   TEXT,
  text        TEXT,
  meta        TEXT
);

-- The immutable settlement projection (#840 §2), in its OWN table rather than a column on the
-- event.
--
-- It lived on the event first, and that made two contracts fight: the timeline is a *bounded
-- feed* (something has to cap it, or one operator retitling an objective in a loop grows it
-- forever), while the projection is a *durability promise* (an old mission must still render its
-- decisions after the ledger has compacted the row away). With the projection stored on the row,
-- bounding the feed necessarily deleted the promise — ordering projection-bearing rows last only
-- delayed it.
--
-- Keyed on `action_id` and owned by no mission, so trimming the feed cannot touch it. Written
-- ONCE, at settlement (`INSERT OR IGNORE`), and never updated. Orphans are collected when the
-- last event referencing them goes, because `rationale` is bounded model text about the
-- operator's work and is deleted with the mission like everything else here.
-- WHO currently has the right to mutate a session's PROVIDER state — move its transcript, or
-- terminate the process group behind it. One row per session key, so the mission teardown path and
-- the sibling session routes exclude each other instead of merely checking each other.
--
-- A read-only guard cannot close this: it answers about the past, and the caller then acts. The
-- reservation IS the act. `token` is a fencing token — a reclaimed reservation mints a new one, so
-- a worker whose lease expired can no longer settle the operation that replaced it.
CREATE TABLE IF NOT EXISTS session_reservations (
  session_key TEXT PRIMARY KEY,
  token       TEXT NOT NULL,
  holder      TEXT NOT NULL,
  at          REAL NOT NULL
);

-- One operator chat turn, as a DURABLE CLAIM rather than a pair of ordinary timeline events
-- (#852). `/message` spans two stores — this one and the orchestrator ledger — so ordering alone
-- cannot make it idempotent: two concurrent requests with the same new `turn_id` would both pass
-- a "no prior turn" check and both call the model, and a crash after the ledger append but before
-- the result event would leave a replay with nothing to return, so it would ask again.
--
-- The INSERT is the claim, so a duplicate loses at the database. `msg_sha` binds the key to the
-- content, so the same id with different text is a different turn rather than a replay. `owner`
-- is renewed while the request runs, so its expiry means the owner STOPPED rather than that it is
-- slow; `fence` is minted on every ownership change, so a holder that turns out not to be dead
-- cannot land its result over the recovery that replaced it.
--
-- `write_reserved_at` is the receipt: stamped under the fence in a COMMITTED transaction BEFORE
-- the ledger append, so recovery reads evidence instead of inferring from timing. Set means a
-- write may have landed (reconcile by provenance, never re-ask); null positively means nothing
-- was ever appended, which is the only state permitting the single fenced re-entry.
CREATE TABLE IF NOT EXISTS mission_turns (
  mission_id        TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  turn_id           TEXT NOT NULL,
  msg_sha           TEXT NOT NULL,
  state             TEXT NOT NULL CHECK (state IN ('in_progress','done','indeterminate')),
  owner             TEXT,
  owner_at          REAL,
  fence             TEXT NOT NULL,
  write_reserved_at REAL,
  result            TEXT,
  result_meta       TEXT,
  operator_seq      INTEGER,
  assistant_seq     INTEGER,
  action_ids        TEXT,
  created_at        REAL NOT NULL,
  settled_at        REAL,
  -- The operator has SEEN this turn's outcome and dismissed it. Only meaningful for a turn the
  -- console would otherwise keep showing for ever — an `indeterminate` one, which is terminal
  -- and unresolvable by the server (#903-era review of #902, finding 1). New columns go LAST,
  -- because the DDL of a fresh store and an upgraded one are asserted equal.
  acked_at          REAL,
  -- THE OPERATOR'S MESSAGE, bounded, stored WITH the turn (#902 review 2, finding 2).
  --
  -- It used to be reached by joining to the operator's timeline event, and the timeline is a
  -- capped FEED: past the soft cap the event goes and the open turn comes back with no text at
  -- all. After a reload the question is missing, and CHECK AGAIN on an ambiguous turn resends an
  -- empty message and gets a 422 instead of replaying the stable id.
  --
  -- The turn is durable state; a feed row is not a place to keep durable state. `msg_sha` stays
  -- because it is what the idempotency check compares — this is for display and for the replay.
  message           TEXT,
  PRIMARY KEY (mission_id, turn_id)
);

-- Durable obligations that outlive one call. Today: `scrub_pending`, set when a WAL truncate
-- came back busy so the plaintext of a deleted mission is still on disk. Without it the advertised
-- "retry" was a no-op — the rows are already gone, so the next delete returns early and never
-- reaches the scrub.
CREATE TABLE IF NOT EXISTS store_flags (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- THE DISPATCH PROPOSAL (#893, Phase 4 of #840). One row per mission: a re-plan SUPERSEDES the
-- previous proposal rather than stacking, because two live plans for one mission is a state the
-- operator cannot act on coherently and the newer one is the one they are looking at.
--
-- `plan_id` is the IDENTITY dispatch compares against, and it is why this is a row rather than an
-- event: DISPATCH must run the plan the operator SAW. A model call sits between `/plan` and the
-- button, the project list can move under it, and "the mission's current plan" is a slot, not a
-- plan. The operator sends the id back and a mismatch is a 409.
--
-- The cwd is stored RESOLVED, server-side, from a project id — the same rule `POST /api/missions`
-- follows, and the reason no model-authored path can reach a launch argument.
CREATE TABLE IF NOT EXISTS mission_plans (
  mission_id    TEXT PRIMARY KEY REFERENCES missions(id) ON DELETE CASCADE,
  plan_id       TEXT NOT NULL,
  project_id    TEXT,
  cwd           TEXT,
  engine        TEXT,
  engine_reason TEXT,
  brief         TEXT NOT NULL,
  created_at    REAL NOT NULL,
  -- The planning attempt that produced this row (#967) — see `missions.plan_generation`.
  generation    INTEGER NOT NULL DEFAULT 0
);
-- THE DISPATCH IN FLIGHT (#904 review 2). `dispatching` is a promise the process makes and a
-- process can die; the plan row is consumed by the claim, so without this there is nothing left
-- on disk that says a launch was ever attempted — and a mission is left `dispatching` for ever,
-- possibly beside a live unattended agent nobody owns.
--
-- Written in the SAME transaction as the claim, so the intent is durable before anything is
-- spawned, and stamped with `session_key` the moment the key is minted and before the master
-- exists. That ordering is the whole value: the record can be ahead of reality (a key that never
-- launched) and never behind it (a launch with no record).
CREATE TABLE IF NOT EXISTS mission_dispatches (
  mission_id  TEXT PRIMARY KEY REFERENCES missions(id) ON DELETE CASCADE,
  plan_id     TEXT NOT NULL,
  engine      TEXT NOT NULL,
  cwd         TEXT NOT NULL,
  session_key TEXT,
  started_at  REAL NOT NULL,
  -- THE WHOLE PROPOSAL, not just what the launch needs. The claim DELETES the plan row, so this
  -- is the only copy while the dispatch is in flight — and a refusal that spawned nothing has to
  -- be able to put it back verbatim rather than leave the operator with a consumed plan and a
  -- mission they cannot re-plan (#904 review 2, finding 7).
  project_id    TEXT,
  engine_reason TEXT,
  brief         TEXT,
  -- WHO IS DRIVING THIS DISPATCH (#904 review 2, finding 3). `dispatching` alone says a launch is
  -- somewhere between claimed and settled; it does not say whether the process doing it still
  -- exists. Startup recovery ran against every such row, so a request that was actively
  -- launching — in this instance or a sibling over the same store — was snapshotted as crashed
  -- and torn down. `pid:starttime`, both read from `/proc`, so the comparison is between two
  -- readings of the same kernel fact rather than between a fact and a memory of one.
  owner         TEXT,
  -- WHOSE SUB-AGENT THIS IS, or NULL for the mission's own launch (#894).
  --
  -- A spawn IS a dispatch — same claim, same launch fence, same settlement — so it gets no second
  -- launcher and no second table. That is not tidiness: this table is keyed by `mission_id`, so
  -- one in-flight launch per mission falls out of the PRIMARY KEY, and the sub-agent CAP can then
  -- be enforced inside the very transaction that reserves the slot. Two concurrent approvals
  -- produce exactly one agent rather than two that each read a cap that was true when they read
  -- it.
  --
  -- It carries the PARENT'S session key because the settlement is what adopts, and a sub-agent
  -- that lands in the roster without saying whose it is cannot afterwards be told from the
  -- mission's own session.
  spawn_parent  TEXT,
  -- WHAT THE LAUNCH TYPED, and whether its teardown was proved (#966). NULL is `unknown`: the
  -- claim writes nothing here, so a crash anywhere before the dispatcher records the answer
  -- leaves no evidence rather than a guess. Recorded BEFORE the settlement, which copies it.
  seed_outcome  TEXT,
  teardown_confirmed INTEGER NOT NULL DEFAULT 0,
  -- THE ATTEMPT'S NONCE, for a late-id engine (#989). Minted per attempt, recorded with the
  -- placeholder key before the spawn and delivered as the brief's last line; the session whose
  -- first turn carries it is the one this attempt became. NULL for a pinned-id engine.
  attempt_nonce TEXT
);
CREATE TABLE IF NOT EXISTS mission_settlements (
  action_id TEXT PRIMARY KEY,
  verb      TEXT,
  state     TEXT,
  rationale TEXT,
  outcome   TEXT,
  at        REAL
);

-- One session may be held by at most one OPEN mission. "Check then insert" cannot be made
-- race-safe in application code, so the database is the arbiter and the loser gets a 409.
CREATE UNIQUE INDEX IF NOT EXISTS mission_sessions_active
  ON mission_sessions(session_key) WHERE removed_at IS NULL;
CREATE INDEX IF NOT EXISTS mission_objectives_by_mission
  ON mission_objectives(mission_id, ord);
CREATE INDEX IF NOT EXISTS mission_events_by_mission
  ON mission_events(mission_id, seq);
CREATE INDEX IF NOT EXISTS missions_by_state
  ON missions(state, archived_at, updated_at DESC);
-- The rail's own query is scope + recency with NO state predicate, so `missions_by_state` (which
-- leads with `state`) cannot serve it. This one can.
CREATE INDEX IF NOT EXISTS missions_by_archived
  ON missions(archived_at, updated_at DESC);
CREATE INDEX IF NOT EXISTS mission_events_by_action
  ON mission_events(action_id) WHERE action_id IS NOT NULL;
"""


MISSION_SPAWNS_DDL = """
-- WHAT THIS MISSION STARTED THAT NOBODY HAS PROVEN STOPPED (#894 review 1, finding 4).
--
-- The resource ledger, kept deliberately apart from `mission_sessions`. Membership answers "does
-- this mission claim this session"; that is an ownership question, it is mutable on purpose, and
-- it is NOT the same question as "is there a process on this host because of this mission". The
-- cap counted membership and was therefore evadable by RELEASING a child (which stops nothing)
-- or by RE-ADOPTING it (which rewrites `spawned_by`). Neither touches this table.
--
-- `ended_at` is stamped by EVIDENCE ONLY: a launch that provably started nothing, or a process
-- observed dead. "We could not look" leaves the row open and the slot held — the conservative
-- direction, because the cost of holding a slot too long is a refused spawn and the cost of
-- freeing one too early is an unbounded fan-out of live agents.
--
-- Keyed on `plan_id`: the slot is reserved inside `claim_spawn`, before any session key exists.
CREATE TABLE IF NOT EXISTS mission_spawns (
  plan_id     TEXT PRIMARY KEY,
  mission_id  TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  parent_key  TEXT NOT NULL,
  session_key TEXT,
  started_at  REAL NOT NULL,
  ended_at    REAL,
  end_reason  TEXT,
  -- WHERE THIS ATTEMPT IS IN ITS OWN LIFE (#894 review 3). The first two rounds discharged a
  -- reservation from whichever caller happened to notice it, and every caller had to GUESS
  -- whether a process existed: the identity callback runs before the socket does, recovery
  -- cannot tell `spared` from `stopped`, and a cancellation before the key leaves a row nobody
  -- can probe. Three rounds of patching one caller at a time produced a new gap in an adjacent
  -- one each time, because the missing thing was never a branch — it was this column.
  --
  --   reserved  — claimed; no key, no process. NOT probeable: there is nothing to probe, and
  --               absence of a socket is not evidence of a death that never happened.
  --   launching — a key is minted and the spawn is in flight. STILL not probeable, and this is
  --               review 3 finding 1 exactly: the socket does not exist yet, so a concurrent
  --               reaper read `DEAD` and freed a slot whose agent then started successfully.
  --   live      — adopted. The only state in which liveness is a question worth asking.
  --   ended     — discharged, with `ended_at` and a reason.
  state       TEXT NOT NULL DEFAULT 'reserved'
);
CREATE INDEX IF NOT EXISTS idx_mission_spawns_open
  ON mission_spawns(mission_id, ended_at);
"""

# FRESH INSTALLS DO NOT RUN THE MIGRATION LADDER — `_migrate` stamps `user_version` straight to
# `SCHEMA_VERSION` when the store is new, so a table that exists only inside a migration step is
# created on UPGRADED stores and missing on new ones. That asymmetry is silent: every test on a
# fresh temp store passes and the defect only appears on somebody's real install, or the reverse.
# Verified by creating a fresh store and asserting the table is there (`test_a_FRESH_store_has_the
# _spawn_ledger`). One DDL string, appended to the canonical schema AND executed by the upgrade
# step, so the two paths cannot drift.
_SCHEMA += MISSION_SPAWNS_DDL

#: v28 (#983 P4). Two tables, both about the ONE autonomous AI-written direction an objective
#: episode may get.
#:
#: **`mission_ai_directions` IS the bound**, not a record of one. The primary key admits exactly one
#: row per (mission, objective, episode), so the allowance is taken by a database constraint rather
#: than by a check. Counting completed sends cannot enforce it: a claimed action is IN FLIGHT, not
#: spent, so two overlapping passes on different sessions of the same objective both read zero —
#: before the claim and again inside their own write fences — and both type. Reserved before any
#: byte, and **never released**: an in-flight or indeterminate outcome consumes the allowance for
#: good, because losing one possible send is acceptable and a second unreviewed write is not.
#:
#: **`mission_auto_announcements` is the durable RECEIPT** for the bell. The notification ring is
#: bounded (200 rows) and dismissible, so a receipt kept there is destroyed by ordinary operator
#: action — and the next sweep then re-announces a delivery the operator had already cleared. No
#: foreign key: a tombstone has to outlive whatever it is a tombstone for.
AUTO_DIRECTION_DDL = """
CREATE TABLE IF NOT EXISTS mission_ai_directions (
  mission_id    TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
  objective_key TEXT NOT NULL,
  -- KEYED BY INCARNATION TOO. An episode number is not an identity: dropping an objective
  -- resets the episode and clears the supervisor bindings, so the same key added back arrives at
  -- episode 1 again and a reservation keyed only on (mission, key, episode) still matched it —
  -- the new objective inherited a spent allowance and its draft could never send. The incarnation
  -- is the identity P3 already carries, and it makes a stale row self-evident instead of
  -- something a second delete has to chase.
  incarnation   TEXT NOT NULL,
  episode       INTEGER NOT NULL,
  action_id     TEXT NOT NULL,
  at            REAL NOT NULL,
  PRIMARY KEY (mission_id, objective_key, incarnation, episode)
);

CREATE TABLE IF NOT EXISTS mission_auto_announcements (
  action_id  TEXT PRIMARY KEY,
  mission_id TEXT NOT NULL,
  at         REAL NOT NULL
);
"""

_SCHEMA += AUTO_DIRECTION_DDL

# THE EVIDENCE OF THE LAST FAILED PRIMARY LAUNCH (#966). A failed settlement deletes the dispatch
# record, so the seed outcome and teardown proof it carried are copied here in the same commit —
# with the proposal, which is what Start again restores as the plan. One row per mission; any other
# lifecycle move deletes it, and Start again consumes it. The brief lives here only until then, and
# goes with the mission (ON DELETE CASCADE).
MISSION_DISPATCH_EVIDENCE_DDL = """
CREATE TABLE IF NOT EXISTS mission_dispatch_evidence (
  mission_id         TEXT PRIMARY KEY REFERENCES missions(id) ON DELETE CASCADE,
  plan_id            TEXT NOT NULL,
  session_key        TEXT,
  seed_outcome       TEXT NOT NULL,
  teardown_confirmed INTEGER NOT NULL DEFAULT 0,
  failed_at          REAL NOT NULL,
  project_id         TEXT,
  cwd                TEXT,
  engine             TEXT,
  engine_reason      TEXT,
  brief              TEXT
);
"""
_SCHEMA += MISSION_DISPATCH_EVIDENCE_DDL


def _migrate(con) -> int:
    """Bring the file to :data:`SCHEMA_VERSION`. Explicit and tested, never implicit.

    Version 0 is "no file / empty file"; the create is the migration. Every later version adds an
    ``if version < N`` step below, applied **in ascending order**, so an install upgraded in place
    walks the same path a fresh one would arrive at — and each step is a code path with a test
    rather than a hope.
    """
    version = int(con.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        # A newer app wrote this file. Refuse rather than corrupt it — reads fail soft above.
        raise MissionError(
            f"missions store is at schema {version}, newer than this build ({SCHEMA_VERSION})",
            status=500,
        )
    if version == SCHEMA_VERSION:
        return version
    if version < 1:
        con.executescript(_SCHEMA)
    else:
        # Ascending, one step per version, each guarded so a partially-upgraded file converges.
        if version < 2:
            _migrate_1_to_2(con)
        if version < 3:
            _migrate_2_to_3(con)
        if version < 4:
            _migrate_3_to_4(con)
        if version < 5:
            _migrate_4_to_5(con)
        if version < 6:
            _migrate_5_to_6(con)
        if version < 7:
            _migrate_6_to_7(con)
        if version < 8:
            _migrate_7_to_8(con)
        if version < 9:
            _migrate_8_to_9(con)
        if version < 10:
            _migrate_9_to_10(con)
        if version < 11:
            _migrate_10_to_11(con)
        if version < 12:
            _migrate_11_to_12(con)
        if version < 13:
            _migrate_12_to_13(con)
        if version < 14:
            _migrate_13_to_14(con)
        if version < 15:
            _migrate_14_to_15(con)
        if version < 16:
            _migrate_15_to_16(con)
        if version < 17:
            _migrate_16_to_17(con)
        if version < 18:
            _migrate_17_to_18(con)
        if version < 19:
            _migrate_18_to_19(con)
        if version < 20:
            _migrate_19_to_20(con)
        if version < 21:
            _migrate_20_to_21(con)
        if version < 22:
            _migrate_21_to_22(con)
        if version < 23:
            _migrate_22_to_23(con)
        if version < 24:
            _migrate_23_to_24(con)
        if version < 25:
            _migrate_24_to_25(con)
        if version < 26:
            _migrate_25_to_26(con)
        if version < 27:
            _migrate_26_to_27(con)
        if version < 28:
            _migrate_27_to_28(con)
        if version < 29:
            _migrate_28_to_29(con)
    con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return SCHEMA_VERSION


def _migrate_9_to_10(con) -> None:
    """v10 adds `mission_turns` — the durable claim behind `/message` (#852).

    A new table only, so an in-place upgrade is the same statement a fresh install runs. Nothing
    backfills: a turn is claimed at request time, and there is no history to reconstruct.
    """
    con.execute(
        "CREATE TABLE IF NOT EXISTS mission_turns ("
        "  mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,"
        "  turn_id TEXT NOT NULL,"
        "  msg_sha TEXT NOT NULL,"
        "  state TEXT NOT NULL CHECK (state IN ('in_progress','done','indeterminate')),"
        "  owner TEXT, owner_at REAL, fence TEXT NOT NULL, write_reserved_at REAL,"
        "  result TEXT, result_meta TEXT, action_ids TEXT, created_at REAL NOT NULL,"
        "  settled_at REAL,"
        "  PRIMARY KEY (mission_id, turn_id))"
    )


def _migrate_10_to_11(con) -> None:
    """v11 adds `mission_turns.result_meta`.

    It exists as its own version because it was very nearly added WITHOUT one: the column went
    into the v9 `CREATE TABLE` while `SCHEMA_VERSION` stayed at 9, so a database created by the
    previous build was accepted as current, never migrated, and then failed **every** turn
    settlement with `no such column: result_meta`. A fresh install would have looked perfect and
    every existing one would have broken.

    That is the whole reason this machinery is explicit rather than implicit: editing a `CREATE
    TABLE` only ever describes a *new* file.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(mission_turns)")}
    if "result_meta" not in cols:
        con.execute("ALTER TABLE mission_turns ADD COLUMN result_meta TEXT")
    # …and the two columns that make a turn's timeline events exactly-once. They live on the turn
    # rather than beside the events because the turn is the thing with an identity: "has this
    # turn already written its operator message" is a question only the claim can answer.
    if "operator_seq" not in cols:
        con.execute("ALTER TABLE mission_turns ADD COLUMN operator_seq INTEGER")
    if "assistant_seq" not in cols:
        con.execute("ALTER TABLE mission_turns ADD COLUMN assistant_seq INTEGER")


def _migrate_1_to_2(con) -> None:
    """v2 makes UNARCHIVE durable the way archive already was.

    A crash between "the provider sessions were restored" and "the record says un-archived" used
    to leave live sessions under a mission still recorded archived, and boot recovery only looked
    at ``archiving_at`` — so the torn state was invisible. The claim stamp is what makes it
    findable.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(missions)")}
    if "unarchiving_at" not in cols:
        con.execute("ALTER TABLE missions ADD COLUMN unarchiving_at REAL")


def _migrate_2_to_3(con) -> None:  # noqa: D401 — see the body; two changes ship as one step
    """v3 moves the settlement projection off the event row into its own table.

    Storing it on the event made two contracts fight: the timeline is a **bounded feed** and the
    projection is a **durability promise**, so bounding the feed necessarily deleted the promise.
    Normalising it settles that — see the ``mission_settlements`` comment in the schema.
    """
    con.execute(
        "CREATE TABLE IF NOT EXISTS mission_settlements ("
        "  action_id TEXT PRIMARY KEY, verb TEXT, state TEXT,"
        "  rationale TEXT, outcome TEXT, at REAL)"
    )
    mcols = {r[1] for r in con.execute("PRAGMA table_info(missions)")}
    if "unarchive_sessions" not in mcols:
        # The unarchive claim now remembers the mode it was made for.
        con.execute("ALTER TABLE missions ADD COLUMN unarchive_sessions INTEGER")
    cols = {r[1] for r in con.execute("PRAGMA table_info(mission_events)")}
    if "settlement" not in cols:
        return
    for row in con.execute(
        "SELECT action_id, settlement FROM mission_events "
        "WHERE action_id IS NOT NULL AND settlement IS NOT NULL"
    ).fetchall():
        proj = _loads(row["settlement"]) or {}
        con.execute(
            "INSERT OR IGNORE INTO mission_settlements "
            "(action_id, verb, state, rationale, outcome, at) VALUES (?,?,?,?,?,?)",
            (
                row["action_id"],
                proj.get("verb"),
                proj.get("state"),
                proj.get("rationale"),
                proj.get("outcome"),
                proj.get("at"),
            ),
        )
    con.execute("ALTER TABLE mission_events DROP COLUMN settlement")


def _migrate_3_to_4(con) -> None:
    """v4 gives the archive/unarchive OPERATION an owner, and the store a durable obligation slot.

    Per-session leases were not enough: a second caller saw an empty pending list — because the
    first worker had already leased every row — and finalised the operation while that worker was
    still inside ``cleanup_runtime``. And a failed scrub had no way to be retried, because the
    rows it should have scrubbed were already deleted.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(missions)")}
    if "op_token" not in cols:
        con.execute("ALTER TABLE missions ADD COLUMN op_token TEXT")
    con.execute("CREATE TABLE IF NOT EXISTS store_flags (key TEXT PRIMARY KEY, value TEXT)")


def _migrate_5_to_6(con) -> None:
    """v6 records which process holds a session lease.

    Boot recovery reopens leases on the premise that any it finds must belong to a crashed
    worker. That is true at boot and false while the app is serving — and it serves immediately,
    with recovery running behind it. Without an owner, recovery could reset a lease a live request
    was holding and then claim the same session, so two workers ran the same destructive effect.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(mission_sessions)")}
    if "lease_owner" not in cols:
        con.execute("ALTER TABLE mission_sessions ADD COLUMN lease_owner TEXT")


def _migrate_6_to_7(con) -> None:
    """v7 stamps WHEN a lease was taken, so one can expire.

    v6 made recovery refuse a lease owned by the current process, which is right — a live worker's
    lease must not be reset. But the write that RELEASES a lease can fail too, and then nothing in
    that process's lifetime could reclaim it. Age is the property that does not depend on another
    write succeeding.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(mission_sessions)")}
    if "lease_at" not in cols:
        con.execute("ALTER TABLE mission_sessions ADD COLUMN lease_at REAL")


def _ddl_from_schema(table: str) -> str:
    """The base schema's OWN `CREATE TABLE` text for one table.

    A migration that rebuilds a table has to produce the same shape a fresh install gets, and
    re-typing the DDL is how the two drift — a constraint fixed in one copy and not the other is
    then present for fresh operators and absent for upgraded ones, or the reverse, and only one
    kind of install ever shows it. `test_a_FRESH_install_and_an_UPGRADED_one_get_the_SAME_
    supervisor_schema` exists to catch that, and it caught it here on a stray space.

    So the migration reads the declaration instead of restating it: there is one copy of the truth,
    and agreement is structural rather than a thing to remember.
    """
    marker = f"CREATE TABLE IF NOT EXISTS {table} ("
    start = _SCHEMA.index(marker)
    end = _SCHEMA.index(");", start) + 2
    return _SCHEMA[start:end]


def _has_table(con, name: str) -> bool:
    """Does this database have that table? Asked before a step ALTERs one.

    A migration step runs against whatever the previous version actually left behind, which is
    not always the version's full schema — the ladder's own v9 regression builds a store with two
    tables in it. A step that assumes more raises `no such table` and strands the upgrade, which
    is precisely the failure the ladder exists to prevent, one level up.
    """
    row = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (name,)
    ).fetchone()
    return row is not None


def _migrate_17_to_18(con) -> None:
    """v18 gives a turn its own durable state: `acked_at` and `message` (#902).

    `acked_at` is the operator's dismissal of an ambiguous turn — terminal, unresolvable by the
    server, and therefore something only they can end. `message` is their own words, which used
    to be reached by joining to the timeline; the timeline is a capped FEED, so on a busy mission
    an open turn came back with no text at all.

    **AND IT BACKFILLS** (#902 review 2, the rollout finding). An existing store already has
    `in_progress` / `indeterminate` rows whose message lives in the linked `operator_msg` event —
    adding the column and leaving it NULL means the upgrade itself drops a question that was
    recoverable the moment before it ran, and CHECK AGAIN then replays an empty message. Only the
    UNSETTLED rows are copied: a `done` turn's answer is on the timeline, which is where finished
    turns live, so nothing needs its prompt back.

    A legacy row whose event has already been trimmed away stays NULL, which reads as "" — the
    same state it was in before this column existed, and the honest one: the words are gone.

    Idempotent by inspection, and skipped where the table is not there to alter.
    """
    if not _has_table(con, "mission_turns"):
        return
    have = {r["name"] for r in con.execute("PRAGMA table_info(mission_turns)").fetchall()}
    if "acked_at" not in have:
        con.execute("ALTER TABLE mission_turns ADD COLUMN acked_at REAL")
    if "message" not in have:
        con.execute("ALTER TABLE mission_turns ADD COLUMN message TEXT")
    if _has_table(con, "mission_events"):
        con.execute(
            "UPDATE mission_turns SET message = ("
            "  SELECT e.text FROM mission_events e"
            "  WHERE e.mission_id = mission_turns.mission_id"
            "    AND e.seq = mission_turns.operator_seq) "
            "WHERE message IS NULL AND operator_seq IS NOT NULL "
            "  AND state IN ('in_progress','indeterminate')"
        )


def _migrate_21_to_22(con) -> None:
    """v22 adds `mission_dispatches.spawn_parent` — whose sub-agent a launch is (#894).

    NULL for the mission's own dispatch, which is every row that already exists, so the upgrade
    needs no backfill: an absent parent is exactly the truth about a launch that was not a spawn.

    Skipped where the table is not there to alter, for the reason `_has_table` gives everywhere
    else in this ladder: a step runs against what the PREVIOUS version left behind, not against
    that version's full schema, and one that raises strands the upgrade.
    """
    if not _has_table(con, "mission_dispatches"):
        return
    have = {r["name"] for r in con.execute("PRAGMA table_info(mission_dispatches)").fetchall()}
    if "spawn_parent" not in have:
        con.execute("ALTER TABLE mission_dispatches ADD COLUMN spawn_parent TEXT")


def _migrate_23_to_24(con) -> None:
    """v24 gives a spawn reservation its own lifecycle state (#894 review 3).

    v23 recorded WHAT was reserved and left WHERE IT HAD GOT TO implicit, inferred by each caller
    from whatever it could see. That inference was wrong in three different places and each fix
    exposed the next: the identity callback runs before the socket exists, so a concurrent reaper
    read a not-yet-created socket as `DEAD` and freed a slot whose agent then started; startup
    recovery could not tell a `spared` child (still running, another mission owns it) from a
    stopped one; and a cancellation before the key was minted left a row with nothing to probe.

    A state column ends the guessing: only `live` rows are probeable, and every other transition
    is an explicit statement by the code that knows what happened rather than a deduction from an
    absent socket.

    Existing rows are back-filled by what they already carry — a key means the launch got at least
    as far as minting one, so `live`; no key means it never did, so `reserved`. Neither is a claim
    about a process; both are the conservative reading, and `reserved`/`launching` are the states
    the reaper refuses to touch.
    """
    if not _has_table(con, "mission_spawns"):
        return
    have = {r["name"] for r in con.execute("PRAGMA table_info(mission_spawns)").fetchall()}
    if "state" not in have:
        con.execute("ALTER TABLE mission_spawns ADD COLUMN state TEXT NOT NULL DEFAULT 'reserved'")
    con.execute("UPDATE mission_spawns SET state='ended' WHERE ended_at IS NOT NULL")
    con.execute(
        "UPDATE mission_spawns SET state='live' "
        "WHERE ended_at IS NULL AND session_key IS NOT NULL"
    )


def _migrate_24_to_25(con) -> None:
    """v25 makes planning a durable, generation-fenced intent (#967).

    Added the way v9 added `objectives_state`, with one deliberate difference: this one BACKFILLS.
    A NULL `objectives_state` means "never retried"; here every existing mission gets an honest
    terminal answer instead — `ready` if it has a plan row, `skipped` if it does not — so the plan
    card has a state to render and no mission that predates the planner starts planning by itself
    on the first boot after the upgrade.

    Skipped where a table is not there to alter, for the reason `_has_table` gives everywhere else
    in this ladder.
    """
    if _has_table(con, "missions"):
        have = {r["name"] for r in con.execute("PRAGMA table_info(missions)").fetchall()}
        if "plan_state" not in have:
            con.execute("ALTER TABLE missions ADD COLUMN plan_state TEXT")
        if "plan_generation" not in have:
            con.execute(
                "ALTER TABLE missions ADD COLUMN plan_generation INTEGER NOT NULL DEFAULT 0"
            )
        if "plan_at" not in have:
            con.execute("ALTER TABLE missions ADD COLUMN plan_at REAL")
        if "plan_detail" not in have:
            con.execute("ALTER TABLE missions ADD COLUMN plan_detail TEXT")
    has_plans = _has_table(con, "mission_plans")
    if has_plans:
        have = {r["name"] for r in con.execute("PRAGMA table_info(mission_plans)").fetchall()}
        if "generation" not in have:
            con.execute(
                "ALTER TABLE mission_plans ADD COLUMN generation INTEGER NOT NULL DEFAULT 0"
            )
    if _has_table(con, "missions"):
        if has_plans:
            con.execute(
                "UPDATE missions SET plan_state = CASE WHEN EXISTS ("
                "  SELECT 1 FROM mission_plans p WHERE p.mission_id = missions.id"
                ") THEN 'ready' ELSE 'skipped' END "
                "WHERE plan_state IS NULL"
            )
        else:
            con.execute("UPDATE missions SET plan_state='skipped' WHERE plan_state IS NULL")


def _migrate_25_to_26(con) -> None:
    """v26 records what a failed launch typed, so Start again can be decided on evidence (#966).

    Existing dispatch rows get NULL `seed_outcome` — `unknown` — and an unconfirmed teardown, so no
    failure that predates the evidence can be started again. The evidence table is created from the
    same DDL string the base schema carries, one statement, inside the ladder's transaction.
    """
    if _has_table(con, "mission_dispatches"):
        have = {r["name"] for r in con.execute("PRAGMA table_info(mission_dispatches)").fetchall()}
        if "seed_outcome" not in have:
            con.execute("ALTER TABLE mission_dispatches ADD COLUMN seed_outcome TEXT")
        if "teardown_confirmed" not in have:
            con.execute(
                "ALTER TABLE mission_dispatches "
                "ADD COLUMN teardown_confirmed INTEGER NOT NULL DEFAULT 0"
            )
    if _has_table(con, "missions"):
        con.execute(MISSION_DISPATCH_EVIDENCE_DDL)


def _migrate_26_to_27(con) -> None:
    """v27 gives an objective the operator's DIRECTION and where it came from (#983).

    Both columns are appended, so an upgraded store gets the column order a fresh one does. Every
    existing row keeps NULL — no direction — which renders the global nudge exactly as before; no
    mission gains a direction it was not given. Idempotent by inspection, and skipped where the
    table is not there to alter.
    """
    if not _has_table(con, "mission_objectives"):
        return
    have = {r["name"] for r in con.execute("PRAGMA table_info(mission_objectives)").fetchall()}
    if "direction" not in have:
        con.execute("ALTER TABLE mission_objectives ADD COLUMN direction TEXT")
    if "direction_source" not in have:
        con.execute("ALTER TABLE mission_objectives ADD COLUMN direction_source TEXT")


#: The same DDL the base schema carries, so an upgraded store gets exactly what a fresh one has.
SESSION_RUNTIME_BINDINGS_DDL = """CREATE TABLE IF NOT EXISTS session_runtime_bindings (
  logical_key  TEXT PRIMARY KEY,
  physical_key TEXT NOT NULL,
  bound_at     REAL NOT NULL
)"""


def _migrate_27_to_28(con) -> None:
    """v28 lets a mission own a session whose id was bound after its launch (#989).

    `session_runtime_bindings` records where a late-bound session's runtime lives, and
    `mission_dispatches.attempt_nonce` records the discriminator its launch delivered. An existing
    store gets an empty table and NULL nonces, which is exactly right: every session adopted before
    this version was adopted under the key its runtime lives under, and no earlier dispatch carried
    a nonce.
    """
    con.execute(SESSION_RUNTIME_BINDINGS_DDL)
    if _has_table(con, "mission_dispatches"):
        have = {r["name"] for r in con.execute("PRAGMA table_info(mission_dispatches)").fetchall()}
        if "attempt_nonce" not in have:
            con.execute("ALTER TABLE mission_dispatches ADD COLUMN attempt_nonce TEXT")


def _migrate_28_to_29(con) -> None:
    """v29 adds the autonomous AI direction's reservation and its announcement receipt (#983 P4).

    This was written as v28 and renumbered when #989's late-bound-session work took that number
    first on main — so it follows that migration rather than competing with it, and a store already
    at v28 runs only this one.

    New tables only, and applied from the SAME DDL string a fresh install runs, so an upgraded
    store and a new one cannot end up with different definitions of the constraint the bound rests
    on. Existing stores gain no rows, so every objective episode starts with its one allowance
    unspent and nothing already delivered is treated as owing an announcement.
    """
    con.executescript(AUTO_DIRECTION_DDL)


def _migrate_22_to_23(con) -> None:
    """v23 adds `mission_spawns` — the durable resource ledger behind the sub-agent cap (#894).

    **The cap counted the roster, and the roster is not a resource fact.** `mission_sessions` is
    membership: `removed_at` says this mission no longer claims a session, and `spawned_by` says
    how it arrived. Both are mutable by design and neither is about the process. Counting them
    made the bound evadable two different ways, both found in review (review 1, finding 4):

    * **RELEASE frees the slot while the agent runs.** `detach` stamps `removed_at` and stops
      nothing — deliberately, because release is an ownership operation and turning it into a kill
      would be a worse bug. But the count filtered on `removed_at IS NULL`, so spawn -> release ->
      spawn repeated without bound while every one of those agents was still on the host.
    * **RE-ADOPT erases parentage.** `_adopt_tx` refreshes `role` and `spawned_by` on a session
      the mission already holds, so adopting a child through the ordinary adopt route overwrote
      `spawned_by` with NULL and dropped it out of the count without releasing anything.

    So the ledger is separate from the roster and answers a different question: **what did this
    mission start that has not been proven to have stopped.** A row is written when the slot is
    reserved and closed only by evidence — a launch that provably started nothing, or a process
    observed dead. Membership changes do not touch it, which is the whole point.

    Keyed on `plan_id` rather than `session_key`: the slot is reserved inside `claim_spawn`,
    before any key has been minted, and a reservation that cannot be recorded until the launch
    succeeds is not a reservation at all.

    A new table only, so the in-place upgrade is the same statement a fresh install runs. Existing
    sub-agents are NOT back-filled: their processes cannot be re-observed retroactively, and
    inventing ledger rows for them would assert a resource obligation nobody measured. They age
    out of the roster normally and the ledger starts from the first spawn after the upgrade.
    """
    # One statement per `execute` — `executescript` would COMMIT the migration transaction out
    # from under the ladder, and the DDL constant carries both a table and its index.
    con.execute(
        "CREATE TABLE IF NOT EXISTS mission_spawns ("
        "  plan_id TEXT PRIMARY KEY,"
        "  mission_id TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,"
        "  parent_key TEXT NOT NULL, session_key TEXT, started_at REAL NOT NULL,"
        "  ended_at REAL, end_reason TEXT)"
    )
    con.execute(
        "CREATE INDEX IF NOT EXISTS idx_mission_spawns_open "
        "ON mission_spawns(mission_id, ended_at)"
    )


def _migrate_20_to_21(con) -> None:
    """v21 adds `mission_plans` and `mission_dispatches` — the proposal and the launch (#893).

    Renumbered from v19 while this branch was in review: `main` took 19 for the objective
    incarnation and 20 for the question hold. The ladder has to stay a ladder, so a migration
    that arrives late goes on the END rather than into the middle of somebody else's.

    New tables only, so the in-place upgrade is the same statement a fresh install runs. Nothing
    backfills: a plan is produced on request and there is no history to reconstruct, and a
    dispatch that predates this table was never durable to begin with.
    """
    con.execute(
        "CREATE TABLE IF NOT EXISTS mission_plans ("
        "  mission_id TEXT PRIMARY KEY REFERENCES missions(id) ON DELETE CASCADE,"
        "  plan_id TEXT NOT NULL, project_id TEXT, cwd TEXT,"
        "  engine TEXT, engine_reason TEXT, brief TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS mission_dispatches ("
        "  mission_id TEXT PRIMARY KEY REFERENCES missions(id) ON DELETE CASCADE,"
        "  plan_id TEXT NOT NULL, engine TEXT NOT NULL, cwd TEXT NOT NULL,"
        "  session_key TEXT, started_at REAL NOT NULL,"
        "  project_id TEXT, engine_reason TEXT, brief TEXT, owner TEXT,"
        "  spawn_parent TEXT)"
    )


def _migrate_16_to_17(con) -> None:
    """v17 records the merge SHA, so `http_revision` has the revision it is supposed to look for.

    #891's `change_live` contract binds the probe to the merge SHA; without a producer that
    fallback pointed at a field nothing wrote, so every objective relying on it answered `unknown`
    for ever. Idempotent by inspection; SQLite's `ADD COLUMN` has no `IF NOT EXISTS`.

    Skipped where `mission_objectives` is not there to alter, for the reason `_has_table` gives:
    a step runs against what the PREVIOUS version left behind, not against that version's
    full schema, and one that raises strands the upgrade.
    """
    if not _has_table(con, "mission_objectives"):
        return
    have = {r["name"] for r in con.execute("PRAGMA table_info(missions)").fetchall()}
    if "merge_sha" not in have:
        con.execute("ALTER TABLE missions ADD COLUMN merge_sha TEXT")
    # …AND `probe_rev`, WHICH A v16 STORE NEVER GOT (#897 re-review 6, finding 1).
    #
    # It was appended to the v14→v15 step after v16 already existed, so a database that had
    # already reached 16 walks straight past it and lands on 17 without the column — and the
    # first probe then fails with `no such column: probe_rev`. A fresh install is fine, because
    # the base `CREATE TABLE` carries it, which is exactly the asymmetry that makes this kind of
    # edit dangerous: it is invisible to everyone except the operators who already had the app.
    #
    # Adding it here rather than editing the v15 step, because a v15 file that already ran that
    # step would not re-run it either. Every migration is guarded by inspection, so a store that
    # HAS the column is untouched whichever path it took.
    ocols = {r["name"] for r in con.execute("PRAGMA table_info(mission_objectives)").fetchall()}
    if "probe_rev" not in ocols:
        con.execute(
            "ALTER TABLE mission_objectives ADD COLUMN probe_rev INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_15_to_16(con) -> None:
    """v16 moves the probe generation off the objective row and onto the mission (#897 re-review).

    A generation derived from a per-row counter restarts when the row is dropped and re-added, so
    an answer issued for the previous incarnation of an objective key matched the new one — the
    exact failure the generation exists to prevent. The source is now mission-scoped and
    monotonic, so a value is never handed out twice within a mission.

    Idempotent by inspection; SQLite's `ADD COLUMN` has no `IF NOT EXISTS`.
    """
    have = {r["name"] for r in con.execute("PRAGMA table_info(missions)").fetchall()}
    if "probe_gen_seq" not in have:
        con.execute("ALTER TABLE missions ADD COLUMN probe_gen_seq INTEGER NOT NULL DEFAULT 0")


def _migrate_19_to_20(con) -> None:
    """v20 gives every objective a stable incarnation (#900 review, finding 4).

    A key is a slot. Without something that survives a drop-and-re-add, a decision made ABOUT an
    objective during a model call could land on the row that replaced it.

    Existing rows are backfilled with one each, so the column is meaningful immediately rather
    than only for objectives created after the upgrade. Idempotent by inspection, and skipped
    entirely where the table is not there to alter — see `_has_table`.
    """
    if not _has_table(con, "mission_objectives"):
        return
    have = {r["name"] for r in con.execute("PRAGMA table_info(mission_objectives)").fetchall()}
    if "incarnation" not in have:
        con.execute("ALTER TABLE mission_objectives ADD COLUMN incarnation TEXT")
    rows = con.execute(
        "SELECT mission_id, key FROM mission_objectives "
        "WHERE incarnation IS NULL OR incarnation=''"
    ).fetchall()
    for r in rows:
        con.execute(
            "UPDATE mission_objectives SET incarnation=? WHERE mission_id=? AND key=?",
            (uuid.uuid4().hex, r["mission_id"], r["key"]),
        )


def _migrate_14_to_15(con) -> None:
    """v15 binds an in-flight probe to its resolved target durably (#897 re-review, finding 2).

    The fence was a pre/post digest comparison held in the runner's own locals. That does not
    survive a restart, does not see a second runner probing the same row, and leaves a window
    between the comparison and the write in which the answer settles against a target nobody
    checked. Two columns move it into the row, where the settling transaction can read it.

    Idempotent by inspection — SQLite's `ADD COLUMN` has no `IF NOT EXISTS`.

    Skipped where `mission_objectives` is not there to alter, for the reason `_has_table` gives:
    a step runs against what the PREVIOUS version left behind, not against that version's
    full schema, and one that raises strands the upgrade.
    """
    if not _has_table(con, "mission_objectives"):
        return
    have = {r["name"] for r in con.execute("PRAGMA table_info(mission_objectives)").fetchall()}
    if "probe_target" not in have:
        con.execute("ALTER TABLE mission_objectives ADD COLUMN probe_target TEXT")
    if "probe_gen" not in have:
        con.execute(
            "ALTER TABLE mission_objectives ADD COLUMN probe_gen INTEGER NOT NULL DEFAULT 0"
        )
    if "probe_rev" not in have:
        con.execute(
            "ALTER TABLE mission_objectives ADD COLUMN probe_rev INTEGER NOT NULL DEFAULT 0"
        )


def _migrate_18_to_19(con) -> None:
    """v19 gives the question hold a column of its own (#892).

    Reusing `stood_down` would have made two different facts share one boolean — the operator's
    "stop telling me" and the supervisor's "waiting on your answer" — so answering a question
    would clear a silence the operator set, and the board could not name which applied.

    Idempotent by inspection: SQLite's `ADD COLUMN` has no `IF NOT EXISTS`. And skipped where the
    table is not there to alter, for the reason `_has_table` gives.
    """
    if not _has_table(con, "mission_objective_episode"):
        return
    have = {r["name"] for r in con.execute("PRAGMA table_info(mission_objective_episode)")}
    if "question_seq" not in have:
        con.execute("ALTER TABLE mission_objective_episode ADD COLUMN question_seq INTEGER")


def _migrate_13_to_14(con) -> None:
    """v14 adds the stall baseline to `mission_supervisor` (#888 review, finding 2).

    The columns were added to the v13 *table definition* after the v13 migration had already been
    written, which is a shape change without a version change: a database created by the earlier
    v13 returns immediately from `_migrate` and never gets them, and the first stall check then
    raises `no such column: growth_mark`. A fresh install was fine, which is exactly why it needed
    a version of its own — that asymmetry is the whole failure mode of an unversioned edit.

    Idempotent by inspection rather than by `IF NOT EXISTS`, which SQLite's `ADD COLUMN` does not
    support: a v13 file that never had the columns gets them, and one that did (created after the
    definition changed) is left alone.

    Skipped where `mission_supervisor` is not there to alter, for the reason `_has_table` gives:
    a step runs against what the PREVIOUS version left behind, not against that version's
    full schema, and one that raises strands the upgrade.
    """
    if not _has_table(con, "mission_supervisor"):
        return
    have = {r["name"] for r in con.execute("PRAGMA table_info(mission_supervisor)").fetchall()}
    if "growth_mark" not in have:
        con.execute("ALTER TABLE mission_supervisor ADD COLUMN growth_mark INTEGER")
    if "growth_at" not in have:
        con.execute("ALTER TABLE mission_supervisor ADD COLUMN growth_at REAL")


def _migrate_12_to_13(con) -> None:
    """v13 re-keys two supervisor tables (#888 review, findings 4 and 9).

    Both are constraint changes, so both are table rebuilds — SQLite cannot alter a PRIMARY KEY or
    a UNIQUE in place.

    * `mission_supervisor` gains `session_key` in its key. Existing rows are mission-wide and there
      is no way to know which session they described, so they are carried onto a sentinel key
      rather than guessed at. A sentinel row matches no real session, so the first pass after the
      upgrade simply takes a fresh checkpoint per session — one extra model call per session, once,
      which is the honest price of not inventing an attribution.

    It also adds `supervisor_state`, the sweep's durable cursor.

    * `mission_escalations` drops `session_key` from its uniqueness key. Any duplicate rows that
      the old per-session key allowed are collapsed to the earliest, because the earliest is the
      one whose notification the operator actually saw.
    """
    con.execute(_ddl_from_schema("supervisor_state"))
    con.execute("ALTER TABLE mission_supervisor RENAME TO mission_supervisor_old")
    con.execute(_ddl_from_schema("mission_supervisor"))
    con.execute(
        "INSERT INTO mission_supervisor "
        "(mission_id, session_key, input_fp, recap_seq, updated_at) "
        "SELECT mission_id, ?, input_fp, recap_seq, updated_at FROM mission_supervisor_old",
        (_PRE_V13_CHECKPOINT,),
    )
    con.execute("DROP TABLE mission_supervisor_old")

    con.execute("ALTER TABLE mission_escalations RENAME TO mission_escalations_old")
    con.execute(_ddl_from_schema("mission_escalations"))
    con.execute(
        "INSERT INTO mission_escalations "
        "(mission_id, session_key, objective_key, episode, reason, at) "
        "SELECT mission_id, session_key, objective_key, episode, reason, at "
        # ONE row per group, chosen by `rowid`, not by `at`. Two legacy per-session rows for the
        # same objective episode can legally share a timestamp — they were written by passes that
        # raced — and `at = MIN(at)` then selects BOTH, which the new objective-level UNIQUE
        # rejects, failing the whole upgrade. Ordering by `(at, rowid)` keeps "earliest wins" as
        # the intent and makes the tie-break total.
        "FROM mission_escalations_old o WHERE o.rowid = ("
        "  SELECT i.rowid FROM mission_escalations_old i "
        "  WHERE i.mission_id=o.mission_id AND i.objective_key=o.objective_key "
        "    AND i.episode=o.episode ORDER BY i.at ASC, i.rowid ASC LIMIT 1)"
    )
    con.execute("DROP TABLE mission_escalations_old")


def _migrate_11_to_12(con) -> None:
    """v12 adds the supervisor's durable state (#885, Phase 5a of #840).

    Four tables, and each one exists because the thing it records cannot be derived:

    * `mission_supervisor` — the recap checkpoint. `input_fp` and `recap_seq` advance together.
    * `mission_objective_episode` — the budget's reset boundary. "Resets when the objective moved"
      is not expressible by derivation alone: re-deriving after progress still counts the earlier
      nudges, so the boundary has to be written down.
    * `mission_supervisor_actions` — WHICH action each nudge was, so the budget is read back from
      the ledger's terminal states instead of incremented at send time. A counter is wrong on both
      sides of a crash; a binding is wrong on neither.
    * `mission_escalations` — one row per objective episode, `UNIQUE` so overlapping passes
      collide at the database rather than at a check.
    """
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS mission_supervisor (
          mission_id TEXT PRIMARY KEY REFERENCES missions(id) ON DELETE CASCADE,
          input_fp   TEXT,
          recap_seq  INTEGER,
          updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS mission_objective_episode (
          mission_id    TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
          objective_key TEXT NOT NULL,
          episode       INTEGER NOT NULL,
          stood_down    INTEGER NOT NULL DEFAULT 0,
          at            REAL NOT NULL,
          PRIMARY KEY (mission_id, objective_key)
        );
        CREATE TABLE IF NOT EXISTS mission_supervisor_actions (
          mission_id    TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
          session_key   TEXT NOT NULL,
          objective_key TEXT NOT NULL,
          episode       INTEGER NOT NULL,
          action_id     TEXT NOT NULL,
          at            REAL NOT NULL,
          PRIMARY KEY (mission_id, action_id)
        );
        CREATE TABLE IF NOT EXISTS mission_escalations (
          mission_id    TEXT NOT NULL REFERENCES missions(id) ON DELETE CASCADE,
          session_key   TEXT NOT NULL,
          objective_key TEXT NOT NULL,
          episode       INTEGER NOT NULL,
          reason        TEXT NOT NULL,
          at            REAL NOT NULL,
          UNIQUE (mission_id, session_key, objective_key, episode)
        );
        """
    )


def _migrate_8_to_9(con) -> None:
    """v9 makes objective production a DURABLE intent rather than an in-process hope (#883).

    The producer ran as a `BackgroundTask` on the create response, which lives only in this
    server process: a crash or restart between the mission's commit and the model call left a
    permanent empty checklist, with no timeline event saying why and nobody to retry it. A
    background task is not a promise.

    `objectives_state` records the intent alongside the mission itself, in the same transaction,
    so recovery can find the work the same way `resume_pending_operations` finds a half-finished
    archive. NULL means "this mission predates the producer" and is never retried — a backfill
    would propose objectives for every historical mission at once (review on #884).
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(missions)")}
    if "objectives_state" not in cols:
        con.execute("ALTER TABLE missions ADD COLUMN objectives_state TEXT")
    if "objectives_at" not in cols:
        con.execute("ALTER TABLE missions ADD COLUMN objectives_at REAL")


def _migrate_7_to_8(con) -> None:
    """v8 turns the read-only archive guard into a real reservation with a fencing token.

    A guard answers about the past and the caller then acts, so a mission could adopt a session
    between the check and the provider call. And an expired lease reclaimed by a new worker left
    the old worker still able to settle it, so the stale result could win and the external effect
    could run twice.
    """
    con.execute(
        "CREATE TABLE IF NOT EXISTS session_reservations ("
        "  session_key TEXT PRIMARY KEY, token TEXT NOT NULL,"
        "  holder TEXT NOT NULL, at REAL NOT NULL)"
    )
    cols = {r[1] for r in con.execute("PRAGMA table_info(mission_sessions)")}
    if "lease_token" not in cols:
        con.execute("ALTER TABLE mission_sessions ADD COLUMN lease_token TEXT")


def _flag_get(con, key: str) -> str | None:
    row = con.execute("SELECT value FROM store_flags WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def _flag_set(con, key: str, value: str | None) -> None:
    if value is None:
        con.execute("DELETE FROM store_flags WHERE key=?", (key,))
    else:
        con.execute(
            "INSERT INTO store_flags (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )


def _migrate_4_to_5(con) -> None:
    """v5 records WHY a session left the roster, so archive can respect an explicit detach.

    Terminal-state release and operator detach both set ``removed_at``, so archiving "the whole
    roster" reaped sessions the operator had deliberately removed from the mission. Existing rows
    default to ``closed``: before this column there was no detach distinction to preserve, and
    ``closed`` is the behaviour they already had.
    """
    cols = {r[1] for r in con.execute("PRAGMA table_info(mission_sessions)")}
    if "release_reason" not in cols:
        con.execute("ALTER TABLE mission_sessions ADD COLUMN release_reason TEXT")
        con.execute(
            "UPDATE mission_sessions SET release_reason='closed' WHERE removed_at IS NOT NULL"
        )


_schema_lock = threading.Lock()
_schema_done: set[str] = set()


def _ready(path: Path | None = None, *, busy_timeout_ms: int = _BUSY_TIMEOUT_MS):
    """A connection with the schema present. Cheap after the first call per path."""
    p = path or _db_path()
    con = _connect(p, busy_timeout_ms=busy_timeout_ms)
    key = str(p)
    if key in _schema_done:
        return con
    with _schema_lock:
        _migrate(con)
        _schema_done.add(key)
    return con


def reset_schema_cache_for_test() -> None:
    """Forget which paths have been migrated (tests point the env at a fresh tmp file)."""
    with _schema_lock:
        _schema_done.clear()


# ---------------------------------------------------------------- pool + admission

_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
#: Every write core runs under this, so the app never contends with itself on the DB lock.
_write_lock = threading.RLock()

_budget_lock = threading.Lock()
_inflight = 0


def executor() -> ThreadPoolExecutor:
    """The module's own pool, sized to exactly :data:`MISSIONS_DB_WORKERS`.

    Deliberately not ``asyncio.to_thread``'s default pool — see the module docstring. Sized to
    equal the admission bound so admission and execution coincide and nothing ever queues.
    """
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=MISSIONS_DB_WORKERS, thread_name_prefix="missions-db"
            )
        return _EXECUTOR


def shutdown_executor_for_test() -> None:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is not None:
            _EXECUTOR.shutdown(wait=False)
            _EXECUTOR = None


def inflight_for_test() -> int:
    with _budget_lock:
        return _inflight


def acquire() -> None:
    """Take a slot or raise :class:`MissionsBusy`. Called by the ROUTE, **before** submission.

    Admission has to happen above the executor: submitting first bounds what *runs* while a flood
    piles up in the queue behind it, and with a five-second ``busy_timeout`` per contended write
    that queue is experienced as a hang rather than as an answer.
    """
    global _inflight
    with _budget_lock:
        if _inflight >= MISSIONS_DB_MAX_INFLIGHT:
            raise MissionsBusy()
        _inflight += 1


def _release() -> None:
    global _inflight
    with _budget_lock:
        if _inflight > 0:
            _inflight -= 1


class Slot:
    """One admitted request's slot, releasable **exactly once** by whoever ends up owning it.

    Ownership is genuinely ambiguous, and both halves have been measured wrong before (see
    :class:`agent_sessions.files.Slot`, where this shape comes from):

    * If the worker starts, the worker owns it — releasing from the request task hands the slot
      back while the thread is still running.
    * If the request is cancelled *before* the worker starts, the queued callable is dropped and
      the worker never runs, so nothing in the worker can release and the slot leaks for the life
      of the process.

    Neither owner can be chosen up front, so both try and the lock makes the second a no-op.
    """

    __slots__ = ("_lock", "_done")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._done = False

    def release(self) -> bool:
        """Release if nobody has yet. Returns True iff THIS call did it."""
        with self._lock:
            if self._done:
                return False
            self._done = True
        _release()
        return True


def _run_slot(slot: Slot, fn: Callable[[], Any]) -> Any:
    """Worker entry point: run ``fn`` and release the slot when the callable actually exits."""
    try:
        return fn()
    finally:
        slot.release()


async def run_admitted(fn: Callable[[], Any]) -> Any:
    """Admit, run ``fn`` off the loop on the module pool, release **exactly once**.

    The single entry point every async caller uses, and the release logic is
    :func:`agent_sessions.routes.files._run`'s, verbatim in shape, because the obvious version is
    wrong in both directions:

    * a plain ``finally: slot.release()`` around the await hands the slot back **while the worker
      is still running** — occupancy drops to zero with the thread blocked, and the bound then
      admits more work than it names;
    * releasing only in the worker leaks the slot forever when the request is cancelled while the
      callable is still queued, because that callable is dropped and no worker ever runs it.

    ``concurrent.futures.Future.cancelled()`` is the discriminator: it is True **only** when the
    callable never began. So the worker owns the slot whenever it started, and this frame owns it
    only when it did not — and ``Slot.release`` is exactly-once, so the two can never both fire.

    ``acquire()`` raises :class:`MissionsBusy` *before* anything is submitted, which is what makes
    the bound a refusal rather than a queue.
    """
    acquire()
    slot = Slot()
    try:
        cf = executor().submit(_run_slot, slot, fn)
    except BaseException:
        slot.release()  # never submitted: nobody in a worker can ever release it
        raise
    try:
        return await asyncio.wrap_future(cf)
    except BaseException:
        if cf.cancelled():
            slot.release()
        raise


# ---------------------------------------------------------------- helpers


#: Archive states in which a session key is RESERVED against adoption.
#:
#: `in_progress` alone was not enough. `settle_session_archive` moves the row to `done` and
#: releases it *before* the mission's archive finishes — and at that point the provider session
#: really is archived, so adopting it hands the next mission a session whose transcript has been
#: moved out from under it. The reservation therefore lasts until an explicit restore puts the
#: session back, not until the teardown step happens to settle.
RESERVED_ARCHIVE_STATES: frozenset[str] = frozenset(
    {"pending", "in_progress", "done", "already_archived", "restoring", "restore_failed"}
)
#: Built once at import — the only interpolation is a `?` run sized by a module constant.
_RESERVED_ORDER: tuple[str, ...] = tuple(sorted(RESERVED_ARCHIVE_STATES))
_RESERVED_SQL = (  # noqa: S608
    "SELECT mission_id, archive_state FROM mission_sessions "
    "WHERE session_key=? AND archive_state IN ({m}) LIMIT 1"
).format(m=",".join("?" * len(_RESERVED_ORDER)))
_RESERVED_WHY = {
    "pending": "queued for teardown",
    "in_progress": "being torn down",
    "done": "archived",
    "already_archived": "archived",
    "restoring": "being restored",
    # Still archived — the restore did not land. Reserved for the same reason `done` is: adopting
    # it would hand the next mission a session whose transcript is not there.
    "restore_failed": "archived (its restore failed)",
}


#: Identity of THIS process, minted once at import. A lease stamped with it belongs to a worker
#: that is, by definition, still running — so recovery must not reopen it. A lease stamped with
#: anything else (or nothing) belonged to a process that is gone.
PROCESS_EPOCH: str = uuid.uuid4().hex

#: How long a session lease may be held before it is treated as abandoned, whoever owns it.
#: A teardown is `cleanup_runtime` plus a file move — seconds. Five minutes is unambiguous, and
#: it is the only reclamation path that does not depend on some *other* write succeeding at the
#: moment things are already going wrong.
LEASE_MAX_AGE_S = 300


def _new_op_token() -> str:
    """Identity of one archive/unarchive attempt, so `finish` can prove it owns what it closes."""
    return uuid.uuid4().hex


def new_id() -> str:
    return f"msn_{uuid.uuid4().hex}"


def validate_id(mission_id: object) -> str:
    if not isinstance(mission_id, str) or not MISSION_ID_RE.match(mission_id):
        raise MissionError("bad mission id", status=404)
    return mission_id


def _require_str(value: object, field: str) -> str:
    """A string, or a 422 — checked BEFORE any set-membership test.

    ``value in frozenset`` raises ``TypeError: unhashable type`` for a dict or a list, which
    escapes the route as a 500. A malformed request body is a client error, and it says so.
    """
    if not isinstance(value, str):
        raise MissionError(f"{field} must be a string", status=422)
    return value


def strict_bool(value: object, field: str, *, default: bool | None = None) -> bool:
    """A real JSON boolean, or a 422 — never Python truthiness.

    ``bool(value)`` on request JSON makes the string ``"false"`` mean **true**, which on a
    destructive flag is not a type nit: it is an authorisation bug. Every boolean that crosses
    the route boundary comes through here.
    """
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise MissionError(f"{field} must be true or false", status=422)
    return value


def _cap(value: object, limit: int) -> str:
    return (value if isinstance(value, str) else "").strip()[:limit]


def _cap_or_none(value: object, limit: int) -> str | None:
    if value is None:
        return None
    text = _cap(value, limit)
    return text or None


def _json_or_none(value: object, limit: int, *, field: str = "value") -> str | None:
    """Serialize to bounded JSON, or **refuse**.

    Truncating at an arbitrary character is the wrong failure: an oversized but otherwise valid
    object becomes invalid JSON on disk and reads back as ``None``, so the caller is told the
    write succeeded and the data is silently gone. Over the bound is an error the caller sees.
    """
    if value is None:
        return None
    try:
        blob = json.dumps(value, sort_keys=True, default=str)
    except (TypeError, ValueError) as e:
        raise MissionError(f"{field} is not serialisable", status=422) from e
    if len(blob) > limit:
        raise MissionError(f"{field} is too large (max {limit} serialised chars)", status=422)
    return blob


def _loads(blob: object) -> Any:
    if not isinstance(blob, str) or not blob:
        return None
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        return None


def _row_to_mission(row) -> dict:
    return dict(row)


def _event_row(row) -> dict:
    d = dict(row)
    d["meta"] = _loads(d.get("meta"))
    d["settlement"] = None  # filled by the join in `get_mission`
    return d


def _objective_row(row) -> dict:
    d = dict(row)
    d["gate"] = bool(d.get("gate"))
    d["probe_args"] = _loads(d.get("probe_args"))
    # `observed` is stored as JSON like `probe_args` and, until #891, was handed back as the raw
    # STRING — because nothing wrote it, so nothing ever read it. The console's degraded rendering
    # (#878) checks `typeof observed === "object"` before showing "last seen … · stale", so a
    # string silently failed that check and the staleness could never have appeared. Parsed here,
    # beside its sibling, rather than at each consumer.
    d["observed"] = _loads(d.get("observed"))
    # THE IN-FLIGHT PROBE BINDING IS NOT PART OF THE ROW ANYONE READS. `probe_target` and
    # `probe_gen` exist so the settling transaction can fence a late answer (#897 re-review,
    # finding 2); handing them to the console and the API would publish an internal digest as
    # though it were mission state, and invite a client to send one back.
    d.pop("probe_target", None)
    d.pop("probe_gen", None)
    return d


# ---------------------------------------------------------------- event append (in-tx)


#: How many ids a server-built event meta may name before it summarises instead. A mission's
#: roster is operator-sized, but "operator-sized" is not a bound, and an event meta that grows
#: with it would make an ordinary archive fail on a size check.
META_LIST_MAX = 20


def _bounded_meta(meta: object) -> object:
    """Bound a server-built event meta so it cannot outgrow :data:`EVENT_META_MAX`.

    Only the list-valued fields grow (session rosters, applied ops); each is truncated to
    :data:`META_LIST_MAX` with an explicit ``…_total`` count beside it, so a summarised meta reads
    as a summary rather than as the whole story.
    """
    if not isinstance(meta, dict):
        return meta
    out: dict = {}
    for k, v in meta.items():
        if isinstance(v, list) and len(v) > META_LIST_MAX:
            out[k] = v[:META_LIST_MAX]
            out[f"{k}_total"] = len(v)
        else:
            out[k] = v
    return out


def _append_event(
    con,
    mission_id: str,
    kind: str,
    *,
    at: float | None = None,
    session_key: str | None = None,
    action_id: str | None = None,
    text: str | None = None,
    meta: object = None,
) -> int:
    if kind not in EVENT_KINDS:
        raise MissionError(f"unknown event kind {kind!r}")
    cur = con.execute(
        "INSERT INTO mission_events (mission_id, at, kind, session_key, action_id, text, meta) "
        "VALUES (?,?,?,?,?,?,?)",
        (
            mission_id,
            time.time() if at is None else at,
            kind,
            session_key,
            action_id,
            _cap_or_none(text, EVENT_TEXT_MAX),
            _json_or_none(_bounded_meta(meta), EVENT_META_MAX, field="event meta"),
        ),
    )
    _trim_events(con, mission_id)
    return int(cur.lastrowid)


#: Built once at import from :data:`EVENT_KINDS_PRESERVED` — no per-call SQL construction, and
#: the kind list is a module constant rather than anything a request can reach.
_PRESERVED_ORDER: tuple[str, ...] = tuple(sorted(EVENT_KINDS_PRESERVED))
# The only interpolation is a `?` run sized by a module constant; every value is bound.
_TRIM_SQL = (  # noqa: S608
    "DELETE FROM mission_events WHERE seq IN ("
    "  SELECT seq FROM mission_events"
    "  WHERE mission_id=? AND kind NOT IN ({placeholders})"
    "  ORDER BY seq ASC LIMIT ?)"
).format(placeholders=",".join("?" * len(_PRESERVED_ORDER)))


def append_event(
    mission_id: str,
    kind: str,
    *,
    session_key: str | None = None,
    action_id: str | None = None,
    text: str | None = None,
    meta: object = None,
    now: float | None = None,
    path: Path | None = None,
) -> int:
    """Append one timeline event. Returns its ``seq``.

    The public door for events that are not part of another operator-visible change — recaps,
    questions, probes. Anything that IS part of one (a transition, an adopt, an objective edit)
    appends inside that change's own transaction instead, so the timeline can never disagree with
    the state it describes.
    """
    validate_id(mission_id)
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            # The same lifecycle fence every other mutation gets. This is the public producer
            # later phases write recaps and probe results through, and "every other mutation is
            # fenced" has to include it — otherwise the supervisor keeps appending to a mission
            # whose agents are being torn down, or to one that is already archived.
            _fence_busy(con, mission_id)
            seq = _append_event(
                con,
                mission_id,
                kind,
                at=now,
                session_key=session_key,
                action_id=action_id,
                text=text,
                meta=meta,
            )
            if action_id:
                # ATOMIC with the reference it protects — see the docstring. A reference to a
                # settled action can never exist without the projection that outlives it.
                _project_reference_in_tx(con, action_id)
            con.execute(
                "UPDATE missions SET updated_at=? WHERE id=?",
                (time.time() if now is None else now, mission_id),
            )
            con.execute("COMMIT")
            return seq
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def _project_reference_in_tx(con, action_id: str) -> None:
    """Freeze a settled action's projection **in the same transaction as the reference to it**.

    Compaction asks which doomed actions a mission references and then rewrites the ledger — a
    check-then-act across two stores. An event committed in between creates a reference to a row
    compaction has already decided nobody points at. Fencing the two stores against each other
    would order their locks in both directions, which is the deadlock the settlement hook
    documents.

    Making the two writes **atomic** fixes it without any fence: the reference and its projection
    commit together, so "a reference exists" implies "a projection exists" no matter what
    compaction does next, and a rolled-back event leaves no orphaned projection either. Called
    *after* the event insert so the row is visible to the reference check.

    An action that is still LIVE needs nothing — compaction never removes a live row, and by the
    time it settles the reference is already there for the reference-scoped pass to find.

    **And when the source is already gone**, this records that fact rather than committing a
    reference with nothing behind it. Compaction can legitimately have dropped a terminal row
    before anything referenced it, and there are only three things to do about it: reject the
    operator's write because of ledger housekeeping, commit a decision that renders blank forever,
    or say plainly that the record was destroyed before this reference existed. The third is the
    only one that is both honest and non-destructive, and §16 already has a projection for it —
    *absent from the ledger ⇒ historical, no controls, and no outcome asserted*.
    """
    try:
        from . import orchestrator_ledger

        status, rec = orchestrator_ledger.lookup(action_id)
        terminal = orchestrator_ledger.TERMINAL_STATES
    except Exception:  # noqa: BLE001 — an unreadable ledger is not this write's problem
        return
    if status == "unreadable":
        # **Absent and unreadable are different answers**, and only one of them is permanent.
        # Freezing `source_compacted` on a transient EIO would record "we can never know what this
        # decision said" about a row sitting intact on disk. Leave it unprojected instead: the
        # reference stays retryable by the read-time backfill, and compaction cannot destroy the
        # row in the meantime because it declines when it cannot read the ledger either.
        return
    if rec is None:
        con.execute(
            "INSERT OR IGNORE INTO mission_settlements "
            "(action_id, verb, state, rationale, outcome, at) VALUES (?,?,?,?,?,?)",
            (
                action_id,
                None,
                SETTLEMENT_LOST,
                "the ledger record was compacted before anything referenced it",
                None,
                time.time(),
            ),
        )
        return
    if rec.get("state") not in terminal:
        return
    con.execute(
        "INSERT OR IGNORE INTO mission_settlements "
        "(action_id, verb, state, rationale, outcome, at) VALUES (?,?,?,?,?,?)",
        (
            action_id,
            _cap(rec.get("verb"), 40) or None,
            _cap(rec.get("state"), 40) or None,
            _cap(rec.get("rationale"), SETTLEMENT_RATIONALE_MAX) or None,
            _cap(rec.get("outcome") or rec.get("detail"), SETTLEMENT_OUTCOME_MAX) or None,
            float(rec.get("ts") or time.time()),
        ),
    )


def event_count(mission_id: str, *, path: Path | None = None) -> int:
    con = _ready(path)
    try:
        return int(
            con.execute(
                "SELECT COUNT(*) FROM mission_events WHERE mission_id=?", (mission_id,)
            ).fetchone()[0]
        )
    finally:
        con.close()


def _trim_events(con, mission_id: str) -> int:
    """Hold the per-mission event cap. Two bounds, in order.

    The **soft cap** drops only droppable kinds — recaps and probe noise — so the ordinary case
    never loses a decision, a state change or an objective edit.

    The **hard ceiling** (:func:`_trim_hard`) is what makes "capped" true rather than aspirational,
    because "preserved" would otherwise mean unbounded.
    """
    total = con.execute(
        "SELECT COUNT(*) FROM mission_events WHERE mission_id=?", (mission_id,)
    ).fetchone()[0]
    over = int(total) - MISSION_EVENTS_MAX
    if over <= 0:
        return 0
    dropped = con.execute(_TRIM_SQL, (mission_id, *_PRESERVED_ORDER, over)).rowcount or 0
    dropped += _trim_hard(con, mission_id)
    return dropped


def _trim_hard(con, mission_id: str) -> int:
    """The hard ceiling that makes the cap a *cap*.

    The soft cap protects decision / state / objective rows, but "protected" cannot mean
    "unbounded": an operator retitling one objective in a loop grew the timeline past the stated
    cap (measured at 521 against 500). Past this ceiling the protected kinds go too.

    That is safe **only because the settlement projection no longer lives on the row.** When it
    did, bounding the feed deleted the durability promise, and ordering projection-bearing rows
    last merely delayed it. With projections normalized into ``mission_settlements``, the timeline
    is a bounded *feed* and the decision's content is a separate, durable fact.

    Drop order is stated rather than incidental: the high-volume operator-generated kinds
    (``objective``, ``session``) go before the lifecycle ones, so what a reader loses first is the
    edit noise rather than the shape of what happened.

    The one row this ceiling will not take is a **question that is currently holding an
    objective** — see the query. That row is referenced state, not feed, and deleting it produces
    an objective nobody can release.
    """
    total = int(
        con.execute(
            "SELECT COUNT(*) FROM mission_events WHERE mission_id=?", (mission_id,)
        ).fetchone()[0]
    )
    over = total - MISSION_EVENTS_HARD_MAX
    if over <= 0:
        return 0
    doomed = con.execute(
        "SELECT seq, action_id FROM mission_events WHERE mission_id=? "
        # A QUESTION THAT IS STILL HOLDING AN OBJECTIVE IS NOT A FEED ROW (#900 review, finding 3).
        #
        # The hold stores the question's `seq` and both the display and the answer JOIN back to
        # this row for the text and the options. Deleting it leaves `question_seq` pointing at
        # nothing: `open_question_row` returns None, so no question is on screen, while the
        # objective stays held and `answer_question` can only 409 — an objective silenced by a
        # question the operator can neither see nor answer.
        #
        # It stays a bound: there is at most one hold per objective and objectives are capped, so
        # the ceiling is `MISSION_EVENTS_HARD_MAX` plus that count, not "unbounded again".
        "AND seq NOT IN ("
        "  SELECT question_seq FROM mission_objective_episode "
        "  WHERE mission_id=? AND question_seq IS NOT NULL) "
        "ORDER BY CASE kind WHEN 'objective' THEN 0 WHEN 'session' THEN 0 ELSE 1 END ASC, "
        "seq ASC LIMIT ?",
        (mission_id, mission_id, over),
    ).fetchall()
    if not doomed:
        return 0
    marks = ",".join("?" * len(doomed))
    con.execute(
        f"DELETE FROM mission_events WHERE seq IN ({marks})",  # noqa: S608
        tuple(r["seq"] for r in doomed),
    )
    _gc_settlements(con, [r["action_id"] for r in doomed if r["action_id"]])
    return len(doomed)


# ---------------------------------------------------------------- create / read


def create_mission(
    instruction: str,
    *,
    title: str = "",
    project_id: str | None = None,
    cwd: str | None = None,
    engine: str | None = None,
    engine_source: str | None = None,
    playbook_id: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Create a ``draft`` from an instruction. One transaction: the row and its first event.

    ``cwd`` stays NULL when the project is not resolved yet — that is the state in which the
    console asks which project was meant, and the schema ``CHECK`` is what stops it launching.
    """
    text = _cap(instruction, INSTRUCTION_MAX)
    if not text:
        raise MissionError("instruction is required", status=422)
    ts = time.time() if now is None else now
    mission_id = new_id()
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO missions (id, title, instruction, brief, project_id, cwd, engine,"
                " engine_source, state, playbook_id, created_at, updated_at,"
                " objectives_state, objectives_at, plan_state, plan_generation, plan_at) "
                # BOTH INTENTS IN THE CREATE TRANSACTION (#883, #967). A crash between this commit
                # and the background producers can then never lose either: `recover_pending`
                # finds a `pending` row. Generation 1 is the planning attempt the create starts.
                "VALUES (?,?,?,?,?,?,?,?,'draft',?,?,?,'pending',?,'pending',1,?)",
                (
                    mission_id,
                    _cap(title, TITLE_MAX) or text[:TITLE_MAX],
                    text,
                    None,
                    _cap_or_none(project_id, 200),
                    _cap_or_none(cwd, 4096),
                    _cap_or_none(engine, 40),
                    _cap_or_none(engine_source, 20),
                    _cap_or_none(playbook_id, 200),
                    ts,
                    ts,
                    ts,
                    ts,
                ),
            )
            _append_event(con, mission_id, "operator_msg", at=ts, text=text)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return get_mission(mission_id, path=path) or {}


def get_mission(
    mission_id: str,
    *,
    events_limit: int = EVENTS_PAGE_DEFAULT,
    events_before_seq: int | None = None,
    attention: bool = False,
    path: Path | None = None,
) -> dict | None:
    """One mission with its roster, objectives and a bounded timeline page.

    The timeline pages **newest-first on ``seq``**, not on an offset: ``seq`` is monotonic and
    a concurrent append therefore cannot shift a page under the reader.

    ``attention=True`` also returns ``needs_you`` / ``needs_you_why`` / ``question`` **from this
    same transaction** (#900 review 8, finding 1). Reading them beside this call rather than
    inside it was a torn answer with a subtler shape than the one before it: the flag and the
    question agreed with EACH OTHER and disagreed with the TIMELINE returned next to them, so a
    question opening between the two reads produced a 200 carrying an actionable question whose
    own event was not in the `events` array — an answer the console could offer and then not
    show.

    The LEDGER and SIDECAR terms are folded on afterwards, deliberately: they live outside this
    store, so no transaction here could cover them, and they answer a different question ("is an
    action pending on one of this mission's sessions").
    """
    validate_id(mission_id)
    limit = max(1, min(EVENTS_PAGE_MAX, int(events_limit or EVENTS_PAGE_DEFAULT)))
    con = _ready(path)
    try:
        # ONE snapshot for the whole response. In autocommit each SELECT sees its own snapshot,
        # so a legal transition committing between them returned `state="running"` beside a
        # `done` event and a released session — a mission whose parts disagree, which is exactly
        # what the timeline exists to prevent. `BEGIN DEFERRED` starts the read transaction on
        # the first SELECT and holds it across all four; WAL means it blocks no writer.
        con.execute("BEGIN DEFERRED")
        row = con.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
        if row is None:
            con.execute("COMMIT")
            return None
        if events_before_seq is None:
            events = con.execute(
                "SELECT * FROM mission_events WHERE mission_id=? ORDER BY seq DESC LIMIT ?",
                (mission_id, limit),
            ).fetchall()
        else:
            events = con.execute(
                "SELECT * FROM mission_events WHERE mission_id=? AND seq<? "
                "ORDER BY seq DESC LIMIT ?",
                (mission_id, int(events_before_seq), limit),
            ).fetchall()
        sessions = con.execute(
            "SELECT * FROM mission_sessions WHERE mission_id=? ORDER BY added_at ASC",
            (mission_id,),
        ).fetchall()
        objectives = con.execute(
            "SELECT * FROM mission_objectives WHERE mission_id=? ORDER BY ord ASC",
            (mission_id,),
        ).fetchall()
        # THE OPEN TURN, IN THE SAME SNAPSHOT AS THE TIMELINE (#902 review 2, finding 3).
        #
        # Read on its own connection it was a torn answer: a claim committing between the two
        # returned a turn with no operator event, and a settlement between them returned
        # `turn: null` beside a timeline that did not yet carry the answer — a mission whose
        # parts disagree, which is the whole reason this function holds one transaction.
        turn = con.execute(
            "SELECT turn_id, state, result_meta, created_at, message "
            "FROM mission_turns "
            "WHERE mission_id=? AND state IN ('in_progress','indeterminate') "
            "  AND acked_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (mission_id,),
        ).fetchone()
        # THE ATTENTION PROJECTION, IN THIS SAME SNAPSHOT (#900 review 8, finding 1).
        attention_rows = _attention_rows(con, [mission_id]) if attention else None
        question = _open_question_row(con, mission_id) if attention else None
        # START AGAIN, from the same predicate the state write uses and in this same snapshot
        # (#966). Advisory: the write re-reads it under its own transaction.
        retry = _retry_verdict(con, mission_id)
        con.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    mission = _row_to_mission(row)
    mission["sessions"] = [dict(s) for s in sessions]
    mission["objectives"] = [_objective_row(o) for o in objectives]
    mission["events"] = [_event_row(e) for e in events]
    _attach_settlements(mission["events"], path=path)
    # Anything neither compaction nor the settlement hook froze is reconciled here while the
    # ledger row still exists, then joined again so the response carries it.
    if _backfill_settlements(mission["events"], path=path):
        _attach_settlements(mission["events"], path=path)
    mission["events_next_seq"] = mission["events"][-1]["seq"] if len(events) == limit else None
    mission["turn"] = _turn_row(turn)
    mission["retry_eligible"] = bool(retry["eligible"])
    mission["seed_outcome"] = retry["seed_outcome"]
    mission["retry_reason"] = (
        retry["reason"] if mission.get("state") == "failed" and not retry["eligible"] else None
    )
    if attention_rows is not None:
        merged = _attention_merge(
            {mission_id: {"needs_you": False, "why": []}}, [mission_id], *attention_rows
        )[mission_id]
        mission["needs_you"] = bool(merged["needs_you"])
        mission["needs_you_why"] = merged["why"]
        mission["question"] = question
    return mission


# Two constants rather than one query with a parameterised scope: `(archived_at IS NOT NULL) = ?`
# reads neatly and is not sargable — it defeats `missions_by_archived` and turns every list into a
# full scan. No dynamic SQL either way.
_LIST_LIVE_SQL = "SELECT * FROM missions WHERE archived_at IS NULL ORDER BY updated_at DESC"
_LIST_ARCHIVED_SQL = "SELECT * FROM missions WHERE archived_at IS NOT NULL ORDER BY updated_at DESC"


def _attach_settlements(events: list[dict], *, path: Path | None = None) -> None:
    """Join each decision event to its frozen projection.

    A plain read-time join now, because the projection lives in its own table: nothing has to be
    written during a read to make an old decision render.
    """
    ids = [e["action_id"] for e in events if e["action_id"]]
    if not ids:
        return
    with contextlib.suppress(Exception):  # a read degrades, never fails
        found = settlements_for(ids, path=path)
        for e in events:
            row = found.get(e["action_id"])
            if row is not None:
                e["settlement"] = {
                    "verb": row["verb"],
                    "state": row["state"],
                    "rationale": row["rationale"],
                    "outcome": row["outcome"],
                    "at": row["at"],
                }


def _backfill_settlements(events: list[dict], *, path: Path | None = None) -> int:
    """Fill in any projection that was never frozen, while the ledger row still exists.

    Three things now write a projection, in decreasing order of how much they can be relied on:

    1. :func:`orchestrator_ledger.compact` freezes everything about to fall out of the tail. That
       is the **durable** one — compaction is the only thing that ever removes a ledger row, so it
       is the one moment at which "this is about to become unavailable" is knowable.
    2. ``_settled`` freezes each action as it settles. Best-effort by design: a missions failure
       must never fail or undo a settled ledger transition.
    3. This, on read, for anything the other two missed while the ledger row is still there.

    (1) is what makes the promise hold; (2) and (3) are the cheap paths that mean it is almost
    never needed. Best-effort in its turn — this runs on a read path.
    """
    pending = [
        e["action_id"]
        for e in events
        if e["action_id"]
        and (
            e["settlement"] is None
            # A marker is a placeholder, not an answer — retry it while the ledger row may exist.
            or (e["settlement"] or {}).get("state") == SETTLEMENT_LOST
        )
    ]
    if not pending:
        return 0
    filled = 0
    try:
        from . import orchestrator_ledger

        latest = orchestrator_ledger.latest_by_id()
        for action_id in pending:
            rec = latest.get(action_id)
            if rec is None or rec.get("state") not in orchestrator_ledger.TERMINAL_STATES:
                continue  # still live: the ledger IS the answer, no projection needed yet
            filled += record_settlement(action_id, rec, path=path)
    except Exception:  # noqa: BLE001 — a reconciliation hiccup must never fail a read
        return filled
    return filled


def _snapshot_digest(rows: list[dict]) -> str:
    """A digest of the ORDERED ids a page was sliced out of — the comparand for a stitched read.

    The order is part of it, so a reorder that preserves the set still changes the answer: the
    thing a client needs to know is whether OFFSETS meant the same rows, not whether the same
    missions exist.
    """
    h = hashlib.sha256()
    for r in rows:
        h.update(str(r.get("id") or "").encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def list_missions(
    *,
    q: str = "",
    project_id: str = "",
    state: str = "",
    archived: bool = False,
    limit: int = LIST_LIMIT_DEFAULT,
    offset: int = 0,
    path: Path | None = None,
) -> dict:
    """Missions newest-updated-first, plus facets and a filtered ``total``.

    Filters apply to the **full** archived-scoped set **before** ``limit``/``offset``, so ``total``
    and any "load more" describe the *filtered* result — the same rule ``/api/sessions`` follows.
    Facets are computed over the full scoped set **before** the filters, so the dropdowns keep
    listing every option regardless of the current filter.
    """
    lim = max(1, min(LIST_LIMIT_MAX, int(limit or LIST_LIMIT_DEFAULT)))
    off = max(0, int(offset or 0))
    con = _ready(path)
    try:
        scoped = con.execute(_LIST_ARCHIVED_SQL if archived else _LIST_LIVE_SQL).fetchall()
        # `session_keys`, NOT the `sessions` roster. A list row is not a detail row, and the
        # console proved why: it iterated `m.sessions` on a list row, which has never had one,
        # so any non-empty production list threw instead of rendering the rail.
        #
        # Keys rather than a bare count because the console needs BOTH questions answered and
        # they are the same fact: how many sessions a mission holds (the rail's line), and which
        # sessions are held at all (so a session tracked by ANOTHER mission is not offered as
        # untracked). A count alone would answer the first and silently get the second wrong.
        # One query over an indexed partial set, never an N+1.
        keys: dict[str, list[str]] = {}
        for r in con.execute(
            "SELECT mission_id, session_key FROM mission_sessions WHERE removed_at IS NULL"
        ).fetchall():
            keys.setdefault(r["mission_id"], []).append(r["session_key"])
    finally:
        con.close()
    rows = [_row_to_mission(r) for r in scoped]
    for r in rows:
        r["session_keys"] = keys.get(r["id"], [])
    facets = {
        "projects": sorted({r["project_id"] for r in rows if r.get("project_id")}),
        "states": sorted({r["state"] for r in rows if r.get("state")}),
    }
    needle = (q or "").strip().lower()
    filtered = [
        r
        for r in rows
        if (not needle or needle in (r.get("title") or "").lower())
        and (not project_id or r.get("project_id") == project_id)
        and (not state or r.get("state") == state)
    ]
    return {
        "missions": filtered[off : off + lim],
        "total": len(filtered),
        "limit": lim,
        "offset": off,
        "facets": facets,
        # THE SNAPSHOT THIS PAGE WAS SLICED OUT OF (#896 review 19, finding 1).
        #
        # Offsets only compose if the thing they index has not moved, and a client stitching
        # several pages had no way to establish that. Deduplication catches a REORDER — a
        # repeated id cannot occur inside one snapshot — and cannot catch a REMOVAL: archive one
        # mission between page 0 and page 1 and the second page starts one row late, so the rail
        # ends up count-consistent, duplicate-free, holding a stale row and permanently missing
        # a live one. A duplicate is proof of tearing; the absence of one is not proof of a
        # snapshot.
        #
        # So the page carries a digest of the ORDERED ids of the full filtered set it was cut
        # from. Equal digests across two pages is a proof rather than a heuristic: the sequence
        # the offsets index into was identical, so the pages compose exactly. Any insert,
        # removal or reorder changes it.
        #
        # Over `filtered`, not the scoped set: the client compares digests only between pages of
        # one read, which by construction carry the same filters.
        "snapshot": _snapshot_digest(filtered),
    }


def delete_mission(mission_id: str, *, path: Path | None = None) -> bool:
    """Delete a mission and everything it owns — a row delete, never a tombstone.

    ``instruction`` / ``brief`` / recaps are sensitive operator text, so "deleted" has to mean
    the bytes are gone, not that a flag was flipped. ``ON DELETE CASCADE`` plus
    ``foreign_keys=ON`` takes the children.
    """
    validate_id(mission_id)
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            # Never delete a mission mid-operation. Retention already refuses this; the same
            # hazard applies here — the row carries the archive/unarchive journal recovery works
            # from, and the roster naming the agents it was about to reap.
            row = con.execute(
                "SELECT archiving_at, archived_at, unarchiving_at FROM missions WHERE id=?",
                (mission_id,),
            ).fetchone()
            if row is not None and (
                (row["archiving_at"] is not None and row["archived_at"] is None)
                or row["unarchiving_at"] is not None
            ):
                con.execute("ROLLBACK")
                raise MissionError(
                    f"mission {mission_id}: an archive operation is in flight", status=409
                )
            # Capture the action ids this mission's events reference BEFORE the cascade takes
            # them, so the orphaned projections can be collected afterwards — `rationale` is
            # bounded model text about the operator's work and is deleted with the mission like
            # everything else here.
            referenced = [
                r["action_id"]
                for r in con.execute(
                    "SELECT DISTINCT action_id FROM mission_events "
                    "WHERE mission_id=? AND action_id IS NOT NULL",
                    (mission_id,),
                ).fetchall()
            ]
            cur = con.execute("DELETE FROM missions WHERE id=?", (mission_id,))
            deleted = bool(cur.rowcount)
            _gc_settlements(con, referenced)
            con.execute("COMMIT")
            if not deleted:
                # Nothing deleted here, but an EARLIER delete may still owe a scrub — and this is
                # exactly the call that used to return before reaching it, making the advertised
                # retry a no-op.
                if _flag_get(con, SCRUB_PENDING) is not None and not _scrub(con):
                    raise ScrubFailed("an earlier deletion")
                return False
            if not _scrub(con):
                # The rows ARE gone — that transaction committed — but the bytes are not provably
                # off disk, and this function's whole contract is physical deletion. Say so.
                raise ScrubFailed(mission_id)
            return True
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


class ScrubFailed(MissionError):
    """The rows are gone but the bytes were not provably scrubbed. 503 — say so, never imply it."""

    def __init__(self, mission_ids: str) -> None:
        super().__init__(
            f"rows deleted for {mission_ids}, but the store could not be scrubbed "
            f"(a reader is pinning the log); retry",
            status=503,
        )


#: How long to keep trying to truncate the log before admitting the scrub did not happen. Short:
#: the only thing that can block it is another connection holding a read snapshot, and those are
#: request-scoped.
_SCRUB_ATTEMPTS = 20
_SCRUB_PAUSE_S = 0.05


SCRUB_PENDING = "scrub_pending"


def _scrub(con) -> bool:
    """Push the delete all the way to disk, across every file the store owns. Returns success.

    ``secure_delete`` zeroes freed content in the **main database**. It says nothing about the
    write-ahead log, which still holds the pre-delete page images — and while any connection holds
    a read snapshot the log cannot be truncated at all. So a checkpoint that comes back **busy**
    means the plaintext is still on disk, and treating that as success is a claim of physical
    deletion the code did not make good on.

    **A failure leaves a durable obligation**, because "retry" was otherwise a lie: the rows are
    already deleted, so the next call returns early and never reaches the scrub at all. The flag
    is what gives a later delete, retention pass or boot something to act on.
    """
    ok = False
    # **The loop is the waiting strategy, so the checkpoint must REPORT busy rather than block.**
    # The budget below reads as 20 attempts 50ms apart — about a second. That was never the budget
    # that ran: the connection carries a 5s `busy_timeout`, and `wal_checkpoint` honours it, so
    # every attempt sat through five seconds and a one-second budget became a hundred-second one.
    # The pacing here was dead code. Dropping the timeout for the checkpoint gives the loop its
    # own pacing back and gives a briefly-contended log twenty chances instead of one long wait.
    with contextlib.suppress(sqlite3.Error):
        con.execute("PRAGMA busy_timeout=0")
    try:
        for attempt in range(_SCRUB_ATTEMPTS):
            try:
                row = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            except sqlite3.Error:
                break
            if row is None or int(row[0]) == 0:
                ok = True
                break
            if attempt + 1 < _SCRUB_ATTEMPTS:
                time.sleep(_SCRUB_PAUSE_S)
    finally:
        # Restored BEFORE the flag write below, which is an ordinary contended write and does
        # want to wait rather than fail fast.
        with contextlib.suppress(sqlite3.Error):
            con.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_MS)}")
    with contextlib.suppress(sqlite3.Error):
        _flag_set(con, SCRUB_PENDING, None if ok else "1")
    return ok


def scrub_if_pending(*, path: Path | None = None) -> bool | None:
    """Discharge an outstanding scrub. ``None`` when there was nothing owed.

    Called wherever a scrub could next succeed — a later delete, the retention pass, and boot —
    so an obligation created by a busy reader is actually collected rather than merely announced.
    """
    with _write_lock:
        con = _ready(path)
        try:
            if _flag_get(con, SCRUB_PENDING) is None:
                return None
            return _scrub(con)
        finally:
            con.close()


# ---------------------------------------------------------------- lifecycle


def _fence_busy(con, mission_id: str) -> None:
    """Refuse an ordinary mutation on a mission that is archived or mid-operation.

    Called INSIDE the transaction rather than before it so it cannot be raced: an adopt that
    slipped past a pre-check would attach a session to a mission whose teardown loop is already
    killing process groups, and a detach would clear the very ``removed_at`` bookkeeping the
    archive is part-way through writing.

    **Archived counts too.** Fencing only the in-flight window left an archived mission fully
    mutable: ``done → running`` succeeded with ``archived_at`` still set, producing a mission
    that reads *running* and appears only in the archived scope — invisible on the rail while
    claiming to be live. Unarchive is the required predecessor, and it is the one mutation that
    is allowed to act on an archived record (it does not call this).
    """
    row = con.execute(
        "SELECT archiving_at, archived_at, unarchiving_at FROM missions WHERE id=?",
        (mission_id,),
    ).fetchone()
    if row is None:
        raise MissionNotFound(mission_id)
    if row["archiving_at"] is not None and row["archived_at"] is None:
        raise MissionError(f"mission {mission_id}: archive in progress", status=409)
    if row["unarchiving_at"] is not None:
        raise MissionError(f"mission {mission_id}: unarchive in progress", status=409)
    if row["archived_at"] is not None:
        raise MissionError(f"mission {mission_id} is archived; unarchive it first", status=409)


def set_state(
    mission_id: str,
    from_state: str,
    to_state: str,
    *,
    outcome: str | None = None,
    detail: str = "",
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Compare-and-set the lifecycle column, with its ``state`` event, in one transaction.

    ``UPDATE … WHERE id=? AND state=?``: **a zero rowcount is a lost race, not a retry.** A state
    read before an ``await`` cannot be trusted after it — the lesson the ledger's
    ``compare_and_set`` already carries.

    Reaching a terminal state **releases** every session the mission holds (``removed_at`` set);
    the rows stay, so the roster keeps its history. **Reopening re-acquires nothing** — in the
    meantime another mission may legitimately hold those sessions, and silently taking them back
    would either steal one or fail the reopen for a reason unrelated to reopening. Re-attaching is
    the ordinary adopt path, which already answers 409 naming the holder.
    """
    validate_id(mission_id)
    # Type first, then membership. `x in frozenset` raises `TypeError: unhashable type` for a
    # dict or list, which escapes as a 500 — a malformed request body is a 422, always.
    to_state = _require_str(to_state, "to")
    from_state = _require_str(from_state, "from")
    if to_state not in STATES:
        raise MissionError(f"unknown state {to_state!r}", status=422)
    if from_state not in STATES:
        raise MissionError(f"unknown state {from_state!r}", status=422)
    if to_state not in _ALLOWED.get(from_state, frozenset()):
        raise MissionError(f"{from_state} cannot become {to_state}", status=409)
    if outcome is not None:
        outcome = _require_str(outcome, "outcome")
        if outcome not in OUTCOMES:
            raise MissionError(f"unknown outcome {outcome!r}", status=422)
        # An outcome is the *reason a mission closed*, so it has to agree with the state it is
        # recorded against. Validating it against the global set alone let through records like
        # `state=planned, outcome=failed` and `state=done, outcome=abandoned` — a timeline that
        # contradicts itself, which is precisely what the timeline exists to prevent.
        if to_state not in TERMINAL_STATES:
            raise MissionError(
                f"outcome is only meaningful on a terminal state, not {to_state}", status=422
            )
        if outcome != to_state:
            raise MissionError(f"outcome {outcome!r} does not match state {to_state!r}", status=422)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            row = con.execute(
                "SELECT state, cwd FROM missions WHERE id=?", (mission_id,)
            ).fetchone()
            if row is None:
                raise MissionNotFound(mission_id)
            # The schema CHECK exempts draft/planned/abandoned only, so a cwd-less mission
            # cannot become `failed` without the write blowing up at the database. Refuse it
            # here with a reason rather than letting an IntegrityError surface as a 500 — and
            # keep the CHECK as written: a plan that never launched was not `failed`, it was
            # `abandoned`, which IS available.
            if to_state not in CWD_OPTIONAL_STATES and row["cwd"] is None:
                raise MissionError(
                    f"mission {mission_id}: cannot become {to_state} without a resolved cwd "
                    f"(abandon it instead)",
                    status=409,
                )
            # THE ADOPTED PATH'S GUARD (#889). `planned -> running` exists so a session the
            # operator started themselves can be tracked without inventing a dispatch — so it
            # means nothing unless the mission actually holds one. Checked INSIDE the transaction
            # against the same connection that is about to write, because "read the roster, then
            # set the state" is two moments and a concurrent detach lands between them.
            #
            # Deliberately not applied to `dispatching -> running`: there, the session is created
            # BY the dispatch and its association is written by that path.
            if from_state == "planned" and to_state == "running":
                live = con.execute(
                    "SELECT 1 FROM mission_sessions "
                    "WHERE mission_id=? AND removed_at IS NULL LIMIT 1",
                    (mission_id,),
                ).fetchone()
                if live is None:
                    # No explicit ROLLBACK: the `except BaseException` below owns it, exactly as
                    # the cwd check above relies on. The two refusals are the same shape and must
                    # stay that way — a second rollback path is a second thing to get wrong.
                    raise MissionError(
                        f"mission {mission_id}: adopt a session before marking it running "
                        f"(a running mission with no session has nothing to follow through on)",
                        status=409,
                    )
            # START AGAIN (#966). Decided HERE, on the evidence as this transaction reads it, never
            # on what the caller last saw. A mission that is no longer `failed` falls through to
            # the CAS below, which refuses it with the ordinary lost-race answer.
            restart: dict | None = None
            if from_state == "failed" and to_state == "planned" and row["state"] == "failed":
                restart = _retry_verdict(con, mission_id)
                if not restart["eligible"]:
                    raise MissionError(
                        f"mission {mission_id} cannot be started again: {restart['reason']}",
                        status=409,
                    )
            terminal = to_state in TERMINAL_STATES
            cur = con.execute(
                # `outcome` is CLEARED on a non-terminal transition rather than carried forward.
                # `COALESCE(?, outcome)` kept it, so reopening a finished mission produced
                # `state='running', outcome='done'` — a record that says both "in flight" and
                # "finished, successfully". An outcome is the reason a mission CLOSED; a mission
                # that is open has not got one.
                "UPDATE missions SET state=?, updated_at=?, "
                "outcome=CASE WHEN ? THEN ? ELSE NULL END, "
                "closed_at=CASE WHEN ? THEN ? ELSE NULL END WHERE id=? AND state=?",
                (
                    to_state,
                    ts,
                    1 if terminal else 0,
                    outcome,
                    1 if terminal else 0,
                    ts,
                    mission_id,
                    from_state,
                ),
            )
            if not cur.rowcount:
                con.execute("ROLLBACK")
                raise MissionError(f"mission {mission_id} is no longer {from_state}", status=409)
            restored_plan: str | None = None
            if restart is not None:
                restored_plan = _restore_plan_for_start_again_tx(
                    con, mission_id, restart["evidence"], ts
                )
            # THE EVIDENCE IS ABOUT ONE FAILURE. Any lifecycle move supersedes it, and Start again
            # consumes it, so it can never justify a second reopening or outlive a mission that
            # went on to run (#966).
            con.execute("DELETE FROM mission_dispatch_evidence WHERE mission_id=?", (mission_id,))
            # A QUESTION IS ONLY WORTH ASKING ON A MISSION AN ANSWER CAN ACT ON (#900 review 6,
            # finding 3). Clearing the holds only for TERMINAL states left `review` stranding
            # them: `propose_completion` moves a mission there while another objective still has
            # a question, `needs_you` kept reporting it, and `_question_answerable` then rejected
            # every answer — flagged for a decision the server refuses to take.
            #
            # The predicate is the same one that decides whether a question may be OPENED, so the
            # two cannot disagree about which states are answerable.
            if to_state in UNQUESTIONABLE_STATES:
                con.execute(
                    "UPDATE mission_objective_episode SET question_seq=NULL "
                    "WHERE mission_id=? AND question_seq IS NOT NULL",
                    (mission_id,),
                )
            released: list[str] = []
            if to_state in TERMINAL_STATES:
                held = con.execute(
                    "SELECT session_key FROM mission_sessions "
                    "WHERE mission_id=? AND removed_at IS NULL",
                    (mission_id,),
                ).fetchall()
                released = [h["session_key"] for h in held]
                con.execute(
                    "UPDATE mission_sessions SET removed_at=?, release_reason='closed' "
                    "WHERE mission_id=? AND removed_at IS NULL",
                    (ts, mission_id),
                )
            _append_event(
                con,
                mission_id,
                "state",
                at=ts,
                text=_cap(detail, EVENT_TEXT_MAX) or None,
                meta={
                    "from": from_state,
                    "to": to_state,
                    "released": released,
                    **(
                        {
                            "start_again": True,
                            "seed_outcome": restart["seed_outcome"],
                            "plan_id": restored_plan,
                        }
                        if restart is not None
                        else {}
                    ),
                },
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return get_mission(mission_id, path=path) or {}


# ---------------------------------------------------------------- session ownership


def adopt(
    mission_id: str,
    session_key: str,
    *,
    role: str = "primary",
    spawned_by: str | None = None,
    expect_token: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Attach a session to a mission. One transaction: the row and its ``session`` event.

    Exclusivity is the **database's** job. "Check whether anything holds this, then insert" is not
    race-safe — two concurrent adopts both pass the check — so the partial unique index
    ``mission_sessions_active`` is the arbiter and the loser catches ``IntegrityError`` and gets a
    409 naming the holder.

    Re-adopting into the **same** mission is an UPDATE, not an INSERT: the composite primary key
    means the historical row is still there after a detach, so a fresh insert would collide with
    the mission's own history rather than with another mission's claim.

    **There is deliberately no lifecycle predicate here** (#904 review 3). A closed mission taking
    a session back is a legitimate operator act — reopening the record of work that turned out not
    to be finished — and barring it would break the case #895's teardown tests pin, where a
    `done` mission still holds the sessions it ran. The window the review found is the DISPATCH
    one: a launch awaits for tens of seconds while the operator can abandon the mission in one
    tap, and the late adopt then attaches a live unattended agent to a mission that is
    over. That predicate belongs where the two halves are one act — `settle_dispatch`, which
    adopts and transitions under a single ``state='dispatching'`` comparand — not on every adopt
    in the app.
    ``expect_token`` is the caller's RESERVATION, checked in this transaction (#896 review 21).
    A caller that reserved the session before doing slow work must prove it still holds the same
    claim at the moment of the insert — a heartbeat reduces the chance of losing one, it does not
    detect having lost it.
    """
    validate_id(mission_id)
    role = _require_str(role, "role")
    if role not in SESSION_ROLES:
        raise MissionError(f"unknown role {role!r}", status=422)
    key = (session_key or "").strip()
    if not key:
        raise MissionError("session_key is required", status=422)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _adopt_tx(con, mission_id, key, role, spawned_by, ts, path, expect_token)
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    # Read AFTER the lock is released and the connection is closed. `get_mission` opens its own
    # connection and may write a settlement backfill; issuing that while this thread still holds
    # the write lock and an open connection is a knot worth not tying.
    return get_mission(mission_id, path=path) or {}


def _adopt_tx(
    con,
    mission_id: str,
    key: str,
    role: str,
    spawned_by,
    ts: float,
    path,
    expect_token: str | None = None,
) -> None:
    """The adopt transaction. Caller owns BEGIN/COMMIT so the read can happen outside the lock."""
    _fence_busy(con, mission_id)
    # THE FENCING TOKEN, IN THIS TRANSACTION (#896 review 21).
    #
    # A caller that reserved the session and then did slow work — ADOPT's eligibility scan reaches
    # the engines and the filesystem — can LOSE that reservation while it works: the heartbeat's
    # writes can fail until the row ages out, and a rival then reclaims it, archives the session
    # and releases its own row. The holder check below is a check on a STRING, so it sees nothing
    # and the original operation adopts a session that has since been archived.
    #
    # A heartbeat reduces the likelihood of expiry; it is not a fencing check after expiry has
    # occurred. The token is: it changes on every reclaim, so requiring the exact one turns
    # "nobody else holds it now" into "nobody has held it since I took it".
    if expect_token is not None:
        mine = con.execute(
            "SELECT token, at FROM session_reservations WHERE session_key=?", (key,)
        ).fetchone()
        if (
            mine is None
            or str(mine["token"]) != expect_token
            or float(mine["at"] or 0) < ts - RESERVATION_MAX_AGE_S
        ):
            raise MissionError(
                f"the reservation on {key} was lost while this adoption was being prepared; "
                "try again",
                status=409,
            )
    # A CLOSED MISSION HOLDS NOTHING (#896 review 20, finding 2).
    #
    # Reaching `done` / `failed` / `abandoned` RELEASES the roster — that is what the terminal
    # transition is for — so adopting into one puts an active session on a mission nobody is
    # following through on: the supervisor will not nudge it, the board does not render it, and
    # the mission reads finished while owning live work. `_fence_busy` covers archived and
    # mid-operation missions and deliberately not this, because a terminal state is a legal
    # resting place rather than an in-flight one; it needs its own refusal, and the refusal has
    # to name the way out.
    #
    # At the STORE boundary, not in the console: the rail picking an eligible mission is the good
    # affordance, and it is not the guarantee — the route is reachable without it.
    st = con.execute("SELECT state FROM missions WHERE id=?", (mission_id,)).fetchone()
    if st is not None and st["state"] in TERMINAL_STATES:
        raise MissionError(
            f"mission {mission_id} is {st['state']}; reopen it before adopting a session",
            status=409,
        )
    # A leased teardown is a RESERVATION on the session key, and adoption has to honour it.
    # Reaching a terminal state already set `removed_at`, so the partial unique index does not
    # stop this insert — and the archive worker that holds the lease is about to call
    # `cleanup_runtime` and `prov.archive` on that very session. Adopting it here would hand a
    # live session to this mission moments before another mission's worker kills it. The lease is
    # released the instant the teardown settles, so this window is exactly as long as the
    # destructive effect.
    busy = con.execute(_RESERVED_SQL, (key, *_RESERVED_ORDER)).fetchone()
    if busy is not None:
        raise MissionError(
            f"session {key} is {_RESERVED_WHY[busy['archive_state']]} "
            f"by mission {busy['mission_id']}",
            status=409,
        )
    # A session the SIBLING ROUTES are mid-archiving is equally off limits — the reservation is
    # the mutex, and adoption is a mutation of who may touch the provider state. Checking only
    # the mission-side archive states left the other direction open: a mission could adopt a
    # session while `POST /api/sessions/{id}/archive` was inside `cleanup_runtime` for it.
    held = con.execute(
        "SELECT holder, at FROM session_reservations WHERE session_key=?", (key,)
    ).fetchone()
    if held is not None and float(held["at"] or 0) >= ts - RESERVATION_MAX_AGE_S:
        if not str(held["holder"]).startswith(f"mission:{mission_id}"):
            raise MissionError(f"session {key} is being changed by {held['holder']}", status=409)
    mine = con.execute(
        "SELECT removed_at FROM mission_sessions WHERE mission_id=? AND session_key=?",
        (mission_id, key),
    ).fetchone()
    if mine is not None and mine["removed_at"] is None:
        # Already ours: idempotent. The role is refreshed (a primary can be re-declared a sub)
        # but `added_at` is not — the roster's order is history, and re-adopting a session you
        # already hold is not re-joining it.
        con.execute(
            "UPDATE mission_sessions SET role=?, spawned_by=? WHERE mission_id=? AND session_key=?",
            (role, spawned_by, mission_id, key),
        )
        return
    # NOTHING ABOUT THE RUNTIME IS WRITTEN HERE (#994 review 3). Where a late-bound session runs is
    # a fact about its master, kept in `session_runtime_bindings` and keyed by the session — so it
    # already travels with the session across a release and a second mission's adoption, and it
    # survives retention deleting this mission's history.
    try:
        if mine is not None:
            con.execute(
                "UPDATE mission_sessions SET removed_at=NULL, release_reason=NULL, added_at=?, "
                "role=?, spawned_by=?, archive_state=NULL, archive_error=NULL "
                "WHERE mission_id=? AND session_key=?",
                (ts, role, spawned_by, mission_id, key),
            )
        else:
            con.execute(
                "INSERT INTO mission_sessions "
                "(mission_id, session_key, role, spawned_by, added_at) VALUES (?,?,?,?,?)",
                (mission_id, key, role, spawned_by, ts),
            )
    except sqlite3.IntegrityError:
        # The partial unique index refused it, so somebody else holds it. Name them — from THIS
        # connection, after rolling back, rather than opening a second one under the same lock.
        con.execute("ROLLBACK")
        holder = con.execute(
            "SELECT mission_id FROM mission_sessions "
            "WHERE session_key=? AND removed_at IS NULL LIMIT 1",
            (key,),
        ).fetchone()
        raise SessionHeld(key, holder["mission_id"] if holder else "another mission") from None
    _append_event(con, mission_id, "session", at=ts, session_key=key, meta={"adopted": role})
    con.execute("UPDATE missions SET updated_at=? WHERE id=?", (ts, mission_id))


def _holder_of(session_key: str, *, path: Path | None = None) -> str | None:
    con = _ready(path)
    try:
        # BY EITHER NAME (#989). A late-bound session is held under the real id it revealed and
        # runs under the placeholder it was launched with, and a teardown of that launch asks by
        # the placeholder. Matching only `session_key` answered "nobody" for a session a mission
        # had just adopted — and the teardown that trusts this answer would have stopped it.
        row = con.execute(
            "SELECT mission_id FROM mission_sessions WHERE removed_at IS NULL AND ("
            "  session_key=? OR session_key IN ("
            "    SELECT logical_key FROM session_runtime_bindings WHERE physical_key=?)"
            ") LIMIT 1",
            (session_key, session_key),
        ).fetchone()
        return row["mission_id"] if row else None
    finally:
        con.close()


class SessionBusy(MissionError):
    """Somebody else holds the right to mutate this session's provider state. 409."""

    def __init__(self, session_key: str, why: str) -> None:
        super().__init__(f"session {session_key}: {why}", status=409)


class OwnershipUnknown(MissionError):
    """The store that records ownership could not be read. 503 — and nothing is destroyed.

    Presentation reads fail soft here; **destruction fails closed**. Terminating a process group
    or moving a transcript on the strength of "we could not check" is the one outcome the
    reservation exists to prevent, and an earlier version of this guard had it backwards.
    """

    def __init__(self) -> None:
        super().__init__("could not verify session ownership; not archiving. Try again", status=503)


#: How long a reservation may be held **without proof of life** before a later caller may take
#: it. This is not a duration cap on the work — a holder that is still running renews (below), so
#: reaching this age means the holder stopped beating, which is the only thing an expiry can
#: honestly detect. A fixed cap with no renewal was the bug: an unusually slow but perfectly
#: healthy `prov.archive` handed the same session to a second worker while the first was inside it.
RESERVATION_MAX_AGE_S = 300

#: How often a holder proves it is alive. Strictly shorter than the expiry, by enough that several
#: beats can be lost to load before a healthy holder is declared dead.
RESERVATION_RENEW_S = 60.0

#: How soon a *failed* beat tries again. A failure must not cost a full interval: the expiry is a
#: fixed budget, and `renew_session` can spend the connection's whole `busy_timeout` before
#: raising, so retrying on the ordinary cadence lets a handful of consecutive failures run a LIVE
#: holder past its own expiry — while it is still inside the destructive effect. Same shape as the
#: scrub loop's budget: the pacing that matters is the one that runs under the condition the
#: retry exists for, not the one on the happy path.
RESERVATION_RETRY_S = 5.0


def beat_wait(renewed: bool, interval: float) -> float:
    """How long a heartbeat waits before its next attempt. The retry policy, as a pure function.

    Extracted so it can be asserted **directly** rather than inferred from elapsed wall-clock. A
    timing-based test of this is only as reliable as the machine running it: the policies differ by
    a fraction of a second, and a loaded CI runner erases that difference — which is precisely the
    "passes on a quiet machine" failure this module's tests warn against elsewhere.
    """
    return interval if renewed else min(interval, RESERVATION_RETRY_S)


#: What a heartbeat learned. ``released`` and ``superseded`` both stop the beat, and collapsing
#: them into one boolean is what made a successful teardown log as a hostile takeover: settlement
#: happens INSIDE the claim, so the very next beat finds its own reservation gone.
RENEWED, RELEASED, SUPERSEDED = "renewed", "released", "superseded"


def renew_session(
    session_key: str,
    token: str,
    *,
    now: float | None = None,
    path: Path | None = None,
) -> str:
    """Prove the holder of ``token`` is still alive. One of :data:`RENEWED` / :data:`RELEASED` /
    :data:`SUPERSEDED`.

    Renews the reservation **and** the mission-side lease in one transaction, because they are one
    claim: letting the lease age out while the reservation stayed fresh would leave a session no
    worker could re-claim and no worker was working.

    Gated on the token, not the key — a holder that has already been reclaimed must not be able to
    push the expiry of the claim that replaced it.

    **Three answers, not two.** "Did not renew" has two causes that call for opposite reactions:
    the claim was *released* (normally, by this worker's own settlement, which runs while the beat
    is still armed) or it was *superseded* (somebody reclaimed it — a real anomaly). A single
    ``False`` made the ordinary success path indistinguishable from a takeover, which would have
    put a false warning in the log on every completed teardown.
    """
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            n = con.execute(
                "UPDATE session_reservations SET at=? WHERE session_key=? AND token=?",
                (ts, session_key, token),
            ).rowcount
            if not n:
                # Distinguish HERE, inside the same transaction, so the answer cannot be
                # invalidated by whatever happens between a failed update and a second look.
                held = con.execute(
                    "SELECT 1 FROM session_reservations WHERE session_key=?", (session_key,)
                ).fetchone()
                con.execute("COMMIT")
                return SUPERSEDED if held is not None else RELEASED
            con.execute(
                "UPDATE mission_sessions SET lease_at=? WHERE session_key=? AND lease_token=?",
                (ts, session_key, token),
            )
            con.execute("COMMIT")
            return RENEWED
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


@contextlib.asynccontextmanager
async def holding(
    session_key: str,
    token: str | None,
    *,
    interval: float | None = None,
    path: Path | None = None,
) -> AsyncIterator[None]:
    """Keep a claim alive for as long as the external effect inside actually runs.

    Every destructive path here spans calls this process does not control — ``cleanup_runtime``
    waiting on a process group, ``prov.archive`` moving a file on a filesystem that may be under
    load. Without this, the expiry is a bet that those finish inside a fixed window, and losing the
    bet means two workers inside the same irreversible operation.

    **The beat runs on its own thread, not as an asyncio task, and that is the whole point.** The
    protected effect is *synchronous* — `prov.archive` is a file move plus a sidecar write, called
    inline. A task-based heartbeat shares the event-loop thread with it, so the moment the effect
    starts the beat stops being scheduled: exactly when the claim most needs defending, it is least
    able to defend it, and a rival can reclaim the reservation mid-move. A thread beats whether or
    not the loop is blocked, and it keeps that guarantee for call sites written later, which an
    "always dispatch the effect off-loop" rule would depend on every future caller remembering.

    The beat also calls the store **directly** rather than through :func:`run_admitted`. Admission
    exists to refuse work under load; a heartbeat is the one caller that must not be refused, since
    being refused is indistinguishable in its consequences from having died. It is one small write
    per active claim per interval, so it cannot be the thing that overwhelms the pool.

    A beat that fails transiently (store busy) is retried on the next tick. :data:`SUPERSEDED` is
    an anomaly and is logged; :data:`RELEASED` is the ordinary end of the work — settlement runs
    inside the claim — and is silent.
    """
    if not token:
        yield
        return
    # READ AT CALL TIME, not bound at import. A default evaluated in the signature freezes the
    # module constant into the function object, so the cadence cannot be changed — including by a
    # test that needs to prove the beat happens at all without waiting a minute for it (#896
    # review 20, finding 1). Every other knob in this file is read live; this one only looked
    # like it was.
    beat_every = RESERVATION_RENEW_S if interval is None else interval
    stop = threading.Event()

    def _beat() -> None:
        wait = beat_every
        while not stop.wait(wait):
            try:
                verdict = renew_session(session_key, token, path=path)
            except Exception as exc:  # transient — retry SOON, not on the ordinary cadence
                log.debug("mission: heartbeat on %s deferred (%s)", session_key, type(exc).__name__)
                wait = beat_wait(False, beat_every)
                continue
            wait = beat_wait(True, beat_every)
            if verdict == SUPERSEDED:
                log.warning("mission: claim on %s was reclaimed while still working", session_key)
                return
            if verdict == RELEASED:
                return

    beat = threading.Thread(target=_beat, name="mission-heartbeat", daemon=True)
    beat.start()
    try:
        yield
    finally:
        stop.set()
        # Bounded: a wedged beat must not hold the request open. It is a daemon thread, so a
        # thread that outlives this join cannot keep the interpreter alive either.
        beat.join(timeout=5.0)


def reserve_session(
    session_key: str,
    holder: str,
    *,
    now: float | None = None,
    path: Path | None = None,
) -> str:
    """Take the exclusive right to mutate this session's provider state. Returns a fencing token.

    **This replaces a read-only guard, and the difference is the whole point.** A guard answers a
    question about the past and the caller then acts, so a mission could adopt a session — or a
    mission teardown could begin — in the window between. The reservation *is* the act: one row,
    one transaction, and whoever loses gets a 409 naming the holder.

    Mission ownership is checked **inside the same transaction**, so "no mission is using this"
    cannot go stale between the check and the reservation either.

    The token is a **fencing token**. A reservation reclaimed after expiry mints a new one, so the
    previous holder can no longer settle the operation that replaced it — otherwise a stale worker
    finishing late would clear a fresh worker's lease and let the external effect run twice.

    Raises :class:`SessionBusy` when somebody holds it, and :class:`OwnershipUnknown` when the
    store cannot be read — never silence.
    """
    ts = time.time() if now is None else now
    cutoff = ts - RESERVATION_MAX_AGE_S
    token = uuid.uuid4().hex
    try:
        with _write_lock:
            con = _ready(path)
            try:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute(
                    "SELECT holder, at FROM session_reservations WHERE session_key=?",
                    (session_key,),
                ).fetchone()
                if row is not None and float(row["at"] or 0) >= cutoff:
                    con.execute("ROLLBACK")
                    raise SessionBusy(session_key, f"{row['holder']} is changing it")
                # Mission ownership, in the SAME transaction as the reservation.
                if not holder.startswith("mission:"):
                    owner = con.execute(
                        "SELECT mission_id, removed_at, archive_state FROM mission_sessions "
                        "WHERE session_key=? AND (removed_at IS NULL OR archive_state IS NOT NULL)"
                        " ORDER BY removed_at IS NOT NULL, added_at DESC LIMIT 1",
                        (session_key,),
                    ).fetchone()
                    if owner is not None:
                        why = None
                        if owner["archive_state"] in RESERVED_ARCHIVE_STATES:
                            why = (
                                f"mission {owner['mission_id']} is archiving it "
                                f"({_RESERVED_WHY.get(owner['archive_state'])})"
                            )
                        elif owner["removed_at"] is None:
                            why = f"mission {owner['mission_id']} is using it"
                        if why:
                            con.execute("ROLLBACK")
                            raise SessionBusy(session_key, why)
                con.execute(
                    "INSERT INTO session_reservations (session_key, token, holder, at) "
                    "VALUES (?,?,?,?) ON CONFLICT(session_key) DO UPDATE SET "
                    "token=excluded.token, holder=excluded.holder, at=excluded.at",
                    (session_key, token, holder, ts),
                )
                con.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    con.execute("ROLLBACK")
                raise
            finally:
                con.close()
    except MissionError:
        raise
    except Exception as e:  # noqa: BLE001 — an unprovable ownership must not authorise destruction
        raise OwnershipUnknown from e
    return token


def release_session(session_key: str, token: str, *, path: Path | None = None) -> bool:
    """Give the reservation back. Only the holder of ``token`` can — a stale worker cannot free
    a reservation that has already been reclaimed by somebody else."""
    with contextlib.suppress(Exception):
        with _write_lock:
            con = _ready(path)
            try:
                return bool(
                    con.execute(
                        "DELETE FROM session_reservations WHERE session_key=? AND token=?",
                        (session_key, token),
                    ).rowcount
                )
            finally:
                con.close()
    return False


def reservation_of(session_key: str, *, path: Path | None = None) -> dict | None:
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT * FROM session_reservations WHERE session_key=?", (session_key,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def holder_of(session_key: str, *, path: Path | None = None) -> str | None:
    """Which OPEN mission holds ``session_key``, or ``None``."""
    return _holder_of(session_key, path=path)


def detach(
    mission_id: str,
    session_key: str,
    *,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Release a session explicitly. The row stays; only ``removed_at`` is stamped."""
    validate_id(mission_id)
    key = (session_key or "").strip()
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            cur = con.execute(
                # 'detached' is the operator saying this is not part of the mission any more.
                # Archive reads this to know it must not cross that line.
                "UPDATE mission_sessions SET removed_at=?, release_reason='detached' "
                "WHERE mission_id=? AND session_key=? AND removed_at IS NULL",
                (ts, mission_id, key),
            )
            if not cur.rowcount:
                con.execute("ROLLBACK")
                raise MissionError(f"mission {mission_id} does not hold {key}", status=404)
            _append_event(
                con, mission_id, "session", at=ts, session_key=key, meta={"detached": True}
            )
            con.execute("UPDATE missions SET updated_at=? WHERE id=?", (ts, mission_id))
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return get_mission(mission_id, path=path) or {}


def active_session_keys(mission_id: str, *, path: Path | None = None) -> list[str]:
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT session_key FROM mission_sessions WHERE mission_id=? AND removed_at IS NULL "
            "ORDER BY added_at ASC",
            (mission_id,),
        ).fetchall()
        return [r["session_key"] for r in rows]
    finally:
        con.close()


def _bind_runtime_tx(con, logical_key: str, physical_key: str, ts: float) -> None:
    """Record where a session's runtime lives, inside the caller's transaction (#989).

    Idempotent by primary key, and a re-bind of the same session overwrites: the mapping describes
    the master that session currently runs under, and the newest adoption is the one that knows it.
    """
    con.execute(
        "INSERT INTO session_runtime_bindings (logical_key, physical_key, bound_at) "
        "VALUES (?,?,?) ON CONFLICT(logical_key) DO UPDATE SET "
        "physical_key=excluded.physical_key, bound_at=excluded.bound_at",
        (logical_key, physical_key, ts),
    )


def physical_key_of(session_key: str, *, path: Path | None = None) -> str | None:
    """The placeholder a late-bound session's runtime lives under, or None (#989).

    A fact about the session's RUNTIME, not about any mission: it outlives the mission releasing
    the session, and retention deleting that mission's history (#994 review 3). Raises on an
    unreadable store — the callers are fences and teardowns, and one that guesses its key is not a
    fence.
    """
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT physical_key FROM session_runtime_bindings WHERE logical_key=?",
            (session_key,),
        ).fetchone()
        return str(row["physical_key"]) if row else None
    finally:
        con.close()


def physical_bindings(*, path: Path | None = None) -> list[tuple[str, str]]:
    """Every recorded ``(logical_key, physical_key)`` mapping: what the alias repair republishes."""
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT logical_key, physical_key FROM session_runtime_bindings"
        ).fetchall()
        return [(str(r["logical_key"]), str(r["physical_key"])) for r in rows]
    finally:
        con.close()


def session_mission(session_key: str, *, path: Path | None = None) -> str | None:
    """The mission that currently holds this session, or None. ONE row, by session.

    The narrow counterpart to :func:`all_active_memberships`, and it exists because the write
    fence needs this answer immediately before byte one: a full-table read there would scale with
    the fleet inside the lock the terminal also wants (#903 review, finding 1).
    """
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT mission_id FROM mission_sessions "
            "WHERE session_key=? AND removed_at IS NULL LIMIT 1",
            (session_key,),
        ).fetchone()
        return None if row is None else str(row["mission_id"])
    finally:
        con.close()


def all_active_memberships(*, path: Path | None = None) -> dict[str, str]:
    """``session_key -> mission_id`` for every open membership. One query for the rail."""
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT session_key, mission_id FROM mission_sessions WHERE removed_at IS NULL"
        ).fetchall()
        return {r["session_key"]: r["mission_id"] for r in rows}
    finally:
        con.close()


def active_membership_rows(*, path: Path | None = None) -> dict[str, dict]:
    """``session_key -> {"id", "title", "state"}`` for every open membership, in ONE query (#948).

    The session list stamps each row with the mission that holds it and filters and facets on it,
    so it needs the title and state as well as the id. A per-row lookup would scale one list
    request with the size of the fleet; this is the joined counterpart to
    :func:`all_active_memberships` and reads the same open rows.
    """
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT ms.session_key AS session_key, m.id AS id, m.title AS title, m.state AS state "
            "FROM mission_sessions ms JOIN missions m ON m.id = ms.mission_id "
            "WHERE ms.removed_at IS NULL"
        ).fetchall()
        return {
            r["session_key"]: {"id": r["id"], "title": r["title"], "state": r["state"]}
            for r in rows
        }
    finally:
        con.close()


def sessions_barred_from_automation(*, path: Path | None = None) -> set[str]:
    """Session keys no action may be written for or delivered into, right now (#871).

    **The window this closes is an archive IN FLIGHT.** `_settle_live_actions` settles what exists
    at one instant and then releases the ledger lock; the orchestrator reasons about SESSIONS and
    not about missions, so without a fence it could append a fresh action for the same still-live
    session immediately afterwards — or an operator could approve an existing one — and write into
    a mission being torn down.

    Three scoping rules, and the first two were each wrong in an earlier revision:

    * **Membership is read regardless of `removed_at`.** Abandoning RELEASES a mission's sessions,
      so a fence that looked only at open memberships excluded precisely what it protects.
    * **…but a session another mission legitimately owns is NOT barred.** This repo explicitly
      supports "A finishes and releases a session, B adopts it, A is archived" — A marks its own
      historical row `skipped` so B's session is not torn down. Barring on A's history alone
      stopped B being proposed for and refused B's existing actions, purely because A stayed
      archived. That is a cross-mission availability failure, and the row that says so is already
      there: `archive_state='skipped'` means "this archive does not govern this session".
    * **The state is `archiving`, not `abandoned`.** A bare abandon releases its sessions and they
      are then free; barring on "ever belonged to an abandoned mission" would permanently disable
      automation for every such session. `archiving_at` set with `archived_at` null is exactly the
      teardown-in-flight window, including the indefinite stretch after a failed sweep leaves the
      mission on the recovery worklist — which is when the fence matters most.

    Returns a SET rather than a predicate so a caller takes one snapshot per critical section
    instead of one query per record.
    """
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT ms.session_key AS session_key FROM mission_sessions ms "
            "JOIN missions m ON m.id = ms.mission_id "
            "WHERE ((m.archiving_at IS NOT NULL AND m.archived_at IS NULL) "
            "       OR m.archived_at IS NOT NULL) "
            # This archive does not govern a session it deliberately skipped…
            "  AND COALESCE(ms.archive_state, '') != 'skipped' "
            # …nor one the operator explicitly DETACHED. `begin_archive` already excludes those
            # from teardown for exactly this reason: reaching a terminal state releases ownership
            # and those sessions are still the mission's to reap, but a detach is the operator
            # taking the session OUT of the mission. Both stamp `removed_at`, so only the reason
            # tells them apart — and without it, archiving A disabled automation on a session A
            # no longer had (review on #881).
            "  AND COALESCE(ms.release_reason, 'closed') != 'detached' "
            # …nor one that some OTHER mission currently holds. That mission is live and its
            # session is legitimately workable; the archiving mission's history does not reach it.
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM mission_sessions o JOIN missions om ON om.id = o.mission_id "
            "    WHERE o.session_key = ms.session_key AND o.mission_id != ms.mission_id "
            "      AND o.removed_at IS NULL "
            "      AND om.archiving_at IS NULL AND om.archived_at IS NULL"
            "  )"
        ).fetchall()
        return {r["session_key"] for r in rows}
    finally:
        con.close()


def sessions_governed_by_archive(mission_id: str, *, path: Path | None = None) -> list[str]:
    """The session keys THIS archive is responsible for — the abandon sweep's correct scope.

    Not the full historical roster: a row this archive marked `skipped`, or a session another
    live mission now holds, belongs to that other mission. Expiring its action during recovery
    would mutate a mission nobody archived (review on #881).
    """
    validate_id(mission_id)
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT ms.session_key AS session_key FROM mission_sessions ms "
            "WHERE ms.mission_id = ? "
            "  AND COALESCE(ms.archive_state, '') != 'skipped' "
            # The same detach exclusion `begin_archive` applies to teardown. Without it the sweep
            # expired an approved action belonging to a session the operator had removed from
            # this mission — archiving A mutating work that was deliberately taken out of A.
            "  AND COALESCE(ms.release_reason, 'closed') != 'detached' "
            "  AND NOT EXISTS ("
            "    SELECT 1 FROM mission_sessions o JOIN missions om ON om.id = o.mission_id "
            "    WHERE o.session_key = ms.session_key AND o.mission_id != ms.mission_id "
            "      AND o.removed_at IS NULL "
            "      AND om.archiving_at IS NULL AND om.archived_at IS NULL"
            "  ) "
            "ORDER BY ms.added_at ASC",
            (mission_id,),
        ).fetchall()
        return [r["session_key"] for r in rows]
    finally:
        con.close()


def settle_objectives_state(
    mission_id: str, state: str, *, now: float | None = None, path: Path | None = None
) -> bool:
    """Record that objective production finished. `done` | `failed` | `skipped`.

    Compare-and-set on `pending`, so two racing producers cannot both claim the outcome and a
    recovery pass cannot overwrite a result that landed while it was deciding.
    """
    validate_id(mission_id)
    if state not in ("done", "failed", "skipped"):
        raise MissionError(f"unknown objectives_state {state!r}", status=422)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            cur = con.execute(
                "UPDATE missions SET objectives_state=?, objectives_at=? "
                "WHERE id=? AND objectives_state='pending'",
                (state, ts, mission_id),
            )
            con.commit()
            return bool(cur.rowcount)
        finally:
            con.close()


def missions_awaiting_objectives(
    *,
    older_than: float = 0.0,
    limit: int = 50,
    after: tuple[float, str] | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> list[tuple[float, str]]:
    """Missions whose objective production never finished — the recovery worklist (#883).

    Returns `(objectives_at, id)` pairs so the caller can PAGE FORWARD. Returning bare ids and
    always serving the oldest `limit` rows starved everything behind a stuck page: the caller
    remembered what it had attempted, the query kept handing back the same rows, and it stopped
    with unattempted work still pending (review on #884). A cursor fixes both halves at once —
    each row is visited at most once per pass, so nothing spins AND nothing is skipped.

    `older_than` skips intents still legitimately in flight in THIS process; boot passes 0
    because a just-started process has no producers of its own. NULL `objectives_state` is
    deliberately excluded: those missions predate the producer, and treating them as pending
    would propose objectives for the entire history at once.
    """
    ts = time.time() if now is None else now
    con = _ready(path)
    try:
        sql = (
            "SELECT objectives_at, id FROM missions "
            "WHERE objectives_state='pending' AND objectives_at <= ? "
            "AND state NOT IN ('done','failed','abandoned') "
        )
        args: list[object] = [ts - max(0.0, older_than)]
        if after is not None:
            # Row-value comparison, so a shared timestamp cannot hide a row behind its neighbour.
            sql += "AND (objectives_at, id) > (?, ?) "
            args += [after[0], after[1]]
        sql += "ORDER BY objectives_at ASC, id ASC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        rows = con.execute(sql, args).fetchall()
        return [(float(r["objectives_at"]), r["id"]) for r in rows]
    finally:
        con.close()


def missions_awaiting_plan(
    *,
    older_than: float = 0.0,
    limit: int = 50,
    after: tuple[float, str] | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> list[tuple[float, str]]:
    """Missions whose planning attempt never settled — the plan recovery worklist (#967).

    The same cursor contract as :func:`missions_awaiting_objectives`, for the same reasons: pairs
    of `(plan_at, id)` so the caller pages forward, visits each row at most once per pass and
    skips nothing. A backfilled mission is `ready` or `skipped`, never `pending`, so an upgrade
    queues nothing.
    """
    ts = time.time() if now is None else now
    con = _ready(path)
    try:
        sql = (
            "SELECT plan_at, id FROM missions "
            "WHERE plan_state='pending' AND COALESCE(plan_at, 0) <= ? "
            "AND state NOT IN ('done','failed','abandoned') "
        )
        args: list[object] = [ts - max(0.0, older_than)]
        if after is not None:
            sql += "AND (COALESCE(plan_at, 0), id) > (?, ?) "
            args += [after[0], after[1]]
        sql += "ORDER BY COALESCE(plan_at, 0) ASC, id ASC LIMIT ?"
        args.append(max(1, min(int(limit), 500)))
        rows = con.execute(sql, args).fetchall()
        return [(float(r["plan_at"] or 0.0), r["id"]) for r in rows]
    finally:
        con.close()


# ---------------------------------------------------------------- supervisor (#885)


def objective_episode(
    mission_id: str, objective_key: str, *, path: Path | None = None
) -> tuple[int, bool]:
    """`(episode, stood_down)` for one objective. Episode 1 until something moves it.

    Absent means episode 1, not "no episode": an objective that has never stalled has still had
    exactly one run at it, and numbering it 0 would make the first budget look like a reset.

    `stood_down` here is the OPERATOR's silence only. The question hold is a separate fact with a
    separate reason and a separate way of ending — see :func:`objective_hold`.
    """
    episode, stood_down, _ = objective_hold(mission_id, objective_key, path=path)
    return episode, stood_down


def objective_hold(
    mission_id: str, objective_key: str, *, path: Path | None = None
) -> tuple[int, bool, int | None]:
    """`(episode, stood_down, question_seq)` — BOTH reasons an objective can be quiet (#892).

    They are different facts and they end differently: a stand-down is the operator saying "stop
    telling me" and lasts the episode; a question hold is the supervisor saying "I am waiting on
    you" and ends when the question is answered or superseded. A supervisor pass skips an
    objective for either reason; a board has to be able to say which, because "you silenced this"
    and "this is waiting on you" ask opposite things of the reader.
    """
    validate_id(mission_id)
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT episode, stood_down, question_seq FROM mission_objective_episode "
            "WHERE mission_id=? AND objective_key=?",
            (mission_id, objective_key),
        ).fetchone()
        if row is None:
            return (1, False, None)
        q = row["question_seq"]
        return (int(row["episode"]), bool(row["stood_down"]), int(q) if q is not None else None)
    finally:
        con.close()


def _bump_episode_con(
    con, mission_id: str, objective_key: str, ts: float, *, keep_stand_down: bool = False
) -> int:
    """Advance one objective's episode ON AN OPEN TRANSACTION. Returns the new number.

    Split out so an objective transition can advance the episode in the SAME transaction that
    performs the transition. The standalone `bump_episode` opens its own connection under
    `_write_lock`, so calling it from inside an op would deadlock — and, more importantly, would
    make the advance a separate commit that a crash could lose while keeping the transition.

    **`keep_stand_down` is for answering a question, and only for that (#892).** A new episode
    normally ends the operator's silence, because the silence was about the run that just ended.
    Answering a question is not that: it is the operator engaging with an objective they may also
    have separately told the supervisor to stop mentioning, and clearing that silence as a side
    effect of the answer would undo a decision they never revisited. The question hold is cleared
    by its own caller; this flag keeps the two from being the same switch.
    """
    row = con.execute(
        "SELECT episode, stood_down FROM mission_objective_episode "
        "WHERE mission_id=? AND objective_key=?",
        (mission_id, objective_key),
    ).fetchone()
    nxt = int(row["episode"]) + 1 if row else 2
    held = bool(row["stood_down"]) if (row and keep_stand_down) else False
    con.execute(
        "INSERT INTO mission_objective_episode "
        "(mission_id, objective_key, episode, stood_down, at) VALUES (?,?,?,?,?) "
        "ON CONFLICT(mission_id, objective_key) DO UPDATE SET "
        "episode=excluded.episode, stood_down=excluded.stood_down, at=excluded.at",
        (mission_id, objective_key, nxt, 1 if held else 0, ts),
    )
    return nxt


def bump_episode(
    mission_id: str, objective_key: str, *, now: float | None = None, path: Path | None = None
) -> int:
    """Start a NEW episode for this objective, clearing any stand-down. Returns the new number.

    The only legitimate trigger is the objective's own state changing. Input churn must not reset
    a budget — that is the difference between "the agent made progress" and "something unrelated
    happened", and conflating them hands an unmoving objective an unlimited supply of nudges.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            cur = _bump_episode_con(con, mission_id, objective_key, ts)
            con.execute("COMMIT")
            return cur
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


#: A sentinel for "this argument was not supplied", distinct from `None` — which is a legitimate
#: `probe_args` value and must be comparable.
_UNSET: object = object()

#: How much of a probe's own account of what it saw is kept on the objective row. Bounded because
#: `observed` is a durable column on every objective of every mission, and a forge that starts
#: returning something verbose must not be able to grow the store without limit.
OBSERVED_DETAIL_MAX = 300


def observation_supports(o: dict) -> bool:
    """Does the LATEST observation still back this objective's settlement? (#897 re-review)

    Lives here, beside the only writer of `observed`, because **two callers must agree**: the
    supervisor's board, which renders the difference between "was met" and "still holds", and
    `propose_completion`, which must not carry a mission into review on a gate that has since
    gone red. Those two answering differently is finding 1 — the board said not-done and the
    committing transaction said done, because only one of them asked this question.

    Three answers, and two of them are "no":

    * the last look SAW it hold → yes;
    * the last look saw it NOT hold (a check went red, a deploy rolled back) → no;
    * the last look could not happen, so the row is **stale** → no. "We could not check" is not
      evidence that a gate still holds, and a completion proposed on it would be exactly the
      stale-200 claim one layer up.

    An objective with no observation at all is supported: it was settled some other way — by a
    waiver, or by a probe kind that no longer exists — and second-guessing that here would quietly
    un-meet rows this function does not own.
    """
    obs = o.get("observed")
    if not isinstance(obs, dict):
        return True
    if obs.get("stale") is True:
        return False
    if "value" in obs:
        return bool(obs.get("value"))
    return True


def note_merge_sha(
    mission_id: str, sha: str, *, now: float | None = None, path: Path | None = None
) -> bool:
    """Record the mission's merge commit. **Write-once**; True iff this call set it.

    The producer #891's `change_live` contract needs: `http_revision` asks whether THIS revision
    is live, and a static playbook cannot name a SHA that does not exist when it is written. The
    only thing that knows one is an observation — `forge_merged` returns it — so the probe runner
    records it and every later revision probe has a marker to look for.

    Write-once because a merge commit does not change. A second, different value would mean this
    row is about a different merge, and quietly overwriting would repoint every revision objective
    on the mission at it without anything saying so.
    """
    validate_id(mission_id)
    sha = str(sha or "").strip()
    if not sha or len(sha) > 64 or not re.fullmatch(r"[0-9a-fA-F]+", sha):
        return False
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                "UPDATE missions SET merge_sha=?, updated_at=? "
                "WHERE id=? AND (merge_sha IS NULL OR merge_sha='')",
                (sha, ts, mission_id),
            )
            con.execute("COMMIT")
            return bool(cur.rowcount)
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def bind_probe_target(
    mission_id: str,
    objective_key: str,
    *,
    target: str,
    expect_probe: str | None = None,
    expect_args: object = _UNSET,
    path: Path | None = None,
) -> int | None:
    """Claim this objective for a probe against `target`. Returns the new generation, or `None`.

    Called immediately BEFORE the request goes out. It records, durably, what the answer that is
    about to be fetched will have been fetched *from*, and stamps a generation onto the row.
    `observe_objective` then refuses any answer whose `(target, generation)` is not the one the
    row is still bound to.

    That is the difference between this and the digest comparison it replaces (#897 re-review,
    finding 2). A pair of comparisons around the request lived in one caller's locals: it could
    not see a second runner probing the same row, did not survive a restart, and left the window
    between the second comparison and the write unguarded. A generation in the row is visible to
    everyone who writes it, and is checked by the transaction that settles.

    `None` means the objective is gone or is no longer the one being asked about — the caller
    should not issue the request at all.
    """
    validate_id(mission_id)
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT probe, probe_args FROM mission_objectives WHERE mission_id=? AND key=?",
                (mission_id, objective_key),
            ).fetchone()
            if row is None:
                con.execute("ROLLBACK")
                return None
            if expect_probe is not None and str(row["probe"] or "") != expect_probe:
                con.execute("ROLLBACK")
                return None
            if expect_args is not _UNSET and _loads(row["probe_args"]) != expect_args:
                con.execute("ROLLBACK")
                return None
            # ALLOCATED FROM THE MISSION, not from this row (#897 re-review, finding 2). A
            # per-row counter restarts at zero when the objective is dropped and re-added, so an
            # answer issued for the old incarnation matched the new one on every check. The
            # mission's counter only ever goes up, so a generation is never reused — and a
            # re-added row starts at the column default, which no outstanding answer can match.
            mrow = con.execute(
                "SELECT probe_gen_seq FROM missions WHERE id=?", (mission_id,)
            ).fetchone()
            if mrow is None:
                con.execute("ROLLBACK")
                return None
            gen = int(mrow["probe_gen_seq"] or 0) + 1
            con.execute("UPDATE missions SET probe_gen_seq=? WHERE id=?", (gen, mission_id))
            # …AND THE CONFIG REVISION THIS PROBE IS ABOUT. Read on THIS connection inside the
            # binding transaction, so the value stamped on the row is the one in force at the
            # moment the request is issued.
            rrow = con.execute(
                "SELECT value FROM supervisor_state WHERE key=?", (FORGE_REV_KEY,)
            ).fetchone()
            try:
                rev = int((rrow["value"] if rrow else "0") or 0)
            except (TypeError, ValueError):
                rev = 0
            con.execute(
                "UPDATE mission_objectives SET probe_target=?, probe_gen=?, probe_rev=? "
                "WHERE mission_id=? AND key=?",
                (target, gen, rev, mission_id, objective_key),
            )
            con.execute("COMMIT")
            return gen
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def observe_objective(
    mission_id: str,
    objective_key: str,
    *,
    observed: bool,
    value: bool,
    detail: str = "",
    extra: dict | None = None,
    expect_probe: str | None = None,
    expect_args: object = _UNSET,
    expect_target: str | None = None,
    expect_gen: int | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> dict | None:
    """Record what a PROBE saw. **The only path that may write `met` from evidence (#891).**

    `patch_objectives` — the operator's path — refuses `state` / `met_at` / `observed` outright, so
    an edit can never retroactively claim an objective holds. This is the other side of that rule:
    a settlement written here is always backed by a fetch that actually happened, and the row keeps
    the fact that settled it.

    **Three outcomes, and the third is not the second.**

    * ``observed=True, value=True``  — settle ``met``, stamp ``met_at``, advance the episode.
    * ``observed=True, value=False`` — record what was seen and leave the objective UNMET. The
      supervisor's nudge budget is what acts on this; the probe does not.
    * ``observed=False``            — **we could not look.** Nothing settles, ``met_at`` is not
      touched, and the row is marked ``stale`` with the reason and the time. This is the case the
      whole three-way split exists for: collapsing it into "false" makes a forge outage read as a
      mission going backwards, and collapsing it into "true" is the stale-200 lie.

    An **already-settled** objective is never re-opened by a probe. A `met` that later reads false
    (a PR reopened, a check re-run red) is a fact the operator needs, but silently un-meeting a
    gate would let the supervisor resume nudging a mission it had already proposed for completion —
    so the observation is recorded and the state is left alone. Moving a settled objective
    backwards is an operator decision, through the edit path.

    **`expect_probe` / `expect_args` are the objective's IDENTITY, compared inside this
    transaction.** A probe is an external call that can outlive the row it was issued for: drop and
    re-add the same key pointing at a different target while a request is in flight, and the old
    answer would settle the new objective. `(mission_id, key)` is not identity — it is a slot, and
    the same slot can hold a different question a second later. Refused rather than applied, and
    reported as ``None`` like any other row that is not there to write (#897 review).

    **A `could not look` never destroys the last successful observation.** The prior value is
    carried forward under ``last``, so the console's "last seen … · stale" has something to name
    and the operator can tell "it was green an hour ago and the forge is down" from "we have never
    seen this". Blanking it was the first version's bug.

    Returns the updated row, or ``None`` if the objective does not exist or has moved on.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    obs: dict = {
        "at": ts,
        "detail": _cap(str(detail or ""), OBSERVED_DETAIL_MAX),
    }
    if not observed:
        # `stale` is the flag `MissionObjectives` already reads to render "last seen … · stale"
        # with its reason — the console's degraded rendering shipped in #878 against no producer,
        # and this is the producer.
        obs["stale"] = True
        obs["reason"] = obs["detail"]
    else:
        obs["value"] = bool(value)
    # THE EXTERNAL TARGET is ADJUDICATED here now, against the binding the row carries.
    #
    # Where a probe went is not row state — it is the configured forge plus the mission's checkout
    # plus what those resolve to — and this module still does not read any of it (it must not
    # import prefs to settle a row). What it CAN do is compare: `bind_probe_target` wrote the
    # resolved digest and a generation onto the row before the request went out, the caller passes
    # the digest it re-resolved *after* the answer came back, and the transaction below requires
    # the two to be the same row-generation and the same destination. An operator who repointed
    # the forge mid-flight, or a second runner that rebound the row, is refused by the transaction
    # that would otherwise commit (#897 re-review, finding 2).
    #
    # Set BEFORE the blob is serialised: `obs` is frozen into JSON above the transaction, so a
    # field added inside it never reaches the row (which is exactly what happened first).
    if expect_target is not None:
        obs["target"] = expect_target
    # …and WHICH ARGUMENTS it answered for (#983). The settling transaction below refuses an
    # answer whose arguments no longer match the row, and this stamp is what lets a direction prove
    # the fact it fills belongs to the arguments the objective has now.
    if expect_args is not _UNSET:
        from . import mission_directions

        obs["args_sha"] = mission_directions.probe_args_digest(expect_args)
    if isinstance(extra, dict):
        for k, v in list(extra.items())[:10]:
            if v is None:
                continue
            if isinstance(v, str | int | float | bool):
                obs[k] = _cap(v, OBSERVED_DETAIL_MAX) if isinstance(v, str) else v
    # Serialised BEFORE the transaction opens, so an over-bound observation is refused without
    # having held the write lock — and refused rather than truncated, because a truncated blob
    # reads back as `None` and would report a write that silently lost its evidence.
    blob = _json_or_none(obs, OBSERVED_MAX, field="observed")
    blob_source = None  # set inside the tx when a prior observation is carried forward
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT state, probe, probe_args, observed, probe_target, probe_gen, probe_rev "
                "FROM mission_objectives WHERE mission_id=? AND key=?",
                (mission_id, objective_key),
            ).fetchone()
            if row is None:
                con.execute("ROLLBACK")
                return None
            # THE IDENTITY CHECK, inside the transaction that writes. Outside it, this is
            # check-then-act and the row can change between the two.
            if expect_probe is not None and str(row["probe"] or "") != expect_probe:
                con.execute("ROLLBACK")
                return None
            if expect_args is not _UNSET and _loads(row["probe_args"]) != expect_args:
                con.execute("ROLLBACK")
                return None
            # THE TARGET FENCE, in the same transaction as the write. `probe_target` is what the
            # request was issued against; `expect_target` is what the caller resolved after it
            # returned. Different ⇒ the destination moved under the request. A generation that
            # has moved on ⇒ somebody rebound this row, and this answer is the older one.
            if expect_gen is not None and int(row["probe_gen"] or 0) != expect_gen:
                con.execute("ROLLBACK")
                return None
            if expect_target is not None and str(row["probe_target"] or "") != expect_target:
                con.execute("ROLLBACK")
                return None
            # THE CONFIG REVISION, READ HERE — which is the whole point (#897 re-review 5,
            # finding 1). Every other fence compares two values the caller supplied, so all of
            # them are answers about a moment BEFORE this transaction: an operator who repoints
            # the forge between the caller's last resolution and this write leaves every one of
            # them agreeing while the answer came from an authority nobody is configured for any
            # more. A counter this transaction reads for itself has no such window.
            rrow = con.execute(
                "SELECT value FROM supervisor_state WHERE key=?", (FORGE_REV_KEY,)
            ).fetchone()
            try:
                now_rev = int((rrow["value"] if rrow else "0") or 0)
            except (TypeError, ValueError):
                now_rev = 0
            if expect_target is not None and int(row["probe_rev"] or 0) != now_rev:
                con.execute("ROLLBACK")
                return None
            # THE MISSION HAS TO STILL BE ONE A PROBE MAY WRITE TO (#897 re-review 5, finding 3).
            # A probe issued while the mission was running can resolve after the operator has
            # failed or abandoned it, and marking an objective `met` on a finished record edits
            # history — the settlement is about a mission that no longer exists in that form.
            mrow = con.execute(
                "SELECT state, archived_at FROM missions WHERE id=?", (mission_id,)
            ).fetchone()
            if mrow is None:
                con.execute("ROLLBACK")
                return None
            if str(mrow["state"] or "") in TERMINAL_STATES or mrow["archived_at"] is not None:
                con.execute("ROLLBACK")
                return None
            # CARRY THE LAST SUCCESSFUL OBSERVATION FORWARD. `unknown` says nothing new; it must
            # not erase what was known.
            if not observed:
                prior = _loads(row["observed"]) or {}
                keep = prior.get("last") if isinstance(prior, dict) else None
                if keep is None and isinstance(prior, dict) and "value" in prior:
                    keep = {k: v for k, v in prior.items() if k not in ("stale", "reason")}
                if keep is not None:
                    obs["last"] = keep
                    blob_source = obs
            state = str(row["state"] or "")
            settle = observed and value and state not in ("met", "waived")
            if settle:
                con.execute(
                    "UPDATE mission_objectives SET state='met', met_at=?, observed=? "
                    "WHERE mission_id=? AND key=?",
                    (ts, blob, mission_id, objective_key),
                )
                # A settlement is a transition, so the nudge budget starts again — in the SAME
                # transaction, because an episode advance that a crash could lose while keeping
                # the settlement would leave a met objective carrying a spent budget.
                _bump_episode_con(con, mission_id, objective_key, ts)
                _append_event(
                    con,
                    mission_id,
                    "probe",
                    at=ts,
                    text=_cap(f"{objective_key}: {obs['detail']}", EVENT_TEXT_MAX) or None,
                    meta={"objective": objective_key, "settled": "met", "probe": row["probe"]},
                )
            else:
                # `met_at` is deliberately NOT in this UPDATE. An objective that was met keeps the
                # time it was met even when a later probe cannot look, which is what lets the
                # console say "last seen … · stale" rather than losing the settlement's own stamp.
                con.execute(
                    "UPDATE mission_objectives SET observed=? WHERE mission_id=? AND key=?",
                    (
                        _json_or_none(blob_source, OBSERVED_MAX, field="observed")
                        if blob_source
                        else blob,
                        mission_id,
                        objective_key,
                    ),
                )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    rows = objectives(mission_id, path=path)
    for r in rows:
        if r["key"] == objective_key:
            return r
    return None


def _question_answerable(con, mission_id: str) -> bool:
    """Is this mission one an open question may still be ANSWERED on?

    The same predicate that decides whether one may be opened, read inside the answering
    transaction (#900 review 5, finding 2). The hold alone was the only comparand, and a hold
    survives nothing but its own clearing — so an answer landing after the mission closed still
    ran its action against work that was over.
    """
    row = con.execute(
        "SELECT state, archived_at FROM missions WHERE id=?", (mission_id,)
    ).fetchone()
    if row is None:
        return False
    return str(row["state"] or "") not in UNQUESTIONABLE_STATES and row["archived_at"] is None


#: The mission states a question may NOT be opened on — spelled as the exclusion rather than the
#: allowance, so a state added to the lifecycle later is questionable by default rather than
#: silently unaskable.
#:
#: A question is a request for a decision about work that is still going on. On a TERMINAL mission
#: there is no decision left to make, and on one in `review` the operator is already being asked a
#: different question about the same mission — a second one arriving underneath it flags work that
#: is over, or double-asks. Everything else is a mission somebody could still act on.
UNQUESTIONABLE_STATES: frozenset[str] = TERMINAL_STATES | {"review"}

#: Bounds on a stored question. Small: a question nobody can read at a glance is not a question.
QUESTION_TEXT_MAX = 300
QUESTION_OPTIONS_MAX = 4


def objective_content(o) -> str:
    """A digest of what an objective MEANS, for "the question I wrote is about this" (#900 rev 6,
    finding 4).

    The incarnation says the ROW is the same one; it does not say the row still says the same
    thing. `retitle` changes neither the incarnation nor the episode nor the state, so a question
    written about "a PR is open" opened unchanged against an objective that now reads "ship the
    release notes" — and a settling answer then waived work nobody asked about.

    Covers the fields the question is generated FROM and the answer acts ON: the title, whether
    it gates, and the probe that settles it. Not `state` or `observed` — those move on their own
    while a model call runs, and refusing on them would make the feature unusable on a live
    mission; the settled-state check beside this one is what covers that direction.
    """
    get = o.get if isinstance(o, dict) else (lambda k, d=None: o[k] if k in o.keys() else d)
    blob = "\x1f".join(
        [
            str(get("title") or ""),
            "1" if get("gate") else "0",
            str(get("probe") or ""),
        ]
    )
    return hashlib.sha256(blob.encode("utf-8", "replace")).hexdigest()[:16]


def objective_incarnation(mission_id: str, objective_key: str, *, path: Path | None = None) -> str:
    """The stable identity of the objective currently in this slot, or `""`.

    Read before anything that spends real time deciding something ABOUT the objective — a model
    call, an in-flight probe — and compared when the decision lands. `(mission_id, key)` is a
    slot: drop the key and re-add it and the new row is a different question wearing the same
    name (#900 review, finding 4).
    """
    validate_id(mission_id)
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT incarnation FROM mission_objectives WHERE mission_id=? AND key=?",
            (mission_id, objective_key),
        ).fetchone()
        return str((row["incarnation"] if row else "") or "")
    finally:
        con.close()


def objective_snapshot(
    mission_id: str, objective_key: str, *, path: Path | None = None
) -> dict | None:
    """Everything a direction is rendered from, in ONE read, or `None` if the slot is empty (#983).

    Unlike the public row this keeps the probe binding (`probe_target`, `probe_gen`, `probe_rev`)
    and adds the objective's current `episode`, because those are the provenance a proposal pins
    and delivery compares. One transaction, so the observation, the binding and the episode all
    describe the same instant — a torn read here could pair a new observation with an old target.
    Internal: never returned by a route.
    """
    validate_id(mission_id)
    con = _ready(path)
    try:
        con.execute("BEGIN")
        row = con.execute(
            "SELECT key, probe, probe_args, observed, state, source, incarnation, probe_target, "
            "probe_gen, probe_rev, direction, direction_source "
            "FROM mission_objectives WHERE mission_id=? AND key=?",
            (mission_id, objective_key),
        ).fetchone()
        ep = con.execute(
            "SELECT episode FROM mission_objective_episode WHERE mission_id=? AND objective_key=?",
            (mission_id, objective_key),
        ).fetchone()
        # The mission half of the probe target (#983 review): `mission_probes.resolve_target`
        # reads only the checkout folder and the merge SHA, so delivery can resolve the CURRENT
        # target from the same snapshot the facts came from.
        mrow = con.execute(
            "SELECT cwd, merge_sha FROM missions WHERE id=?", (mission_id,)
        ).fetchone()
        con.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    if row is None or mrow is None:
        return None
    d = dict(row)
    d["mission_id"] = mission_id
    d["mission_cwd"] = mrow["cwd"]
    d["mission_merge_sha"] = mrow["merge_sha"]
    d["probe_args"] = _loads(d.get("probe_args"))
    d["observed"] = _loads(d.get("observed"))
    d["episode"] = 1 if ep is None else int(ep["episode"])
    return d


def open_question(
    mission_id: str,
    objective_key: str,
    question: str,
    options: list[dict],
    *,
    expect_incarnation: str = "",
    expect_episode: int | None = None,
    expect_content: str = "",
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Open ONE question about one objective, and stand that objective down (#892).

    Both halves in ONE transaction, because they are one fact: a question the supervisor is
    waiting on is an objective it must stop nudging about, and a crash between them would leave
    either a question nobody stopped nudging around, or an objective silenced by a question that
    does not exist.

    **The objective, not the mission.** A mission may have five objectives and be stuck on one;
    standing the whole mission down would stall follow-through on the other four.

    A second question on the same objective SUPERSEDES the first rather than stacking: two open
    questions about one thing is a state the operator cannot act on coherently, and the newer one
    is the one the supervisor actually wants answered.

    **The MISSION'S OWN LIFECYCLE is part of the comparand, not only the objective's identity**
    (#900 review 4, finding 2). Producing a question takes a model call, and a mission can be
    closed, abandoned or carried into review while it runs — so a question could open on a
    terminal mission and `needs_you` would then flag work that is over, asking the operator to
    decide something about a mission nobody can act on any more. The objective's incarnation
    cannot see that: the row is still there, unchanged, on a mission that has finished.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    text = _cap(question, QUESTION_TEXT_MAX)
    if not text:
        raise MissionError("a question needs text", status=422)
    opts = []
    for o in (options or [])[:QUESTION_OPTIONS_MAX]:
        if not isinstance(o, dict):
            continue
        label = _cap(o.get("label"), 120)
        action = str(o.get("action") or "")
        if label and action:
            # `consequence` and `settling` are the SERVER'S words about what this option does,
            # carried with the stored option so the card can never render a model-authored label
            # on its own (#900 review 2, finding 1). Stored rather than joined at read time
            # because a question is durable and its meaning must not shift under a later edit of
            # the table; capped like every other text field that reaches a timeline.
            opts.append(
                {
                    "label": label,
                    "action": action,
                    "consequence": _cap(o.get("consequence"), 200),
                    "settling": bool(o.get("settling")),
                }
            )
    if len(opts) < 2:
        raise MissionError("a question needs at least two options", status=422)

    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            # THE MISSION MUST STILL BE ONE A QUESTION MEANS ANYTHING ABOUT. Read here rather
            # than by the caller before its model call, for the reason every other comparand in
            # this file is read here: a state read before an await cannot be trusted after it.
            mrow = con.execute(
                "SELECT state, archived_at FROM missions WHERE id=?", (mission_id,)
            ).fetchone()
            if mrow is None:
                raise MissionError(f"unknown mission {mission_id}", status=404)
            mstate = str(mrow["state"] or "")
            if mstate in UNQUESTIONABLE_STATES or mrow["archived_at"] is not None:
                raise MissionError(
                    f"mission {mission_id} is {mstate}, so there is nothing to ask about",
                    status=409,
                )
            known = con.execute(
                "SELECT incarnation, state, observed, title, gate, probe FROM mission_objectives "
                "WHERE mission_id=? AND key=?",
                (mission_id, objective_key),
            ).fetchone()
            if known is None:
                raise MissionError(f"unknown objective {objective_key!r}", status=404)
            # THE OBJECTIVE MUST STILL BE UNRESOLVED (#900 review 5, finding 3). Producing a
            # question takes a model call, and a probe or an operator can settle the objective
            # inside it — the incarnation is unchanged, because it is the same row, and the
            # question then asks the operator to decide about work that is already done.
            # …AND IT MUST STILL SAY THE SAME THING (#900 review 6, finding 4). A retitle
            # changes no identity the checks above compare, so a question written about one piece
            # of work opened unchanged against an objective that now describes another.
            if expect_content and objective_content(known) != expect_content:
                raise MissionError(
                    f"objective {objective_key!r} was rewritten while the question was being "
                    "written",
                    status=409,
                )
            if str(known["state"] or "") in {"met", "waived"}:
                raise MissionError(
                    f"objective {objective_key!r} was settled while the question was being "
                    "written",
                    status=409,
                )
            # THE INCARNATION, compared inside the transaction that opens the question (#900
            # review, finding 4). Producing a question takes a model call, and the objective can
            # be dropped and re-added while it runs — "a row with this key exists" then passes
            # while the row is a different objective, and the question opens against work nobody
            # asked about. The key is a slot; this is the identity.
            if expect_incarnation and str(known["incarnation"] or "") != expect_incarnation:
                raise MissionError(
                    f"objective {objective_key!r} was replaced while the question was being "
                    "written",
                    status=409,
                )
            # Read on THIS connection, inside the transaction — `objective_episode` opens its
            # own and would read outside the fence it is being written under.
            erow = con.execute(
                "SELECT episode, stood_down FROM mission_objective_episode "
                "WHERE mission_id=? AND objective_key=?",
                (mission_id, objective_key),
            ).fetchone()
            episode = int(erow["episode"]) if erow else 1
            # …AND THE OPERATOR MUST NOT HAVE SAID "STOP TELLING ME" (#900 review 9, finding 1).
            #
            # A stand-down deliberately does NOT advance the episode — it silences the episode it
            # is in — so every comparand above stays satisfied while it commits. A question
            # written before it and landing after it therefore opened on an objective the
            # operator had just silenced, and `needs_you` then flagged the mission for exactly
            # the thing they had asked to stop hearing about.
            #
            # An invariant rather than a comparand: there is no version of this that is correct
            # while the operator's silence is in force, so nothing is compared against a captured
            # value — the row is simply asked whether it is silent.
            if erow is not None and int(erow["stood_down"] or 0):
                raise MissionError(
                    f"objective {objective_key!r} was stood down while the question was being "
                    "written",
                    status=409,
                )
            # …AND IN THE SAME EPISODE IT WAS ASKED ABOUT. An episode advances when the objective
            # is stood down or re-opened, so a question written about episode 3 landing in
            # episode 4 is a question about a situation that has already been closed out.
            if expect_episode is not None and episode != int(expect_episode):
                raise MissionError(
                    f"objective {objective_key!r} moved on while the question was being written",
                    status=409,
                )
            seq = _append_event(
                con,
                mission_id,
                "question",
                at=ts,
                text=text,
                meta={
                    "objective": objective_key,
                    "episode": episode,
                    "options": opts,
                },
            )
            # HOLD the objective while the question is open — in its OWN column, never in
            # `stood_down`. They are different facts: `stood_down` is the operator saying "stop
            # telling me", this is the supervisor saying "I am waiting on you", and they end
            # differently. Sharing one boolean would make answering a question clear a silence
            # the operator set separately, and would leave the board unable to say which applied.
            #
            # Storing the SEQ rather than a flag is what makes the lifecycle exact: a second
            # question overwrites it (superseding the first), and an answer clears it only when
            # it names the question that is actually holding.
            con.execute(
                "INSERT INTO mission_objective_episode "
                "(mission_id, objective_key, episode, stood_down, question_seq, at) "
                "VALUES (?,?,?,0,?,?) "
                "ON CONFLICT(mission_id, objective_key) DO UPDATE SET "
                "question_seq=excluded.question_seq, at=excluded.at",
                (mission_id, objective_key, episode, seq, ts),
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {
        "seq": seq,
        "mission_id": mission_id,
        "objective": objective_key,
        "episode": episode,
        "question": text,
        "options": opts,
    }


def open_question_row(mission_id: str, *, path: Path | None = None) -> dict | None:
    """The question currently awaiting an answer, or None.

    Read from the HOLD, not from the timeline. The first version compared the mission's newest
    `question` seq against its newest `answer` seq, which cannot express any of the states this
    feature actually has (#892 issue review): a question on one objective while another is
    answered, a superseded question, or a late answer to an older one. The hold is per objective
    and holds the question's own seq, so all three fall out of reading it.

    The NEWEST hold is returned when several objectives are each waiting — the console shows one
    question at a time, and the newest is the one the supervisor most recently could not proceed
    without.
    """
    validate_id(mission_id)
    con = _ready(path)
    try:
        return _open_question_row(con, mission_id)
    finally:
        con.close()


def _open_question_row(con, mission_id: str) -> dict | None:
    """The hold's question, on a caller-supplied connection — so `get_mission` can read it in the
    same transaction as the timeline and the flag it both has to agree with."""
    row = con.execute(
        "SELECT e.seq AS seq, e.text AS text, e.meta AS meta "
        "FROM mission_objective_episode ep "
        "JOIN mission_events e ON e.mission_id = ep.mission_id AND e.seq = ep.question_seq "
        "WHERE ep.mission_id=? AND ep.question_seq IS NOT NULL "
        "ORDER BY e.seq DESC LIMIT 1",
        (mission_id,),
    ).fetchone()
    if row is None:
        return None
    meta = _loads(row["meta"]) or {}
    return {
        "seq": int(row["seq"]),
        "question": row["text"],
        "objective": meta.get("objective"),
        "episode": meta.get("episode"),
        "options": meta.get("options") or [],
    }


#: What answering a question is allowed to CAUSE, and the whole of it. The strings are what the
#: route reports back as `applied`, so a branch that did nothing has to say so rather than borrow
#: the success wording (#900 review, finding 2).
#: What an answer may name, duplicated from `mission_questions.ACTIONS` on purpose: the store
#: cannot import that module (it imports this one), and a validation that runs only in the caller
#: is one a second caller will not run. Asserted equal by a test, so the two cannot drift.
ANSWER_ACTIONS: frozenset[str] = frozenset(
    {"note_answer", "waive_objective", "stand_down_objective", "close_mission"}
)


def unmet_gate_count(rows) -> int:
    """How many required objectives are NOT satisfied, observation and all (#900 rev 5, f.4).

    `state == 'met'` is the stored SETTLEMENT; `observation_supports` asks whether the latest look
    still backs it — checks go red when the head advances, a deploy is rolled back, an approval
    is dismissed. `assess()` and `propose_completion()` already read both, and counting only the
    stored state here made two answers to one question: the supervisor's board correctly said not
    done, while `close_mission` was offered and, when answered, carried the mission into review.
    A waiver is exempt — the operator said it was not required, and that does not go stale.

    One function so the three callers cannot drift, which is how they drifted in the first place.
    """
    n = 0
    for r in rows:
        if not int((r["gate"] if not isinstance(r, dict) else r.get("gate")) or 0):
            continue
        state = str((r["state"] if not isinstance(r, dict) else r.get("state")) or "")
        if state == "waived":
            continue
        if state != "met":
            n += 1
            continue
        raw = r["observed"] if not isinstance(r, dict) else r.get("observed")
        obs = _loads(raw) if isinstance(raw, str) else raw
        if not observation_supports({"observed": obs}):
            n += 1
    return n


def _apply_answer_con(
    con, mission_id: str, action: str, objective_key: str, ts: float
) -> tuple[str, bool]:
    """Run the chosen action on the answer's own connection. Returns `(what happened, did it)`.

    **The boolean is not a summary of the string** (#900 review 7, finding 9). The client has to
    tell "waived" from "not waived — the objective was already met", and it was left to do that by
    reading English: every refusal happens to begin with "not ", and a phrasing change would
    silently turn a refusal into a success on screen. A flag says it in the one place that knows.

    Called from inside `answer_question`'s transaction, which is the point: settle-then-act over
    two connections has a window where the settlement is durable and the action is not, and the
    answer cannot be retried because the hold is gone.

    A refusal here does not roll the answer back. The operator answered — that is a fact worth
    keeping, and the timeline records it — but the return value says plainly that the effect did
    not happen, because "waived" over an objective that was never waived is the failure this was
    written to end.
    """
    if action == "waive_objective" and objective_key:
        row = con.execute(
            "SELECT state FROM mission_objectives WHERE mission_id=? AND key=?",
            (mission_id, objective_key),
        ).fetchone()
        if row is None:
            return "not waived — the objective is gone", False
        if str(row["state"] or "") == "met":
            return "not waived — the objective was already met", False
        # `_op_waive` also advances the episode, which is right: waiving is a state change, and
        # the answer's own bump above was for the hold, not for this.
        _op_waive(con, mission_id, {"key": objective_key}, ts)
        return "waived", True
    if action == "stand_down_objective" and objective_key:
        # The CURRENT episode, read on this connection AFTER the answer's bump. Reading it over a
        # second connection — as the route did — is a check-then-act: the episode it read could
        # already have moved, and `stand_down` would then silently do nothing and still be
        # reported as a stand-down.
        row = con.execute(
            "SELECT episode FROM mission_objective_episode "
            "WHERE mission_id=? AND objective_key=?",
            (mission_id, objective_key),
        ).fetchone()
        if row is None:
            return "not stood down — the objective is gone", False
        con.execute(
            "UPDATE mission_objective_episode SET stood_down=1, at=? "
            "WHERE mission_id=? AND objective_key=? AND episode=?",
            (ts, mission_id, objective_key, int(row["episode"])),
        )
        return "stood down", True
    if action == "close_mission":
        # A PROPOSAL, never a close — and it takes THE SAME GATE the supervisor's own proposal
        # takes (#900 review 3, finding 2).
        #
        # Appending the event alone was a claim the lifecycle had not accepted: a mission with an
        # unmet required objective answered `close_mission` and got a completion event on its
        # timeline while its state stayed `running` and the gate stayed unmet. A proposal the
        # mission never entered `review` for is a document about a decision nobody made.
        #
        # The gate is re-read HERE, on this connection, inside the answer's own transaction —
        # `propose_completion` opens its own, and calling it would put the check and the write in
        # two transactions, which is the exact split it exists to close.
        rows = con.execute(
            "SELECT * FROM mission_objectives WHERE mission_id=? ORDER BY ord ASC",
            (mission_id,),
        ).fetchall()
        unmet = unmet_gate_count(rows)
        if not rows:
            return "not proposed — this mission has no objectives, so it is unmeasured", False
        if unmet:
            # The operator's answer is still RECORDED — they said what they think — and the
            # effect honestly did not happen.
            return f"not proposed — {unmet} required objective(s) are still unmet", False
        state_row = con.execute("SELECT state FROM missions WHERE id=?", (mission_id,)).fetchone()
        from_state = str((state_row["state"] if state_row else "") or "")
        moved = con.execute(
            "UPDATE missions SET state='review', updated_at=? WHERE id=? AND state='running'",
            (ts, mission_id),
        ).rowcount
        if moved:
            # …AND THE QUESTION HOLDS GO WITH IT (#900 review 6, finding 3). `review` is an
            # unanswerable state — see `UNQUESTIONABLE_STATES` — so a hold surviving into it
            # flags the mission for a decision every answer is then refused. Same transaction as
            # the transition, because "this mission is in review" is one fact.
            con.execute(
                "UPDATE mission_objective_episode SET question_seq=NULL "
                "WHERE mission_id=? AND question_seq IS NOT NULL",
                (mission_id,),
            )
        if not moved:
            return f"not proposed — the mission is {from_state}, not running", False
        _append_event(
            con,
            mission_id,
            "state",
            at=ts,
            meta={"from": from_state, "to": "review", "why": "you answered that it is finished"},
        )
        _append_event(
            con,
            mission_id,
            "completion",
            at=ts,
            text="You answered that there is nothing further to do. "
            "Confirm from the mission's controls to close it.",
        )
        return "proposed completion", True
    # `note_answer` is deliberately a no-op beyond the answer event already on the timeline:
    # recording the operator's words IS the action, and the next supervisor pass reads them.
    return "recorded", True


def answer_question(
    mission_id: str,
    seq: int,
    *,
    option_index: int | None = None,
    text: str = "",
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Answer the open question. Returns ``{action, objective, episode, answer}``.

    **Compare-and-set on the question's own `seq`.** The caller states WHICH question it is
    answering, and a question that has been superseded or already answered is a 409 rather than a
    second application — the same rule `set_state` follows, and for the same reason: a value read
    before an await cannot be trusted after it. Answering twice must not run the action twice.

    **The action comes from the stored OPTION, by index.** The label is display text; nothing the
    model wrote is executed. An index outside the stored list is a 422.

    Answering **advances the episode and CARRIES THE STAND-DOWN ACROSS IT**, in the same
    transaction (#900 review 7, finding 13; this docstring said "clears the stand-down" and the
    code has done the opposite since review 4). A question that unblocked the work must not
    resume against a budget the previous episode spent — that is what the bump is for — but a
    silence the operator set SEPARATELY is their decision, and answering a question is not
    withdrawing it. Two different things, and conflating them let an answer resume nudging on a
    mission the operator had told to be quiet.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            # COMPARE-AND-SET ON THE HOLD, not on "is this the newest question event".
            #
            # The hold is what makes an answer valid: a question that has been superseded or
            # already answered no longer holds anything, so this row simply is not there. That
            # covers a late answer to an older question, an answer to a question about a
            # different objective, and a second answer to the same one — three states the
            # newest-question comparison could not tell apart (#892 issue review).
            q = con.execute(
                "SELECT e.seq AS seq, e.meta AS meta, ep.objective_key AS objective_key "
                "FROM mission_objective_episode ep "
                "JOIN mission_events e "
                "  ON e.mission_id = ep.mission_id AND e.seq = ep.question_seq "
                "WHERE ep.mission_id=? AND ep.question_seq=?",
                (mission_id, int(seq)),
            ).fetchone()
            if q is None:
                raise MissionError(
                    "that question is no longer the open one — it was answered or superseded",
                    status=409,
                )
            # …AND THE MISSION MUST STILL BE ONE AN ANSWER MEANS ANYTHING ON (#900 review 5,
            # finding 2). The hold was the only comparand, and a hold survives nothing but its
            # own clearing — so an answer landing after the mission closed still ran its action
            # against work that was over, and the timeline recorded a `waived` objective on a
            # mission that had failed hours earlier.
            if not _question_answerable(con, mission_id):
                raise MissionError(
                    "this mission is closed, so there is nothing left to decide",
                    status=409,
                )

            meta = _loads(q["meta"]) or {}
            opts = meta.get("options") or []
            objective_key = str(meta.get("objective") or "")
            action = ""
            answer_text = _cap(text, QUESTION_TEXT_MAX)
            if option_index is not None:
                if (
                    isinstance(option_index, bool)
                    or not isinstance(option_index, int)
                    or not 0 <= option_index < len(opts)
                ):
                    raise MissionError("that option does not exist", status=422)
                chosen = opts[option_index]
                action = str(chosen.get("action") or "")
                answer_text = answer_text or _cap(chosen.get("label"), QUESTION_TEXT_MAX)
            elif not answer_text:
                raise MissionError("an answer needs an option or some text", status=422)

            # THE CLOSED SET, BEFORE THE MUTATION (#900 review 2, non-blocking follow-up).
            #
            # The route checks it after `answer_question` returns, which is after the hold has
            # been released and the effect has run — so a stored option naming something outside
            # the set would consume the question and then be reported as a 422, leaving the
            # operator with neither the answer nor the question. The producer is allowlisted, so
            # this is defence rather than a live hole; it belongs before the write anyway,
            # because a validation that runs after the commit is a validation of the past.
            if action and action not in ANSWER_ACTIONS:
                raise MissionError(f"unknown answer action {action!r}", status=422)

            _append_event(
                con,
                mission_id,
                "answer",
                at=ts,
                text=answer_text or None,
                meta={
                    "question_seq": int(seq),
                    "objective": objective_key,
                    "action": action or "note_answer",
                },
            )
            # THE HOLD IS RELEASED, and only this question's hold: the `question_seq=?` predicate
            # means a newer question that arrived between the read and the write keeps holding.
            # The episode advances so the objective does not resume against a budget the previous
            # one spent — carrying the operator's own stand-down across it, because answering a
            # question is not withdrawing a silence they set separately.
            hold_key = str(q["objective_key"] or "") or objective_key
            if hold_key:
                _bump_episode_con(con, mission_id, hold_key, ts, keep_stand_down=True)
                con.execute(
                    "UPDATE mission_objective_episode SET question_seq=NULL "
                    "WHERE mission_id=? AND objective_key=? AND question_seq=?",
                    (mission_id, hold_key, int(seq)),
                )
            # THE CHOICE AND WHAT IT CAUSES, IN ONE TRANSACTION (#900 review, finding 2).
            #
            # It used to settle here and let the route run the effect afterwards, over its own
            # connections. That gap is unrecoverable in both directions: a crash or a cancelled
            # request between them loses the effect for good, because the retry is now a 409 — the
            # hold is already released — and the operator has no second chance to answer. And the
            # route's waiver branch suppressed `MissionError`, so an objective that had been met
            # in the meantime returned `applied: "waived"` over an objective that was not waived.
            #
            # Every action in the closed set writes to THIS store, so there is nothing to
            # co-ordinate: they are statements on this connection, inside this transaction,
            # committed with the answer or not at all.
            applied, applied_ok = _apply_answer_con(
                con, mission_id, action or "note_answer", hold_key or objective_key, ts
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {
        "action": action or "note_answer",
        "objective": objective_key,
        "answer": answer_text,
        "applied": applied,
        # DID IT HAPPEN, as a fact rather than as a prefix on a sentence (review 7, finding 9).
        "applied_ok": applied_ok,
    }


def stand_down(
    mission_id: str,
    objective_key: str,
    *,
    episode: int,
    now: float | None = None,
    path: Path | None = None,
) -> bool:
    """ "Stop telling me", for THIS episode only. Returns False if the episode already moved on.

    It silences; it does not settle. The objective stays unmet and visibly so — a stand-down that
    marked something met would be the operator's annoyance quietly becoming a false claim about
    the work. The silence ends when the objective transitions, because that starts a new episode.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            # The objective must EXIST. Upserting on an unknown key would let an authenticated
            # malformed request pre-silence a key before it is ever added, and the row would then
            # be waiting for it — a stand-down nobody could see in the objective list.
            known = con.execute(
                "SELECT 1 FROM mission_objectives WHERE mission_id=? AND key=?",
                (mission_id, objective_key),
            ).fetchone()
            if known is None:
                con.execute("ROLLBACK")
                return False
            # …and the episode must be the CURRENT one. The previous spelling put the episode
            # guard only on the DO UPDATE branch, so the INSERT branch — a fresh objective with no
            # episode row — accepted ANY number: `episode=99` was written and became current,
            # silencing every real episode up to it. An absent row means episode 1, and nothing
            # else is acceptable.
            row = con.execute(
                "SELECT episode FROM mission_objective_episode "
                "WHERE mission_id=? AND objective_key=?",
                (mission_id, objective_key),
            ).fetchone()
            current = int(row["episode"]) if row else 1
            if int(episode) != current:
                con.execute("ROLLBACK")
                return False
            con.execute(
                "INSERT INTO mission_objective_episode "
                "(mission_id, objective_key, episode, stood_down, at) VALUES (?,?,?,1,?) "
                "ON CONFLICT(mission_id, objective_key) DO UPDATE SET "
                "stood_down=1, at=excluded.at",
                (mission_id, objective_key, current, ts),
            )
            con.execute("COMMIT")
            return True
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def record_supervisor_action(
    mission_id: str,
    *,
    session_key: str,
    objective_key: str,
    episode: int,
    action_id: str,
    max_per_episode: int | None = None,
    expect_incarnation: str | None = None,
    require_no_direction: bool = False,
    now: float | None = None,
    path: Path | None = None,
) -> bool:
    """Bind an action to the objective episode it was sent for. True if the binding was taken.

    **`expect_incarnation` and `require_no_direction` are the AI-drafted direction's eligibility,
    enforced HERE** (#983 P3, review 4887). Both are properties the caller checked earlier and then
    awaited on — a precondition capture is an external read — and neither moves anything the other
    guards watch: a re-created objective under the same key restarts at episode 1, and
    `set_direction` changes neither episode nor incarnation. Checked in the transaction that takes
    the binding, they have no window at all. A caller passing neither gets exactly the old
    behaviour.

    Written BEFORE the ledger append is delivered, so a crash leaves a binding whose action may or
    may not have landed; the ledger's terminal state for that id then says which. That ordering is
    the point: a counter incremented at send time is wrong on one side of the crash or the other,
    and this is wrong on neither.

    **With `max_per_episode`, this is also the RESERVATION**, and that is what closes the overspend
    race (#888 review, finding 3). Reading the budget and then appending is a check-then-act: two
    overlapping passes both read "one left" and both send, and a 3-nudge budget delivers four. The
    count and the insert happen here inside one `BEGIN IMMEDIATE`, so the second caller loses and
    is told, rather than discovering it from a ledger that has already been written.

    Counting BINDINGS rather than re-deriving spend is deliberate: the derived figure needs the
    ledger, which is a different store and cannot join this transaction. Bindings are the right
    thing to cap anyway — they are exactly the actions this supervisor minted for the episode, and
    a definite refusal is removed again by `forget_supervisor_action`, so the count self-heals.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            if not _objective_is_current(con, mission_id, objective_key, episode):
                con.execute("ROLLBACK")
                return False
            if expect_incarnation is not None or require_no_direction:
                row = con.execute(
                    "SELECT incarnation, direction FROM mission_objectives "
                    "WHERE mission_id=? AND key=?",
                    (mission_id, objective_key),
                ).fetchone()
                if row is None:
                    con.execute("ROLLBACK")
                    return False
                # THE INCARNATION THE CALLER WAS WRITING ABOUT. An episode number is not an
                # identity: a drop and a re-add of the same key starts at 1 again.
                if (
                    expect_incarnation is not None
                    and str(row["incarnation"] or "") != expect_incarnation
                ):
                    con.execute("ROLLBACK")
                    return False
                # …and an objective the operator has since given a direction gets the operator's
                # words, so there is nothing for a draft to be.
                if require_no_direction and str(row["direction"] or "").strip():
                    con.execute("ROLLBACK")
                    return False
            if max_per_episode is not None:
                n = con.execute(
                    "SELECT COUNT(*) AS n FROM mission_supervisor_actions "
                    "WHERE mission_id=? AND objective_key=? AND episode=?",
                    (mission_id, objective_key, episode),
                ).fetchone()["n"]
                if int(n) >= int(max_per_episode):
                    con.execute("ROLLBACK")
                    return False
            con.execute(
                "INSERT OR IGNORE INTO mission_supervisor_actions "
                "(mission_id, session_key, objective_key, episode, action_id, at) "
                "VALUES (?,?,?,?,?,?)",
                (mission_id, session_key, objective_key, episode, action_id, ts),
            )
            con.execute("COMMIT")
            return True
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def forget_supervisor_action(mission_id: str, action_id: str, *, path: Path | None = None) -> bool:
    """Drop one supervisor→action binding. True if a row went away.

    For the KNOWN-drop case only: the ledger definitively refused the slot, so nothing was
    appended and there is nothing to account for. Leaving the binding would make the next budget
    read see an id with no ledger row — which is deliberately treated as `indeterminate`, i.e.
    charged and terminal — and permanently stop automatic attempts over a write the code KNOWS
    never happened.

    This is not a weakening of the fail-closed rule, and the distinction is the whole point: a
    crash or an exception leaves the binding exactly where it was, because then nobody can say
    whether the append landed. Only a definite refusal is forgotten.
    """
    validate_id(mission_id)
    with _write_lock:
        con = _ready(path)
        try:
            cur = con.execute(
                "DELETE FROM mission_supervisor_actions WHERE mission_id=? AND action_id=?",
                (mission_id, action_id),
            )
            con.commit()
            return bool(cur.rowcount)
        finally:
            con.close()


def reserve_ai_direction(
    mission_id: str,
    *,
    objective_key: str,
    episode: int,
    action_id: str,
    expect_incarnation: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> bool:
    """Take this episode's ONE autonomous AI-written direction. True if `action_id` holds it.

    **This is the bound, not a report of it** (#983 P4 review). It is taken BEFORE any byte, in
    one transaction, and the primary key decides the winner — so two overlapping supervisor calls
    on different sessions of the same objective episode cannot both proceed. The previous shape
    counted completed sends, which cannot work: between the two reads that would have to disagree,
    both actions are merely `claimed`, and a claimed action is in flight rather than spent.

    **Never released.** A caller that reserved and then failed, was refused at the fence, or crashed
    leaves the allowance spent for this episode. That is deliberate: losing one possible autonomous
    send costs the operator a nudge they can still make by tapping, while releasing it on an
    outcome nobody can account for risks a second unreviewed write into a live agent.

    The objective must still be current and, when `expect_incarnation` is given, still be the same
    incarnation — checked in this transaction, so a drop-and-re-add cannot slip between.
    """
    validate_id(mission_id)
    # NO IDENTITY, NO BOUND. The incarnation is what distinguishes this objective from a later one
    # reusing its key, so a caller that cannot name it does not get to reserve against it.
    if not expect_incarnation:
        return False
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            if not _objective_is_current(con, mission_id, objective_key, episode):
                con.execute("ROLLBACK")
                return False
            row = con.execute(
                "SELECT incarnation FROM mission_objectives WHERE mission_id=? AND key=?",
                (mission_id, objective_key),
            ).fetchone()
            if row is None or str(row["incarnation"] or "") != expect_incarnation:
                con.execute("ROLLBACK")
                return False
            cur = con.execute(
                "INSERT OR IGNORE INTO mission_ai_directions "
                "(mission_id, objective_key, incarnation, episode, action_id, at) "
                "VALUES (?,?,?,?,?,?)",
                (mission_id, objective_key, expect_incarnation, episode, action_id, ts),
            )
            took = bool(cur.rowcount)
            if not took:
                # IDEMPOTENT FOR THE SAME ACTION, exclusive across different ones. The delivery
                # path re-takes this slot at the write boundary for an action that already
                # reserved it upstream, and that must not read as "somebody else holds it".
                row = con.execute(
                    "SELECT action_id FROM mission_ai_directions "
                    "WHERE mission_id=? AND objective_key=? AND incarnation=? AND episode=?",
                    (mission_id, objective_key, expect_incarnation, episode),
                ).fetchone()
                took = row is not None and str(row["action_id"]) == action_id
            con.execute("COMMIT")
            return took
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def ai_direction_holder(
    mission_id: str, objective_key: str, episode: int, *, path: Path | None = None
) -> str | None:
    """Which action holds this episode's one autonomous AI direction, or ``None``.

    The in-fence question is "is the reservation still MINE", not "does one exist" — the caller has
    already taken it, so a bare existence check would refuse its own send.
    """
    validate_id(mission_id)
    con = _ready(path)
    try:
        # JOINED TO THE OBJECTIVE'S CURRENT INCARNATION, so a reservation left behind by an
        # objective that was dropped and re-created simply does not match — the new incarnation
        # reads as unspent without anything having to go and delete the old row first.
        row = con.execute(
            "SELECT d.action_id FROM mission_ai_directions d "
            "JOIN mission_objectives o ON o.mission_id = d.mission_id "
            "AND o.key = d.objective_key AND o.incarnation = d.incarnation "
            "WHERE d.mission_id=? AND d.objective_key=? AND d.episode=?",
            (mission_id, objective_key, episode),
        ).fetchone()
        return str(row["action_id"]) if row is not None else None
    finally:
        con.close()


def record_auto_announcement(
    action_id: str, mission_id: str, *, now: float | None = None, path: Path | None = None
) -> None:
    """Durably record that an autonomous send HAS been announced (#983 P4).

    Kept here rather than in the notification ring because that ring is evictable and dismissible:
    a receipt living there is destroyed by the operator clearing the very row it is a receipt for,
    and the next sweep announces it again as unread.
    """
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute(
                "INSERT OR IGNORE INTO mission_auto_announcements (action_id, mission_id, at) "
                "VALUES (?,?,?)",
                (action_id, mission_id, ts),
            )
            con.commit()
        finally:
            con.close()


def auto_announcement_recorded(action_id: str, *, path: Path | None = None) -> bool:
    """Has this autonomous send already been announced? One durable answer for every caller."""
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT 1 FROM mission_auto_announcements WHERE action_id=?", (action_id,)
        ).fetchone()
        return row is not None
    finally:
        con.close()


def unannounced_auto_ids(action_ids: list[str], *, path: Path | None = None) -> set[str]:
    """Which of `action_ids` still OWE an announcement. Bulk read for the compaction pin."""
    ids = [a for a in action_ids if a]
    if not ids:
        return set()
    con = _ready(path)
    try:
        have: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            rows = con.execute(
                f"SELECT action_id FROM mission_auto_announcements WHERE action_id IN ({marks})",  # noqa: S608
                chunk,
            ).fetchall()
            have.update(str(r["action_id"]) for r in rows)
        return {a for a in ids if a not in have}
    finally:
        con.close()


def supervisor_action_episode(
    mission_id: str, action_id: str, *, path: Path | None = None
) -> int | None:
    """The episode an action was BOUND to when the supervisor minted it, or None.

    The binding row has always recorded this. It is the fail-closed answer for a durable action
    whose own record predates `objective_episode`: rather than skipping the episode check for
    compatibility — which a drop-and-re-add of the same key turns into a way to deliver a proposal
    against an incarnation it was never minted for — the episode is recovered from the binding, and
    an action with neither is refused (#888 review).
    """
    validate_id(mission_id)
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT episode FROM mission_supervisor_actions WHERE mission_id=? AND action_id=?",
            (mission_id, action_id),
        ).fetchone()
        return None if row is None else int(row["episode"])
    finally:
        con.close()


def supervisor_action_ids(
    mission_id: str, objective_key: str, episode: int, *, path: Path | None = None
) -> list[str]:
    """The action ids charged to this objective EPISODE, oldest first."""
    validate_id(mission_id)
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT action_id FROM mission_supervisor_actions "
            "WHERE mission_id=? AND objective_key=? AND episode=? ORDER BY at ASC",
            (mission_id, objective_key, episode),
        ).fetchall()
        return [r["action_id"] for r in rows]
    finally:
        con.close()


def escalated_objectives(mission_id: str, *, path: Path | None = None) -> dict[str, int]:
    """`{objective_key: episode}` for every terminal escalation this mission has recorded.

    Read by the supervisor to find an ASK IT STILL OWES (#900 review 4, finding 1): an escalation
    is the durable record that the supervisor decided it could not resolve an objective alone, and
    a question that never landed beside one is an obligation, not a missed opportunity. Returning
    the episode is what lets the caller compare it against the objective's CURRENT one — an
    escalation from a previous episode says nothing about this one.
    """
    validate_id(mission_id)
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT objective_key, MAX(episode) AS episode FROM mission_escalations "
            "WHERE mission_id=? GROUP BY objective_key",
            (mission_id,),
        ).fetchall()
        return {str(r["objective_key"]): int(r["episode"] or 0) for r in rows}
    finally:
        con.close()


def escalate_once(
    mission_id: str,
    *,
    session_key: str,
    objective_key: str,
    episode: int,
    reason: str,
    meta: dict | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> bool:
    """Record the terminal escalation for this episode. True iff THIS caller won.

    `meta` adds fields to the timeline event (e.g. `held: "direction"`, #983). It cannot replace
    the objective key or the episode, which are written after it.

    The uniqueness constraint is the arbiter, not a preceding check: two overlapping passes both
    reading "not escalated yet" and both writing is exactly what a check-then-insert allows.

    Arbitration is per `(mission, objective, episode)` — `session_key` is recorded for provenance
    but is deliberately NOT part of the key. The budget being reported on is objective-level, so a
    per-session key would let one exhausted objective announce itself once per held session while
    quoting a single shared number (#888 review, finding 9).
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            if not _objective_is_current(con, mission_id, objective_key, episode):
                con.execute("ROLLBACK")
                return False
            con.execute(
                "INSERT INTO mission_escalations "
                "(mission_id, session_key, objective_key, episode, reason, at) "
                "VALUES (?,?,?,?,?,?)",
                (mission_id, session_key, objective_key, episode, _cap(reason, 500), ts),
            )
            # THE ARBITRATION ROW AND THE OPERATOR-VISIBLE ARTIFACT, TOGETHER.
            #
            # Appending afterwards and suppressing the failure is a one-way trap: the unique row is
            # already committed, so a later pass can never win the arbitration again, and the
            # timeline is permanently missing the only record the operator would ever see. The
            # uniqueness that makes "exactly once" safe is exactly what makes a partial write
            # unrepairable, so the two cannot be separate statements.
            _append_event(
                con,
                mission_id,
                "escalation",
                at=ts,
                session_key=session_key,
                text=_cap(reason, 500),
                meta={**(meta or {}), "objective_key": objective_key, "episode": episode},
            )
            con.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            return False
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def supervisor_authority_verdict(
    mission_id: str, state: tuple, *, episode: int | None = None
) -> tuple[bool, str]:
    """Interpret a `supervisor_authority` tuple. `(ok, why_not)`.

    Lives here, beside the read that produces it, because BOTH the supervisor and the actuator
    need the same answer and neither may import the other. A second copy of this reasoning is a
    second thing to keep in step with the tuple's shape.

    Each refusal is its own sentence: the operator acts differently on "the session moved" than on
    "you stood this down".
    """
    holder, obj_state, current, stood_down = state[:4]
    if holder != mission_id:
        return False, (
            "the session left this mission while this was being prepared"
            if holder is None
            else f"the session was adopted by mission {holder} while this was being prepared"
        )
    if obj_state == OBJECTIVE_GONE:
        return False, "the objective was dropped while this was being prepared"
    if obj_state in ("met", "waived"):
        return False, f"the objective was {obj_state} while this was being prepared"
    if stood_down:
        return False, "the operator stood this objective down while this was being prepared"
    # THE QUESTION HOLD IS AUTHORITY, not display. It rides in the tuple — and therefore in the
    # fingerprint the write fence re-reads immediately before byte one — because the window
    # between the decision and the write is exactly where the ask lands: the supervisor escalates,
    # opens a question, and a pass already in flight would otherwise type into a session the
    # console is showing a question about (#900 review, finding 1).
    if len(state) > 5 and int(state[5] or 0):
        return False, "a question about this objective is waiting for you"
    if episode is not None and int(current) != int(episode):
        return False, "the objective started a new episode while this was being prepared"
    return True, ""


def supervisor_action_verdict(mission_id: str, state: tuple) -> tuple[bool, str]:
    """The verdict for a supervisor ACTION, from one atomic `supervisor_authority` snapshot.

    Requires a live binding. An episode number is not an identity — dropping an objective and
    re-adding the same key starts at episode 1 again, so an action minted for the first
    incarnation compares equal to the second. The binding is what distinguishes them, because the
    drop deletes it and the re-add does not bring it back.
    """
    bound = state[4] if len(state) > 4 else None
    if bound is None:
        return False, (
            "this supervisor action has no live objective binding, so the objective incarnation "
            "it was minted for no longer exists"
        )
    return supervisor_authority_verdict(mission_id, state, episode=bound)


def supervisor_authority(
    mission_id: str,
    objective_key: str,
    *,
    session_key: str,
    action_id: str = "",
    path: Path | None = None,
) -> tuple:
    """`(holder, objective_state, episode, stood_down, bound_episode, question_seq, incarnation)`.

    One read. `incarnation` is the objective's current identity, or `None` when the slot is empty
    (#983 P3): an action bound to an incarnation compares it inside the same snapshot.

    `bound_episode` is the episode the supervisor BINDING records for `action_id`, or `None` when
    no binding exists. It is part of this tuple — rather than a separate lookup the caller does
    first — for two reasons, and both were defects:

    * **Atomicity.** Reading the binding and then reading the objective is an ABA window: a
      drop-and-re-add between the two deletes the binding and recreates an objective that looks
      identical, and the caller sees a valid binding beside a valid objective that never belonged
      together.
    * **It has to be in the FINGERPRINT.** The write fence compares this tuple immediately before
      byte one. A drop-and-re-add after the guard restores `(holder, pending, 1, False)` exactly,
      so without the binding the comparison cannot see that the incarnation was withdrawn — and
      the binding is the one part that does not come back, because the drop deletes it and the
      re-add does not recreate it.

    Three independent reads are not a fingerprint. Assembled separately, a detach landing between
    the holder read and the objective read produced a tuple byte-for-byte equal to the pre-detach
    one — so the write fence compared equal and proceeded to `os.write()` on authority that had
    already been withdrawn (#888 review, finding 1). A torn read is worse than a stale one: stale
    is detected by the comparison, torn is invisible to it.

    `BEGIN` gives SQLite's snapshot for the whole tuple, so every field describes the same instant.
    """
    validate_id(mission_id)
    con = _ready(path)
    try:
        con.execute("BEGIN")
        holder = con.execute(
            "SELECT mission_id FROM mission_sessions "
            "WHERE session_key=? AND removed_at IS NULL LIMIT 1",
            (session_key,),
        ).fetchone()
        obj = con.execute(
            "SELECT state, incarnation FROM mission_objectives WHERE mission_id=? AND key=?",
            (mission_id, objective_key),
        ).fetchone()
        ep = con.execute(
            "SELECT episode, stood_down, question_seq FROM mission_objective_episode "
            "WHERE mission_id=? AND objective_key=?",
            (mission_id, objective_key),
        ).fetchone()
        bound = None
        if action_id:
            b = con.execute(
                "SELECT episode FROM mission_supervisor_actions "
                "WHERE mission_id=? AND action_id=?",
                (mission_id, action_id),
            ).fetchone()
            bound = None if b is None else int(b["episode"])
        con.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    return (
        None if holder is None else str(holder["mission_id"]),
        OBJECTIVE_GONE if obj is None else str(obj["state"] or ""),
        1 if ep is None else int(ep["episode"]),
        bool(ep["stood_down"]) if ep is not None else False,
        bound,
        0 if ep is None else int(ep["question_seq"] or 0),
        None if obj is None else (str(obj["incarnation"] or "") or None),
    )


def note_growth(
    mission_id: str,
    *,
    session_key: str,
    mark: int,
    now: float | None = None,
    path: Path | None = None,
) -> None:
    """Record this session's growth mark and when it was observed.

    Only called when the mark has actually MOVED, so `growth_at` is the last time this session was
    seen to make progress — which is the clock a stall is measured against.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute(
                "INSERT INTO mission_supervisor "
                "(mission_id, session_key, growth_mark, growth_at, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(mission_id, session_key) DO UPDATE SET "
                "growth_mark=excluded.growth_mark, growth_at=excluded.growth_at, "
                "updated_at=excluded.updated_at",
                (mission_id, session_key, int(mark), ts, ts),
            )
            con.commit()
        finally:
            con.close()


#: The relay-record states a later writer may still change. See `settle_relay_event`.
RELAY_OPEN_STATES: frozenset[str] = frozenset({"sending", "indeterminate"})


def settle_relay_event(
    mission_id: str,
    *,
    action_id: str,
    state: str,
    detail: str = "",
    now: float | None = None,
    path: Path | None = None,
) -> bool:
    """Stamp the outcome onto the operator's own record for `action_id`. True if a row moved.

    The record is written BEFORE the bytes and settled here afterwards, which is the only
    ordering that cannot lose the operator's words (#903 review, finding 2). Written after, a
    store failure means the relay was delivered and the transcript never says so; written first
    and left unsettled, the worst case is a record that says `sending`, beside a ledger row under
    the same `action_id` that says what happened. One is a silent loss, the other is a
    reconcilable one.

    **A DEFINITE OUTCOME IS NEVER OVERWRITTEN** (#903 review 3, finding 2). Only `sending` and
    `indeterminate` may still move: the first is unfinished, the second is a statement that
    nobody could tell yet, which a later terminal row is allowed to resolve. Everything else is
    somebody's answer about what happened to the operator's words, and a second writer with a
    different opinion — a read-time reconcile working from a compacted ledger, say — must not get
    to replace it. The fence is HERE, at the write, rather than in each caller's head.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT seq, meta FROM mission_events "
                "WHERE mission_id=? AND action_id=? AND kind='operator_msg' "
                "ORDER BY seq DESC LIMIT 1",
                (mission_id, action_id),
            ).fetchone()
            if row is None:
                con.execute("ROLLBACK")
                return False
            meta = _loads(row["meta"]) or {}
            if not isinstance(meta, dict):
                meta = {}
            if str(meta.get("state") or "") not in RELAY_OPEN_STATES:
                con.execute("ROLLBACK")
                return False
            meta["state"] = str(state)
            if detail:
                meta["detail"] = _cap(detail, 500)
            con.execute(
                "UPDATE mission_events SET meta=?, at=? WHERE seq=?",
                (_json_or_none(meta, EVENT_META_MAX, field="event meta"), ts, int(row["seq"])),
            )
            con.execute("COMMIT")
            return True
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def ensure_held_event(
    mission_id: str,
    *,
    action_id: str,
    session_key: str,
    text: str,
    meta: dict,
    now: float | None = None,
    path: Path | None = None,
) -> bool:
    """Write the operator-visible "held" record for `action_id`, exactly once. True if it exists.

    IDEMPOTENT BY `action_id`, which is what makes the retry safe: the supervisor's binding row is
    the recoverable intent, and it is only released once this has succeeded. A transient store
    failure therefore delays the record rather than losing it, and a retry cannot produce a second
    one (#888 review, finding 5).
    """
    return ensure_action_event(
        mission_id,
        action_id=action_id,
        session_key=session_key,
        text=text,
        meta=meta,
        stage="held",
        now=now,
        path=path,
    )


#: The two records a supervisor action can leave on the thread (#983 review). They are DIFFERENT
#: events with different identities: a Suggest proposal is held when it is minted and delivered
#: when the operator approves it, and the thread must show both. An event written before stages
#: existed carries no `stage` and is a held one — the only kind there was.
ACTION_EVENT_STAGES: frozenset[str] = frozenset({"held", "delivered"})


def ensure_action_event(
    mission_id: str,
    *,
    action_id: str,
    session_key: str,
    text: str,
    meta: dict,
    stage: str = "held",
    now: float | None = None,
    path: Path | None = None,
) -> bool:
    """The thread's one `action` event for `(action_id, stage)`, written at most once.

    True if it exists afterwards, False if the mission does not. Shared by the "held" record above
    and by a DELIVERED supervisor nudge (#983), whose `text` is the snapshot that was typed:
    written once, never updated, so a later direction or template edit cannot change what the
    thread says was sent.

    **The stage is part of the identity.** Deduplicating on the action id alone let the held event
    a Suggest proposal writes at mint time suppress the delivered event its approval writes later,
    so the thread said "not delivered" beside text that had been typed (#983 review).
    """
    validate_id(mission_id)
    if stage not in ACTION_EVENT_STAGES:
        raise MissionError(f"unknown action event stage {stage!r}", status=500)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone() is None:
                con.execute("ROLLBACK")
                return False
            existing = con.execute(
                "SELECT 1 FROM mission_events WHERE mission_id=? AND action_id=? AND kind='action'"
                " AND COALESCE(json_extract(meta, '$.stage'), 'held')=?",
                (mission_id, action_id, stage),
            ).fetchone()
            if existing is None:
                _append_event(
                    con,
                    mission_id,
                    "action",
                    at=ts,
                    session_key=session_key,
                    text=text,
                    action_id=action_id,
                    meta={**meta, "stage": stage},
                )
            con.execute("COMMIT")
            return True
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def delivered_event_action_ids(action_ids: list[str], *, path: Path | None = None) -> set[str]:
    """Which of `action_ids` already have a DELIVERED `action` event, in one bulk read (#983).

    What `actuator.reconcile_delivered_nudges` asks before it writes, so a sweep over a ledger
    whose deliveries are all recorded costs a read and takes no write lock.
    """
    ids = [a for a in dict.fromkeys(action_ids) if a]
    if not ids:
        return set()
    con = _ready(path)
    try:
        out: set[str] = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            rows = con.execute(
                "SELECT DISTINCT action_id FROM mission_events WHERE kind='action' "  # noqa: S608
                f"AND action_id IN ({marks}) AND json_extract(meta, '$.stage')='delivered'",
                chunk,
            ).fetchall()
            out.update(str(r["action_id"]) for r in rows)
        return out
    finally:
        con.close()


#: The supervisor action kinds whose delivered text the thread keeps (#983): a nudge, and an
#: AI-drafted direction the operator approved (P3). Spelled here rather than imported from `prefs`
#: because this store sits below it; a test pins the second to `prefs.DRAFT_DIRECTION_VERB`.
DELIVERED_TEXT_VERBS: frozenset[str] = frozenset({"continue", "draft_direction"})


def is_delivered_supervisor_nudge(rec: object) -> bool:
    """A ledger row that is a DELIVERED supervisor action carrying the text it typed (#983)."""
    return (
        isinstance(rec, dict)
        and rec.get("state") == "delivered"
        and rec.get("verb") in DELIVERED_TEXT_VERBS
        and str(rec.get("source") or "") == "supervisor"
        and isinstance(rec.get("delivered_text"), str)
        and isinstance(rec.get("id"), str)
        and bool(rec.get("mission_id"))
    )


def ensure_delivered_nudge_event(
    rec: dict,
    *,
    text: str,
    source: object,
    digest: object,
    auto: bool = False,
    confidence: object = None,
    at: float | None = None,
    path: Path | None = None,
) -> bool:
    """The thread's DELIVERED record for supervisor action `rec`, written at most once (#983).

    ONE writer for both callers — the delivery itself (`actuator._record_delivered_nudge`) and the
    recovery from the ledger (`reconcile_delivered_records`) — so the two cannot write different
    shapes. True if it exists afterwards; False when the mission does not exist.
    """
    return ensure_action_event(
        str(rec.get("mission_id") or ""),
        action_id=str(rec.get("id") or ""),
        session_key=str(rec.get("session_id") or ""),
        text=str(text or ""),
        meta={
            "source": "supervisor",
            "objective_key": str(rec.get("objective_key") or ""),
            "episode": rec.get("objective_episode"),
            "delivered": True,
            "text_source": source,
            "digest": digest,
            # AN AUTONOMOUSLY SENT AI-WRITTEN DIRECTION (#983 P4): the two extra facts its row
            # needs — that nobody read it, and how sure the model said it was. Both come from the
            # SETTLED ledger row, so the delivery and the repair write the same row. Omitted
            # entirely rather than written false/null, so every pre-P4 event reads unchanged.
            **({"auto": True} if auto else {}),
            **(
                {"confidence": float(confidence)}
                if isinstance(confidence, int | float) and not isinstance(confidence, bool)
                else {}
            ),
        },
        stage="delivered",
        now=at,
        path=path,
    )


def reconcile_delivered_records(records, *, path: Path | None = None) -> dict:
    """Write the missing DELIVERED thread record for each delivered supervisor row in `records`.

    Returns ``{"written": int, "unrecorded": set[str]}``. `unrecorded` names the rows whose record
    could NOT be made to exist — the ones whose ledger row is still the only copy of what was
    typed. A row for a mission that no longer exists (or never had a valid id) has nothing left to
    preserve and is not unrecorded.

    **Never types, never renders.** Its only inputs are the rows it is given: `delivered_text`,
    the render's `source` and `delivered_digest`, stamped at the settlement's own time. Idempotent,
    because the write deduplicates on `(mission, action, stage='delivered')` in its transaction.
    Raises only when the bulk read itself fails, which a caller must treat as "all unrecorded".

    Callers: `actuator.reconcile_delivered_nudges` (the supervisor sweep, over the whole ledger)
    and `orchestrator_ledger.compact` (over the rows it is about to delete).
    """
    wanted = [r for r in records if is_delivered_supervisor_nudge(r)]
    out: dict = {"written": 0, "unrecorded": set()}
    if not wanted:
        return out
    have = delivered_event_action_ids([r["id"] for r in wanted], path=path)
    for r in wanted:
        if r["id"] in have:
            continue
        ts = r.get("ts")
        # THE RECORDED PROVENANCE, never the live policy (#983 P4). `sent_by` was stamped on the row
        # by the settling compare-and-set, so a repair running days later — after the operator has
        # turned the mode off — still restores the row as it was actually sent.
        is_draft = r.get("verb") == "draft_direction"
        auto = is_draft and str(r.get("sent_by") or "") == "auto"
        if is_draft:
            source: object = "ai_auto" if auto else "ai_draft"
        elif isinstance(r.get("render"), dict):
            source = (r.get("render") or {}).get("source")
        else:
            source = None
        try:
            if ensure_delivered_nudge_event(
                r,
                text=r["delivered_text"],
                source=source,
                auto=auto,
                confidence=r.get("confidence") if is_draft else None,
                digest=r.get("delivered_digest"),
                at=float(ts) if isinstance(ts, int | float) and not isinstance(ts, bool) else None,
                path=path,
            ):
                out["written"] += 1
        except MissionError as e:
            if e.status == 404:
                # A malformed id (`validate_id` raises a 404 MissionError) or a deleted mission:
                # there is no thread to preserve anything on, so pinning the row would only widen
                # retention for ever.
                continue
            out["unrecorded"].add(r["id"])
        except Exception:  # noqa: BLE001 — the row stays the only copy; the caller keeps it
            out["unrecorded"].add(r["id"])
    return out


#: The key holding the FORGE CONFIGURATION REVISION — a counter this store owns and `prefs`
#: advances whenever the forge block is written.
#:
#: It lives here rather than in `prefs.json` for one reason: the transaction that settles an
#: objective has to be able to READ it (#897 re-review 5, finding 1). A digest captured before
#: that transaction cannot close the window between the last resolution and the write — the
#: operator can repoint the forge inside it, and the old authority's answer still lands. A
#: counter in the same database the settlement writes to can be validated by the settlement
#: itself, which is the only thing that closes it.
FORGE_REV_KEY = "forge_config_revision"


def forge_revision(*, path: Path | None = None) -> int:
    """The current forge-configuration revision. 0 when it has never been written."""
    raw = get_supervisor_state(FORGE_REV_KEY, path=path)
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def bump_forge_revision(*, now: float | None = None, path: Path | None = None) -> int:
    """Advance the forge revision. Called by `prefs.set_forge` after a successful write.

    Monotonic and read-modify-write under the store's own write lock, so two concurrent config
    writes cannot both produce the same number — a repeated revision is a window in which a stale
    answer matches.
    """
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT value FROM supervisor_state WHERE key=?", (FORGE_REV_KEY,)
            ).fetchone()
            try:
                nxt = int((row["value"] if row else "0") or 0) + 1
            except (TypeError, ValueError):
                nxt = 1
            con.execute(
                "INSERT INTO supervisor_state (key, value, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at=excluded.updated_at",
                (FORGE_REV_KEY, str(nxt), ts),
            )
            con.execute("COMMIT")
            return nxt
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def get_supervisor_state(key: str, *, path: Path | None = None) -> str | None:
    """One durable supervisor-loop value, or None."""
    con = _ready(path)
    try:
        row = con.execute("SELECT value FROM supervisor_state WHERE key=?", (key,)).fetchone()
        return None if row is None else row["value"]
    finally:
        con.close()


def set_supervisor_state(
    key: str, value: str | None, *, now: float | None = None, path: Path | None = None
) -> None:
    """Write one durable supervisor-loop value. `None` clears it."""
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            if value is None:
                con.execute("DELETE FROM supervisor_state WHERE key=?", (key,))
            else:
                con.execute(
                    "INSERT INTO supervisor_state (key, value, updated_at) VALUES (?,?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                    "updated_at=excluded.updated_at",
                    (key, value, ts),
                )
            con.commit()
        finally:
            con.close()


def supervisor_worklist(
    *,
    states: tuple[str, ...],
    after: str | None = None,
    limit: int = 50,
    path: Path | None = None,
) -> list[str]:
    """Eligible mission ids in id order, starting strictly after `after`. A KEYSET cursor.

    The supervisor sweep needs to walk every eligible mission and cannot use the list route to do
    it: that route pages by offset over a newest-updated-first ordering, which shifts under its own
    writes, and it clamps `limit`. Rebuilding a capped prefix of it and rotating within that prefix
    is fair only over the prefix — missions past the cap are never supervised at all, however the
    rotation is arranged.

    Keyset on `id` instead. `id` is immutable and unique, so the walk is stable under concurrent
    updates, it resumes exactly where it stopped, and it has no ceiling: `after=None` starts the
    ring, and the cursor wrapping back to `None` is one complete revolution.
    """
    if not states:
        return []
    lim = max(1, min(LIST_LIMIT_MAX, int(limit)))
    marks = ",".join("?" for _ in states)
    con = _ready(path)
    try:
        rows = con.execute(
            f"SELECT id FROM missions WHERE state IN ({marks}) "  # noqa: S608 — placeholders only
            "AND archived_at IS NULL AND id > ? ORDER BY id ASC LIMIT ?",
            (*states, after or "", lim),
        ).fetchall()
        return [str(r["id"]) for r in rows]
    finally:
        con.close()


def propose_completion(
    mission_id: str,
    *,
    from_state: str,
    render,
    now: float | None = None,
    path: Path | None = None,
) -> bool:
    """Move `from_state` -> `review` AND post the proposal, in ONE transaction. True iff it moved.

    Three separate operations — assess the gates, flip the state, append the artifact — have two
    failure modes between them, and a probe reproduced both (#888 review, finding 5):

    * a gating objective added between the assessment and the flip still went to `review`, carrying
      a proposal that listed the OLD checklist and never mentioned the gate that reopened it;
    * an append that failed after the flip left a mission sitting in `review` with no proposal to
      review, and no later pass could repair it, because the state compare-and-set had already won
      and would never fire again.

    So the gate check moves INSIDE the transaction and is re-evaluated against the objectives as
    they are right now, and the event is appended in the same transaction as the flip. Either the
    mission is in review with its proposal, or it is untouched.

    `render(rows) -> (text, meta)` is called with the objective rows read INSIDE this transaction,
    and that is the point of taking a callable rather than finished bytes. Re-reading the gates but
    then appending the caller's pre-built snapshot re-opens the window on the artifact alone: a
    non-gating objective added, or a title changed, after the caller assessed still produced a
    proposal listing the old set — a document that claims to enumerate what the operator is being
    asked to sign off, and does not. The caller owns the WORDING; the store owns the FACTS.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            # RE-READ the objectives here. The caller's assessment is a proposal to act on, never
            # the authority to act — between it and this line an operator can add a gate.
            rows = con.execute(
                "SELECT * FROM mission_objectives WHERE mission_id=? ORDER BY ord ASC",
                (mission_id,),
            ).fetchall()
            if not rows:
                con.execute("ROLLBACK")
                return False
            parsed = [_objective_row(r) for r in rows]
            # THE SAME PREDICATE THE BOARD USES, not a second, weaker one (#897 re-review,
            # finding 1). `state == 'met'` is the stored settlement — it says a probe once saw the
            # gate hold, and nothing more. For a gate whose truth can move (a check re-run red, a
            # deploy rolled back, a PR reopened) the stored settlement is stale the moment the
            # world changes, and a probe that observes the change records it in `observed` without
            # un-meeting the row, deliberately: un-meeting is an operator decision.
            #
            # So the board reported `likely_done: false` while THIS transaction, asking only about
            # `state`, happily carried the mission into review and posted a completion proposal
            # over a gate that had gone red. Two answers to one question is the whole defect;
            # `observation_supports` is now the one place it is answered.
            #
            # A WAIVER is exempt. The operator said the objective was not required, and that
            # decision does not go stale when a probe cannot look.
            unmet = 0
            for o in parsed:
                if not o.get("gate"):
                    continue
                state = str(o.get("state") or "")
                if state == "waived":
                    continue
                if state != "met" or not observation_supports(o):
                    unmet += 1
            if unmet:
                con.execute("ROLLBACK")
                return False
            cur = con.execute(
                "UPDATE missions SET state='review', updated_at=? WHERE id=? AND state=?",
                (ts, mission_id, from_state),
            )
            if cur.rowcount:
                # …AND THE QUESTION HOLDS GO WITH IT (#900 review 6, finding 3). `review` is an
                # unanswerable state, so a hold surviving into it flags the mission for a
                # decision every answer is then refused — `needs_you` says one thing and
                # `_question_answerable` says another. Same transaction as the transition.
                con.execute(
                    "UPDATE mission_objective_episode SET question_seq=NULL "
                    "WHERE mission_id=? AND question_seq IS NOT NULL",
                    (mission_id,),
                )
            if not cur.rowcount:
                con.execute("ROLLBACK")
                return False
            _append_event(
                con,
                mission_id,
                "state",
                at=ts,
                meta={"from": from_state, "to": "review", "why": "every gate is met"},
            )
            text, meta = render(parsed)
            _append_event(con, mission_id, "completion", at=ts, text=text, meta=meta)
            con.execute("COMMIT")
            return True
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def supervisor_checkpoint(mission_id: str, *, session_key: str, path: Path | None = None) -> dict:
    """`{input_fp, recap_seq}` — what the last pass saw for THIS session, and what it wrote.

    Per session, because a mission's sessions are read separately and a shared row makes them
    fight: one overwrites the other's fingerprint, and the loser is re-read on the next sweep at
    the cost of a model call it did not need (#888 review, finding 4).
    """
    validate_id(mission_id)
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT input_fp, recap_seq, growth_mark, growth_at FROM mission_supervisor "
            "WHERE mission_id=? AND session_key=?",
            (mission_id, session_key),
        ).fetchone()
        return (
            {
                "input_fp": row["input_fp"],
                "recap_seq": row["recap_seq"],
                "growth_mark": row["growth_mark"],
                "growth_at": row["growth_at"],
            }
            if row
            else {
                "input_fp": None,
                "recap_seq": None,
                "growth_mark": None,
                "growth_at": None,
            }
        )
    finally:
        con.close()


def advance_checkpoint(
    mission_id: str,
    *,
    session_key: str,
    input_fp: str,
    recap_text: str = "",
    recap_meta: dict | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> int | None:
    """Write the recap and move the fingerprint in ONE transaction. Returns the recap's seq.

    Two writes, and both orders are broken on their own: fingerprint-then-recap loses the recap to
    a crash and never writes it again (the input now looks unchanged); recap-then-fingerprint
    writes it twice. One transaction is the only version with neither failure.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            seq = None
            if recap_text:
                seq = _append_event(
                    con, mission_id, "recap", at=ts, text=recap_text, meta=recap_meta
                )
            con.execute(
                "INSERT INTO mission_supervisor "
                "(mission_id, session_key, input_fp, recap_seq, updated_at) "
                "VALUES (?,?,?,?,?) ON CONFLICT(mission_id, session_key) DO UPDATE SET "
                "input_fp=excluded.input_fp, recap_seq=COALESCE(excluded.recap_seq, "
                "mission_supervisor.recap_seq), updated_at=excluded.updated_at",
                (mission_id, session_key, input_fp, seq, ts),
            )
            con.execute("COMMIT")
            return seq
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


# ---------------------------------------------------------------- objectives


def _binding_digest(status: str, templates: list[dict]) -> str:
    """The digest, as a PURE function of one resolution.

    Separated from the read so a caller can digest exactly the templates it is going to use.
    Computing the two with separate reads is a race of precisely the kind this fence exists to
    close: prefs changing between them yields a digest of the NEW config beside the OLD
    templates, the write-boundary check then compares new against new and passes, and the stale
    templates are written anyway.
    """
    payload = json.dumps([status, templates], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def templates_and_binding(
    mission_id: str, *, path: Path | None = None
) -> tuple[str, list[dict], str]:
    """`(status, templates, binding)` from ONE resolution — what a producer should call."""
    status, templates = templates_for_mission(mission_id, path=path)
    return status, templates, _binding_digest(status, templates)


def playbook_binding(mission_id: str, *, path: Path | None = None) -> str:
    """A digest identifying WHICH templates a mission resolves to, right now.

    **The revocation fence for a producer that spans a model call (#883 review).**
    `propose()` resolves templates, awaits the model for many seconds, and then writes. If the
    operator deletes or edits the bound playbook inside that window, instantiating from the
    snapshot persists a probe target they have just revoked — and Phase 5 would then schedule
    requests to it. "Re-read the policy at the WRITE boundary" is the same rule `deliver_auto`
    follows for the orchestrator tier, applied to the thing that authorizes a probe.

    It digests the resolved templates rather than a version counter, so ANY change that matters
    — deleting the playbook, retargeting a URL, adding or reordering an objective — changes it,
    with nothing to remember to bump. Indices are positional, so reordering MUST invalidate:
    a selection of index 2 means a different objective afterwards.
    """
    return _binding_digest(*templates_for_mission(mission_id, path=path))


def templates_for_mission(mission_id: str, *, path: Path | None = None) -> tuple[str, list[dict]]:
    """`(status, templates)` — the objective templates in play for this mission (#883).

    `status` is one of `"ok"` / `"no_default"` / `"unknown_playbook"`, and the caller records the
    last one so the operator can see WHICH id went missing rather than wondering why a mission
    got no objectives.

    **The two failure modes are deliberately different**, and collapsing them is how probes get
    armed for a mission nobody chose them for:

    * ``playbook_id`` ABSENT ⇒ the operator's configured `default_id`. Still their decision, made
      once in Settings rather than silently per mission.
    * ``playbook_id`` SET but UNKNOWN ⇒ **nothing**. The config changed under the mission, and
      substituting another playbook would instantiate gating objectives with operator-authored
      probe targets that were never chosen here. "You did not choose" and "what you chose is
      gone" are different facts.

    Reads the prefs block through the normalizer, so a hand-edited or legacy entry is already
    degraded (non-probing, non-gating) before it can be offered as a template.
    """
    from . import prefs

    row = get_mission(mission_id, path=path)
    if row is None:
        raise MissionNotFound(mission_id)
    block = prefs.get_mission_playbooks()
    by_id = {p["id"]: p for p in block["playbooks"]}
    wanted = str(row.get("playbook_id") or "")
    if wanted:
        pb = by_id.get(wanted)
        return ("ok", list(pb["objectives"])) if pb else ("unknown_playbook", [])
    pb = by_id.get(block["default_id"]) if block["default_id"] else None
    return ("ok", list(pb["objectives"])) if pb else ("no_default", [])


def _finish_objective_write(
    con,
    mission_id: str,
    *,
    by: str,
    applied: list[dict],
    added_unmet_gate: bool,
    prior_state: str,
    ts: float,
) -> bool:
    """The tail EVERY objective write shares: reopen, timeline, `updated_at`. Returns `reopened`.

    Extracted because the two writers had drifted. `instantiate_objectives` committed rows and
    stopped there, so a proposal against a mission already in `review` could add a pending gate
    while the mission stayed review-ready — an objective list saying "not done" beside a mission
    saying "ready to close" — and left `updated_at` and the timeline stale, so no consumer could
    even see it had happened (#883 review).

    Sharing the code is the fix rather than copying it: these three invariants belong to "an
    objective was written", not to one caller's route.
    """
    reopened = False
    if added_unmet_gate and prior_state == "review":
        cur = con.execute(
            "UPDATE missions SET state='running', updated_at=?, closed_at=NULL "
            "WHERE id=? AND state='review'",
            (ts, mission_id),
        )
        reopened = bool(cur.rowcount)
        if reopened:
            _append_event(
                con,
                mission_id,
                "state",
                at=ts,
                meta={
                    "from": "review",
                    "to": "running",
                    "why": "an unmet gating objective was added",
                },
            )
    _append_event(
        con,
        mission_id,
        "objective",
        at=ts,
        meta={"by": by, "ops": applied, "reopened": reopened},
    )
    con.execute("UPDATE missions SET updated_at=? WHERE id=?", (ts, mission_id))
    return reopened


@contextlib.contextmanager
def _playbook_policy_held(mission_id: str, expect_binding: str | None, *, path: Path | None = None):
    """Hold the PREFS write lock across a mission write, and verify the binding inside it.

    **Lock order is MISSIONS -> PREFS, and prefs is a leaf.** Nothing under `json_write_lock`
    opens the missions database — `prefs`' playbook validation calls only pure helpers from this
    module (`NOTE_KEY_PREFIX`, `NON_GATING_PROBES`, `validate_probe_args`) — so this edge cannot
    close a cycle with the LEDGER -> MISSIONS order #862 fixed.

    A `None` expectation takes no prefs lock at all: a caller that is not instantiating from a
    playbook snapshot has no policy to pin, and holding a global file lock for it would serialize
    unrelated writes for nothing.
    """
    if expect_binding is None:
        yield
        return
    from .atomicjson import json_write_lock
    from .prefs import _default_path as _prefs_path

    with json_write_lock(_prefs_path()):
        if playbook_binding(mission_id, path=path) != expect_binding:
            raise MissionError(
                "the mission's playbook changed while its objectives were being proposed; "
                "nothing was instantiated",
                status=409,
            )
        yield


# ---------------------------------------------------------------- the dispatch proposal (#893)

#: Bounds on a stored plan. The brief becomes a bracketed paste into a real agent, so it is capped
#: at the same order as a nudge rather than left to whatever a model produced.
PLAN_BRIEF_MAX = 4000
PLAN_REASON_MAX = 300


def put_plan(
    mission_id: str,
    *,
    project_id: str | None,
    cwd: str | None,
    engine: str | None,
    engine_reason: str = "",
    brief: str,
    expect_plan_id: str | None = None,
    generation: int | None = None,
    first_plan: bool = False,
    planner_note: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Store THE proposal for this mission, superseding any previous one. Returns the stored row.

    **The plan and its `ready` settlement are ONE write** (#967). The row, `plan_state='ready'`,
    the draft→planned transition and the timeline event commit together, so there is no moment
    where a plan exists while `plan_state` still says `pending`, and no crash can separate them.

    **`generation` says who is writing.**

    * An `int` is a PLANNER RUN for that attempt. It commits only while `plan_state` is still
      `pending` AND `plan_generation` still equals it — a compare-and-set under the same lock as
      the write. A run whose attempt was superseded (a newer Plan again, an operator's save)
      writes nothing, records a `planning` event saying so, and raises :class:`PlanSuperseded`.
    * `None` is an OPERATOR's save (an edit, or a first plan). It is authoritative: it takes a new
      generation, which is exactly what fences out any planner still running for the old one.

    **An edit records less** (#967). With `expect_plan_id` the timeline gets one `plan_edit` event
    naming the new plan id and the fields that changed — never the brief. A plan written without
    one (a planner result, a first plan) is still a full `plan` event.

    `first_plan=True` is the manual first plan: it requires, inside the transaction, that no plan
    exists and that planning is `skipped` or `failed`. `planner_note` is a `planning` event written
    in the same transaction (e.g. a model reply that named a different project than the chosen
    one, which was ignored).

    **A re-plan supersedes rather than stacks.** Two live plans for one mission is a state the
    operator cannot act on coherently, and the newer one is the one on their screen. The primary
    key does the superseding, so there is no window where both exist.

    **`plan_id` is minted here and is the identity DISPATCH compares against.** A plan is not
    "whatever the mission's current proposal is": a model call sits between this and the button,
    the project list can move under it, and the operator dispatches the plan they SAW. The id is
    what makes that assertable — the same reason an objective has an incarnation and an action has
    a binding.

    **Storing a plan MAKES the mission `planned`, in this transaction** (#904 review 1). A plan
    is not a note beside the mission; it is the thing `planned` means — and `claim_plan` only
    claims from `planned`, so a plan that left a fresh mission in `draft` was a proposal that
    could never be dispatched. Every dispatch test had to move the state by hand, which is what
    hid it: the one path an operator actually takes was the one path nothing exercised.

    **`expect_plan_id` is a compare-and-set on the proposal being replaced** (#904 review 6). An
    edit reads a plan, changes one field and writes the whole row back, so two tabs editing
    different fields of the same proposal both succeed and the later write restores its own stale
    copy of the field the first one changed. The caller states which plan it edited; a plan that
    moved underneath it is a 409, not a silent overwrite. `None` means "there was nothing to
    replace" and requires that to be true.

    Refused for a mission that has already left the planning states, because a plan for a mission
    that is running is a proposal to start something twice.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    text = _cap(brief, PLAN_BRIEF_MAX)
    if not text:
        raise MissionError("a plan needs a brief", status=422)
    plan_id = f"pln_{uuid.uuid4().hex}"
    reason = _cap(engine_reason, PLAN_REASON_MAX)
    stale: str | None = None
    out: dict = {}
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            row = con.execute(
                "SELECT state, plan_state, plan_generation FROM missions WHERE id=?",
                (mission_id,),
            ).fetchone()
            if row is None:
                raise MissionError(f"unknown mission {mission_id}", status=404)
            state = str(row["state"] or "")
            if state not in PLANNABLE_STATES:
                raise MissionError(f"a mission that is {state} cannot be planned", status=409)
            have_state = str(row["plan_state"] or "")
            have_gen = int(row["plan_generation"] or 0)
            # THE GENERATION FENCE, compared under the lock that writes. A planner run is
            # admitted only while its own attempt is still the pending one.
            if generation is not None and (have_state != "pending" or have_gen != int(generation)):
                stale = (
                    f"a plan result was discarded: planning attempt {generation} is no longer "
                    f"current (attempt {have_gen} is {have_state or 'unset'})"
                )
                _append_event(
                    con,
                    mission_id,
                    "planning",
                    at=ts,
                    text=stale,
                    meta={
                        "outcome": "discarded",
                        "discarded": "ready",
                        "generation": int(generation),
                        "current_generation": have_gen,
                        "plan_state": have_state or None,
                    },
                )
                con.execute("COMMIT")
            else:
                new_gen = int(generation) if generation is not None else have_gen + 1
                prior = con.execute(
                    "SELECT plan_id, project_id, engine, brief FROM mission_plans "
                    "WHERE mission_id=?",
                    (mission_id,),
                ).fetchone()
                if first_plan and (prior is not None or have_state not in ("skipped", "failed")):
                    raise MissionError(
                        "a first plan can only be written for a mission that has no plan and "
                        "could not be planned",
                        status=409,
                    )
                # THE PROPOSAL BEING REPLACED, compared under the same lock that replaces it.
                # Read in the route and compared here would be two moments and no fence at all.
                if expect_plan_id is not None:
                    have = str(prior["plan_id"]) if prior else None
                    if have != expect_plan_id:
                        raise MissionError(
                            "the plan changed while you were editing it; read it again",
                            status=409,
                        )
                con.execute(
                    "INSERT INTO mission_plans "
                    "(mission_id, plan_id, project_id, cwd, engine, engine_reason, brief, "
                    " created_at, generation) "
                    "VALUES (?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(mission_id) DO UPDATE SET "
                    "plan_id=excluded.plan_id, project_id=excluded.project_id, cwd=excluded.cwd, "
                    "engine=excluded.engine, engine_reason=excluded.engine_reason, "
                    "brief=excluded.brief, created_at=excluded.created_at, "
                    "generation=excluded.generation",
                    (mission_id, plan_id, project_id, cwd, engine, reason, text, ts, new_gen),
                )
                # …AND `ready`, IN THE SAME TRANSACTION. Settling in a second write is the window
                # the issue rules out: a crash between the two would leave a plan beside
                # `pending`, and recovery would have to guess which attempt produced it.
                con.execute(
                    "UPDATE missions SET plan_state='ready', plan_generation=?, plan_at=?, "
                    "plan_detail=NULL, updated_at=? WHERE id=?",
                    (new_gen, ts, ts, mission_id),
                )
                # …AND THE MISSION BECOMES `planned`, here, not in a second call the client is
                # trusted to make. `draft -> planned` is exactly "a proposal now exists", which is
                # what this statement just made true.
                if state == "draft":
                    _to_planned_in_tx(con, mission_id, ts, plan_id)
                if planner_note:
                    _append_event(
                        con,
                        mission_id,
                        "planning",
                        at=ts,
                        text=planner_note,
                        meta={
                            "outcome": "project_conflict",
                            "generation": new_gen,
                            "plan_id": plan_id,
                            "project_id": project_id,
                        },
                    )
                if expect_plan_id is not None and prior is not None:
                    changed = [
                        field
                        for field, new in (
                            ("project_id", project_id),
                            ("engine", engine),
                            ("brief", text),
                        )
                        if prior[field] != new
                    ]
                    _append_event(
                        con,
                        mission_id,
                        "plan_edit",
                        at=ts,
                        meta={"plan_id": plan_id, "changed": changed},
                    )
                else:
                    _append_event(
                        con,
                        mission_id,
                        "plan",
                        at=ts,
                        text=text,
                        meta={
                            "plan_id": plan_id,
                            "project_id": project_id,
                            "engine": engine,
                            "engine_reason": reason,
                            "generation": new_gen,
                        },
                    )
                con.execute("COMMIT")
                out = {
                    "plan_id": plan_id,
                    "mission_id": mission_id,
                    "project_id": project_id,
                    "cwd": cwd,
                    "engine": engine,
                    "engine_reason": reason,
                    "brief": text,
                    "created_at": ts,
                    "generation": new_gen,
                    "plan_state": "ready",
                    # What the mission IS now, so the card renders the state this call
                    # established rather than the one the client last read.
                    "mission_state": "planned" if state == "draft" else state,
                }
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    if stale is not None:
        raise PlanSuperseded(stale)
    return out


class PlanSuperseded(MissionError):
    """A planner run's result arrived after its attempt stopped being current (#967).

    Nothing was written except the `planning` event that says so. A 409 at the route: the operator
    (or a newer Plan again) owns the mission's plan now.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status=409)


#: The four planning states (#967). `pending` is the only non-terminal one.
PLAN_STATES: frozenset[str] = frozenset({"pending", "ready", "failed", "skipped"})
#: Bound on the stored reason for `failed` / `skipped`. It is shown on the plan card.
PLAN_DETAIL_MAX = 500


def _to_planned_in_tx(con, mission_id: str, ts: float, plan_id: str) -> None:
    """`draft -> planned` with its `state` event, inside the caller's transaction."""
    con.execute(
        "UPDATE missions SET state='planned', updated_at=? WHERE id=? AND state='draft'",
        (ts, mission_id),
    )
    _append_event(
        con,
        mission_id,
        "state",
        at=ts,
        text="draft -> planned",
        meta={"from": "draft", "to": "planned", "plan_id": plan_id},
    )


def begin_planning(mission_id: str, *, now: float | None = None, path: Path | None = None) -> int:
    """Record a NEW planning attempt (Plan again) and return its generation (#967).

    `plan_state='pending'` and `plan_generation + 1`, in one transaction. Taking a new generation
    is what retires any result still on its way for an older attempt: its settlement will no
    longer match. Refused for a mission that has left the planning states.

    The caller holds the per-mission single-flight BEFORE calling this, so a second concurrent
    request is refused without bumping — a refused request must not discard the running one.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            row = con.execute("SELECT state FROM missions WHERE id=?", (mission_id,)).fetchone()
            if row is None:
                raise MissionNotFound(mission_id)
            state = str(row["state"] or "")
            if state not in PLANNABLE_STATES:
                raise MissionError(f"a mission that is {state} cannot be planned", status=409)
            con.execute(
                "UPDATE missions SET plan_state='pending', plan_generation=plan_generation+1, "
                "plan_at=?, plan_detail=NULL, updated_at=? WHERE id=?",
                (ts, ts, mission_id),
            )
            gen = con.execute(
                "SELECT plan_generation FROM missions WHERE id=?", (mission_id,)
            ).fetchone()[0]
            con.execute("COMMIT")
            return int(gen)
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def settle_plan(
    mission_id: str,
    generation: int,
    state: str,
    *,
    detail: str = "",
    now: float | None = None,
    path: Path | None = None,
) -> bool:
    """Close ONE planning attempt as `failed` or `skipped`. Returns whether it was this call's.

    **Fenced exactly like a successful plan** (#967): the update commits only while `plan_state`
    is `pending` AND `plan_generation` equals the attempt's. A stale attempt's failure must not
    overwrite a newer attempt's `pending`, nor an operator's `ready` — so on a mismatch nothing
    changes and a `planning` event records that the outcome was discarded.

    Not behind `_fence_busy`: this closes an intent rather than mutating the mission, and an
    attempt that failed BECAUSE the mission was archived must still be able to say so.
    """
    validate_id(mission_id)
    if state not in ("failed", "skipped"):
        raise MissionError(f"unknown plan settlement {state!r}", status=422)
    ts = time.time() if now is None else now
    why = _cap(detail, PLAN_DETAIL_MAX)
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            cur = con.execute(
                "UPDATE missions SET plan_state=?, plan_at=?, plan_detail=?, updated_at=? "
                "WHERE id=? AND plan_state='pending' AND plan_generation=?",
                (state, ts, why or None, ts, mission_id, int(generation)),
            )
            settled = bool(cur.rowcount)
            row = con.execute(
                "SELECT plan_state, plan_generation FROM missions WHERE id=?", (mission_id,)
            ).fetchone()
            if row is not None:
                if settled:
                    lead = "no plan proposed" if state == "skipped" else "could not plan"
                    _append_event(
                        con,
                        mission_id,
                        "planning",
                        at=ts,
                        text=f"{lead}: {why}" if why else lead,
                        meta={"outcome": state, "generation": int(generation)},
                    )
                else:
                    _append_event(
                        con,
                        mission_id,
                        "planning",
                        at=ts,
                        text=(
                            f"a planning outcome ({state}) was discarded: attempt {generation} "
                            f"is no longer current"
                        ),
                        meta={
                            "outcome": "discarded",
                            "discarded": state,
                            "detail": why or None,
                            "generation": int(generation),
                            "current_generation": int(row["plan_generation"] or 0),
                            "plan_state": row["plan_state"],
                        },
                    )
            con.execute("COMMIT")
            return settled
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def settle_plan_from_stored(
    mission_id: str, generation: int, *, now: float | None = None, path: Path | None = None
) -> bool:
    """Recovery's no-model-call exit: settle `ready` ONLY if the stored plan IS this attempt's.

    "A plan row exists" is not the question (#967 review). A Plan again is `pending` while the
    PREVIOUS plan is still stored, because a proposal is replaced only when the model returns — so
    settling on existence would report plan A as the result of attempt B. The stored row's
    `generation` must equal the pending one, compared in the transaction that settles.

    `put_plan` writes the plan and `ready` together, so this state should be unreachable; the
    check is here so recovery stays correct if that ever stops being true.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            row = con.execute(
                "SELECT state, plan_state, plan_generation FROM missions WHERE id=?",
                (mission_id,),
            ).fetchone()
            plan = con.execute(
                "SELECT plan_id, generation FROM mission_plans WHERE mission_id=?", (mission_id,)
            ).fetchone()
            if (
                row is None
                or plan is None
                or row["plan_state"] != "pending"
                or int(row["plan_generation"] or 0) != int(generation)
                or int(plan["generation"] or 0) != int(generation)
            ):
                con.execute("COMMIT")
                return False
            con.execute(
                "UPDATE missions SET plan_state='ready', plan_at=?, plan_detail=NULL, "
                "updated_at=? WHERE id=? AND plan_state='pending' AND plan_generation=?",
                (ts, ts, mission_id, int(generation)),
            )
            if str(row["state"] or "") == "draft":
                _to_planned_in_tx(con, mission_id, ts, str(plan["plan_id"]))
            _append_event(
                con,
                mission_id,
                "planning",
                at=ts,
                text=(
                    f"the plan for attempt {generation} was already stored; settled without "
                    "another model call"
                ),
                meta={
                    "outcome": "recovered",
                    "generation": int(generation),
                    "plan_id": str(plan["plan_id"]),
                },
            )
            con.execute("COMMIT")
            return True
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def plan_intent(mission_id: str, *, path: Path | None = None) -> dict | None:
    """The planning intent and the stored plan's generation, from ONE snapshot (#967)."""
    validate_id(mission_id)
    con = _ready(path)
    try:
        con.execute("BEGIN DEFERRED")
        row = con.execute(
            "SELECT state, plan_state, plan_generation, plan_detail, archived_at, archiving_at "
            "FROM missions WHERE id=?",
            (mission_id,),
        ).fetchone()
        plan = con.execute(
            "SELECT plan_id, generation FROM mission_plans WHERE mission_id=?", (mission_id,)
        ).fetchone()
        con.execute("COMMIT")
    except BaseException:
        with contextlib.suppress(sqlite3.Error):
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()
    if row is None:
        return None
    return {
        "state": str(row["state"] or ""),
        "plan_state": row["plan_state"],
        "plan_generation": int(row["plan_generation"] or 0),
        "plan_detail": row["plan_detail"],
        "archived": row["archived_at"] is not None or row["archiving_at"] is not None,
        "stored_plan_id": str(plan["plan_id"]) if plan else None,
        "stored_generation": int(plan["generation"] or 0) if plan else None,
    }


def get_plan(mission_id: str, *, path: Path | None = None) -> dict | None:
    """The mission's current proposal, or None."""
    validate_id(mission_id)
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT plan_id, project_id, cwd, engine, engine_reason, brief, created_at, generation "
            "FROM mission_plans WHERE mission_id=?",
            (mission_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "plan_id": str(row["plan_id"]),
            "mission_id": mission_id,
            "project_id": row["project_id"],
            "cwd": row["cwd"],
            "engine": row["engine"],
            "engine_reason": str(row["engine_reason"] or ""),
            "brief": str(row["brief"] or ""),
            "created_at": float(row["created_at"] or 0),
            "generation": int(row["generation"] or 0),
        }
    finally:
        con.close()


def objectives_digest(rows) -> str:
    """A stable digest of a mission's checklist, for "the set you approved" (#904 rev 3, f.5).

    Covers the fields the operator is actually approving — the key, the title and whether it
    GATES — and deliberately not the state: an objective becoming met between the card and the
    button is progress, not a different checklist, and refusing on it would make the button
    unpressable on an active mission. Ordered by key rather than by `ord`, so a pure reorder does
    not invalidate an approval of the same set.

    **LENGTH-PREFIXED, because delimiters are forgeable** (#904 review 10, finding 3). The first
    encoding joined fields with U+001F and rows with U+001E while a TITLE may contain either, so a
    one-row checklist whose title embedded them serialized identically to a different two-row one
    — and the second DISPATCH tap could then pass the server's compare-and-set for a checklist the
    first tap never showed. A separator can always be spelled by the data it separates; a length
    cannot, so the encoding is unambiguous by construction rather than by what titles happen to
    contain. (Control characters are also refused at the write boundary now — belt and braces, not
    the guarantee.)

    Lengths are **UTF-8 BYTE counts**, which is the one measure Python and JavaScript agree on:
    `len(str)` is code points here and UTF-16 code units there, and they differ for anything
    outside the BMP. The client computes the identical string, and one shared fixture
    (`tests/fixtures/objectives_digest_cases.json`) drives both so they cannot drift.

    Sorted by KEY, not by the encoded row: keys are `[a-z0-9_-]` slugs, which order identically
    under Python's code-point sort and JavaScript's UTF-16 one. Sorting encoded rows would put a
    title's astral characters into the comparison and the two languages would disagree.
    """

    def _field(text: str) -> str:
        return f"{len(text.encode('utf-8', 'replace'))}:{text}"

    parts = [
        _field(str(r.get("key") or ""))
        + _field(str(r.get("title") or ""))
        + f"{1 if r.get('gate') else 0}"
        for r in sorted(rows or [], key=lambda r: str(r.get("key") or ""))
    ]
    return hashlib.sha256("".join(parts).encode("utf-8", "replace")).hexdigest()[:32]


def _reconcile_reservation_tx(con, plan_id: str, *, stopped: bool, now: float) -> None:
    """Reconcile ONE attempt's resource obligation. **Caller must already hold the transaction.**

    Every exit that removes a dispatch record has to do this, in the SAME commit, and doing it one
    exit at a time is what made it a defect three review rounds running: `clear_dispatch`, then the
    settlement's main path, then the mission-moved early return, then the pre-key discharge. Each
    fix was correct and the next exit still leaked, because the rule lived at the call sites
    instead of with the delete.

    So the rule is written once, here, and covers all three shapes a reservation can be in:

    * `reserved`, no key — nothing was ever launched, so the slot comes back unconditionally. This
      is the cancelled-before-`on_key` case, and it is safe *because* there is no key: no process
      can exist behind a row that never named one.
    * `launching`/`live` with a key — a process may exist. `stopped` (the boundary was proved
      empty) ends it; anything else KEEPS the charge and makes the row `live` so the reaper can
      return the slot when that process actually dies. `spared` lands here: still running, under
      another owner, still costing this host.
    * anything already `ended` — untouched. A late caller does not reopen a settled obligation.
    """
    con.execute(
        "UPDATE mission_spawns SET ended_at=?, end_reason=?, state='ended' "
        "WHERE plan_id=? AND ended_at IS NULL AND state='reserved' AND session_key IS NULL",
        (now, "the dispatch record went away before a session key was ever minted", plan_id),
    )
    if stopped:
        con.execute(
            "UPDATE mission_spawns SET ended_at=?, end_reason=?, state='ended' "
            "WHERE plan_id=? AND ended_at IS NULL AND session_key IS NOT NULL",
            (now, "the launch was cleaned up and proved stopped", plan_id),
        )
    else:
        con.execute(
            "UPDATE mission_spawns SET state='live' "
            "WHERE plan_id=? AND ended_at IS NULL AND state='launching' "
            "AND session_key IS NOT NULL",
            (plan_id,),
        )


def clear_dispatch(
    mission_id: str,
    *,
    expect_plan: str | None = None,
    stopped: bool = False,
    path: Path | None = None,
) -> bool:
    """Drop the in-flight dispatch record. True if a row went.

    Called when a teardown has PROVED the boundary empty and the record's obligation is therefore
    discharged (#904 review 3, finding 1). Separate from `settle_dispatch` because the two facts
    arrive in that order: the settlement happens first and may refuse, and only then does the
    caller learn whether the session it started could be stopped.
    """
    validate_id(mission_id)
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            # Two literal statements rather than one built by concatenation: the shell-free /
            # no-string-built-SQL rule is a property of the source, not of the values.
            if expect_plan is None:
                n = con.execute(
                    "DELETE FROM mission_dispatches WHERE mission_id=?", (mission_id,)
                ).rowcount
            else:
                n = con.execute(
                    "DELETE FROM mission_dispatches WHERE mission_id=? AND plan_id=?",
                    (mission_id, expect_plan),
                ).rowcount
            # THE RESERVATION GOES WITH IT **ONLY WHEN THE PROCESS IS PROVED STOPPED** (#894
            # review 2, finding 3). A launch that failed and was then cleaned up left a KEYLESS
            # ledger row behind: the reaper skips those (there is nothing to probe), so repeated
            # cleaned-up failures exhausted the cap with no live children anywhere.
            #
            # `stopped` and `spared` stay distinct, which is the whole reason this is a parameter
            # rather than something inferred from the delete. `spared` means another mission now
            # holds that agent — the process is still on this host and somebody still answers for
            # it — so its slot must NOT come back here; that is a transfer, not a discharge. Only
            # a teardown that proved the boundary empty returns capacity.
            # THE RESOURCE TRANSITION HAPPENS HERE, IN THIS TRANSACTION (#894 review 5).
            #
            # It was a second write after this one committed — `clear_dispatch(...)` and then a
            # separate `hand_back_spawn_to_the_reaper(...)`, exceptions suppressed. A crash or a
            # SQLite error between the two left the reservation stranded in `launching`, which the
            # reaper never returns, AND no dispatch record for a later recovery pass to find. The
            # obligation became unreachable by construction, which is worse than the leak it was
            # meant to fix.
            #
            # Folding it in also fixes the other half of the same finding: every exit that clears a
            # dispatch now transitions its reservation correctly without having to remember a
            # second call, including the recovery branches that did not.
            #
            #   stopped  -> the boundary was proved empty; the slot comes back now.
            #   otherwise -> `spared` or an unproved stop. The charge is KEPT, because that process
            #                is still on this host — but the row becomes `live` so the reaper can
            #                return the slot when it really dies. Scoped to `launching` with a key,
            #                so a genuinely pre-launch `reserved` row stays unprobeable.
            if expect_plan:
                _reconcile_reservation_tx(con, expect_plan, stopped=stopped, now=time.time())
            # A PROVED STOP CONFIRMS THE FAILED ATTEMPT'S TEARDOWN (#966). `stopped` is the one
            # answer that means the boundary is empty; `spared` and an unproved stop change nothing.
            if stopped and expect_plan:
                con.execute(
                    "UPDATE mission_dispatch_evidence SET teardown_confirmed=1 "
                    "WHERE mission_id=? AND plan_id=?",
                    (mission_id, expect_plan),
                )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return bool(n)


def process_owner() -> str:
    """This process's identity, for a lease another process can evaluate (#904 review 2, f.3).

    `pid:starttime`, both from ``/proc``: the pid makes the question answerable and the start
    time makes the pid trustworthy, because pids are reused. Single-host by construction, which
    this app is — sibling INSTANCES share a store on one machine.
    """
    return f"{os.getpid()}:{_proc_started(os.getpid()) or 'unknown'}"


def _proc_started(pid: int) -> str | None:
    """The kernel's start-time stamp for ``pid``, or None. Field 22 of ``/proc/<pid>/stat``,
    parsed from the LAST ``)`` because field 2 is the executable name and may contain spaces and
    parentheses of its own."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    cut = raw.rfind(")")
    if cut < 0:
        return None
    fields = raw[cut + 2 :].split()
    return fields[19] if len(fields) > 19 else None


def owner_is_live(token: object) -> bool | None:
    """Is the process that took this lease still running? ``None`` means we cannot tell.

    Three answers, and the third is the point: an unparseable token, or a ``/proc`` entry that
    will not read, is not evidence that the owner is gone — and acting on a live dispatch is the
    harmful direction, because it tears down an agent that is being launched right now.
    """
    if not isinstance(token, str) or ":" not in token:
        return None
    pid_s, _, started = token.partition(":")
    try:
        pid = int(pid_s)
    except ValueError:
        return None
    now = _proc_started(pid)
    if now is None:
        return False  # no such process: provably gone
    if started == "unknown":
        return None
    return now == started


def claim_spawn(
    mission_id: str,
    *,
    parent_key: str,
    engine: str,
    cwd: str,
    brief: str,
    project_id: str | None = None,
    engine_reason: str = "",
    cap: int | None = None,
    owner: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Reserve a SUB-AGENT slot and move the mission to `dispatching`. One winner, one transaction.

    **The cap and the reservation are one act, and splitting them is the whole bug.** "Count the
    sub-agents, then start one" is check-then-act: two approvals both read a count under the cap,
    both start, and the mission ends with one more agent than the operator ever allowed — with
    each individual check having been true when it was made. So the count and the INSERT happen
    inside one `BEGIN IMMEDIATE`, and `mission_dispatches` is keyed by `mission_id`, so the second
    writer cannot even create a row. Exactly one caller leaves this function having reserved a
    slot; every other is told, and nothing was launched for it.

    **A spawn IS a dispatch**, deliberately. It writes the same durable row, transits the mission
    the same way, and is settled by `settle_dispatch` — so it inherits the launch fence, the
    attempt-generation CAS, the alive-is-not-started gate and the teardown reconciliation, none of
    which a second launcher would have for free. The only thing that differs is `spawn_parent`,
    which is what the settlement adopts by.

    **The cap counts LIVE sub-agents, not spawns ever made** — and "live" means the PROCESS, not
    the roster entry. Releasing a sub-agent hands over ownership and stops nothing, so it does not
    return capacity: only a proved stop, or the reaper observing the process dead, does. This
    docstring previously said a released child "has stopped consuming the host", which was the
    original defect rather than the contract (review 1, findings 4 and 5) — counting the roster
    made the bound evadable by spawn -> release -> spawn while every one of those agents ran on.

    Raises `MissionError` — 409 at the cap or against a mission that cannot hold work, 404 if it
    is gone. Never partially applied: the transaction is the boundary.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    limit = SPAWN_CAP if cap is None else max(0, int(cap))
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            cur = con.execute("SELECT state FROM missions WHERE id=?", (mission_id,)).fetchone()
            if cur is None:
                raise MissionError(f"unknown mission {mission_id}", status=404)
            state = str(cur["state"] or "")
            if state != "running":
                # A spawn adds a reviewer to work that is UNDER WAY. Anything else — planning,
                # a launch already in flight, a closed record — has nothing to review, and
                # `dispatching` in particular would be a second concurrent launch.
                raise MissionError(
                    f"a mission that is {state} cannot spawn a sub-agent", status=409
                )
            # THE PARENT MUST BE ONE OF THIS MISSION'S OWN LIVE SESSIONS. A spawn is parented to
            # the work it is reviewing; a key from somewhere else would put an unrelated session's
            # id into the roster's provenance and make the tree a fiction.
            held = con.execute(
                "SELECT 1 FROM mission_sessions "
                "WHERE mission_id=? AND session_key=? AND removed_at IS NULL",
                (mission_id, parent_key),
            ).fetchone()
            if held is None:
                raise MissionError(
                    "that session is not held by this mission, so it cannot parent a spawn",
                    status=409,
                )
            # COUNTED FROM THE RESOURCE LEDGER, NOT THE ROSTER (review 1, findings 4 and 5).
            #
            # The roster answers "does this mission claim this session", which is ownership and is
            # mutable on purpose. It was the wrong question twice over: `removed_at IS NULL` let a
            # RELEASE free the slot while the agent was still running (detach stops nothing), and
            # `spawned_by IS NOT NULL` both counted the primary — whose `spawned_by` is the
            # literal `"dispatch"`, so a cap of 1 could never spawn at all — and dropped a child
            # the moment an ordinary re-adopt rewrote that column to NULL.
            #
            # `mission_spawns` answers the question the cap is actually about: what did this
            # mission start that nobody has proven stopped. Membership cannot move it.
            live = con.execute(
                "SELECT COUNT(*) AS n FROM mission_spawns "
                "WHERE mission_id=? AND ended_at IS NULL",
                (mission_id,),
            ).fetchone()["n"]
            if live >= limit:
                raise MissionError(
                    f"this mission already holds {live} sub-agent(s), which is its limit of "
                    f"{limit}. A slot comes back when one of them STOPS — releasing a session "
                    f"hands over ownership without stopping the agent, so it does not return "
                    f"capacity",
                    status=409,
                )
            plan_id = f"pln_{uuid.uuid4().hex}"
            # THE SLOT IS RESERVED HERE, in the same transaction that counted it, so two
            # concurrent approvals cannot both see room. Closed later by evidence only.
            con.execute(
                "INSERT INTO mission_spawns "
                "(plan_id, mission_id, parent_key, session_key, started_at, state) "
                "VALUES (?,?,?,NULL,?,'reserved')",
                (plan_id, mission_id, parent_key, ts),
            )
            try:
                con.execute(
                    "INSERT INTO mission_dispatches "
                    "(mission_id, plan_id, engine, cwd, session_key, started_at, project_id, "
                    " engine_reason, brief, owner, spawn_parent) "
                    "VALUES (?,?,?,?,NULL,?,?,?,?,?,?)",
                    (
                        mission_id,
                        plan_id,
                        engine,
                        cwd,
                        ts,
                        project_id,
                        engine_reason,
                        brief,
                        owner or process_owner(),
                        parent_key,
                    ),
                )
            except sqlite3.IntegrityError as e:
                # The PRIMARY KEY refused it: a launch for this mission is already in flight.
                raise MissionError(
                    "a launch is already in flight for this mission", status=409
                ) from e
            con.execute(
                "UPDATE missions SET state='dispatching', updated_at=? "
                "WHERE id=? AND state='running'",
                (ts, mission_id),
            )
            _append_event(
                con,
                mission_id,
                "session",
                at=ts,
                session_key=parent_key,
                text=f"a sub-agent is being started to work alongside {parent_key}",
                meta={"spawn": True, "parent": parent_key, "cap": limit, "live": live},
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {
        "plan_id": plan_id,
        "project_id": project_id,
        "cwd": cwd,
        "engine": engine,
        "engine_reason": engine_reason,
        "brief": brief,
        "spawn_parent": parent_key,
    }


def claim_plan(
    mission_id: str,
    plan_id: str,
    *,
    cwd: str | None = None,
    project_id: str | None = None,
    owner: str | None = None,
    expect_objectives: str | None = None,
    require_objectives: bool = False,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Take the plan the operator SAW and move the mission to `dispatching`. One winner.

    **Compare-and-set on `plan_id`, and the state transition in the SAME transaction.** Those two
    halves are one fact — "this proposal is the one being launched" — and splitting them is the
    approval race in its usual shape: two taps both read a matching plan, both transition, and two
    unattended agents start against one mission. The `planned -> dispatching` UPDATE
    carries its own `WHERE state='planned'`, so the loser changes nothing and is told.

    The plan row is DELETED here rather than left behind: it has been consumed, and a proposal
    that survives its own dispatch is a button the operator can press again — and a
    `mission_dispatches` row takes its place, so the launch about to happen is durable BEFORE it
    happens (#904 review 2). A crash between here and the settlement is then recoverable, which
    is the difference between "the app died" and "this mission is `dispatching` for ever".

    **`cwd` and `project_id` are the caller's FRESHLY RESOLVED values, not the ones the plan was
    stored with** (#904 review 5). A plan can sit on screen while its project is archived, or
    while its default folder is repointed; launching an unattended agent into the
    directory that project used to mean is the `stale policy across the await` family with a
    filesystem path on the end of it. The route resolves the project entity immediately before
    this call and passes the answer in; omitting it keeps the stored value, which is only correct
    for a caller that has nothing newer.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            row = con.execute(
                "SELECT plan_id, project_id, cwd, engine, engine_reason, brief "
                "FROM mission_plans WHERE mission_id=?",
                (mission_id,),
            ).fetchone()
            if row is None:
                raise MissionError("this mission has no plan to dispatch", status=409)
            if str(row["plan_id"]) != plan_id:
                raise MissionError(
                    "the plan was replaced while you were looking at it; read it again",
                    status=409,
                )
            # NOT WHILE A NEWER PLAN IS BEING PREPARED (#967). A Plan again leaves the previous
            # plan stored until the model returns, so without this the operator could launch plan
            # A after asking for a new one. It would also break recovery: a refused launch puts
            # the plan back under the CURRENT generation, which is the pending one, and recovery
            # would then report A as that attempt's result. An operator's edit settles `ready`
            # under a new generation, so "edit the plan" really does unblock this.
            pstate = con.execute(
                "SELECT plan_state FROM missions WHERE id=?", (mission_id,)
            ).fetchone()
            if pstate is not None and pstate["plan_state"] == "pending":
                raise MissionError(
                    "a plan is still being prepared for this mission; wait for it or edit the plan",
                    status=409,
                )
            # WHAT DONE MEANS, COMPARED IN THIS TRANSACTION (#904 review 4, finding 2). The
            # route reads the objectives and compares the digest, and then claims the plan in a
            # SEPARATE transaction — `patch_objectives` is legal in the gap, so a concurrent edit
            # could replace the checklist the operator approved after they approved it and the
            # dispatch still launched against the new one. The comparand belongs where the claim
            # commits, which is here.
            if expect_objectives is not None or require_objectives:
                objs = [
                    dict(r)
                    for r in con.execute(
                        "SELECT key, title, gate, state FROM mission_objectives "
                        "WHERE mission_id=? ORDER BY ord ASC",
                        (mission_id,),
                    ).fetchall()
                ]
                if require_objectives and not objs:
                    raise MissionError(
                        "this mission has no objectives, so it does not know what finishing "
                        "means",
                        status=409,
                    )
                if expect_objectives is not None and objectives_digest(objs) != expect_objectives:
                    raise MissionError(
                        "the objectives changed since you read them; read the plan again",
                        status=409,
                    )
            use_cwd = cwd if cwd is not None else row["cwd"]
            use_project = project_id if cwd is not None else row["project_id"]
            if not use_cwd:
                raise MissionError("a plan without a project cannot be dispatched", status=422)
            if not row["engine"]:
                raise MissionError("a plan without an agent cannot be dispatched", status=422)
            # THE MISSION ACQUIRES ITS WORKING DIRECTORY HERE, in the same statement that starts
            # it. That is not a convenience: the schema's launch CHECK — `state IN
            # ('draft','planned','abandoned') OR cwd IS NOT NULL` — makes a cwd-less
            # `dispatching` unrepresentable, so a mission planned from the picker (the whole
            # point of a draft with no project) could not be dispatched at all without it.
            #
            # And it is the honest moment for it: the plan is where the project was CHOSEN, and
            # `POST /api/missions` resolved the path from the entity exactly as `put_plan` did.
            # The mission takes the resolved values, never a client's.
            moved = con.execute(
                "UPDATE missions SET state='dispatching', project_id=?, cwd=?, updated_at=? "
                "WHERE id=? AND state='planned'",
                (use_project, use_cwd, ts, mission_id),
            ).rowcount
            if not moved:
                cur = con.execute("SELECT state FROM missions WHERE id=?", (mission_id,)).fetchone()
                raise MissionError(
                    f"the mission is {str(cur['state']) if cur else 'gone'}, not planned",
                    status=409,
                )
            _append_event(
                con,
                mission_id,
                "state",
                at=ts,
                text="planned -> dispatching",
                meta={"from": "planned", "to": "dispatching", "plan_id": plan_id},
            )
            con.execute("DELETE FROM mission_plans WHERE mission_id=?", (mission_id,))
            # A PRIMARY CLAIM MAY NOT SILENTLY REPLACE AN UNRESOLVED ATTEMPT (#894 review 10).
            #
            # The upsert below rewrites every column it names — and `spawn_parent` was not one of
            # them, so a primary dispatch landing on top of an outstanding SPAWN row inherited the
            # child's parentage and was then settled down the child branch: a pre-launch refusal
            # returned the mission to `running` and never restored the approved primary plan.
            #
            # Resetting `spawn_parent` alone does not fix it. The row being overwritten is also
            # the only thing that references the superseded attempt's resource obligation, and
            # `open_spawns` selects `live` while the reaper skips `reserved` — so the replaced
            # attempt's charge became unreachable, and a KEYED one lost its cleanup record too.
            # That is round 9's rule again, one caller further out: an obligation dropped because
            # something else wrote over it is an obligation nobody ever discharges.
            #
            # So the outgoing attempt is resolved, never trampled, and which resolution is
            # possible depends on the one fact that distinguishes the two crash outcomes
            # everywhere else in this module — whether a key was ever minted.
            prior = con.execute(
                "SELECT plan_id, session_key FROM mission_dispatches WHERE mission_id=?",
                (mission_id,),
            ).fetchone()
            if prior is not None:
                if prior["session_key"]:
                    # A KEY WAS MINTED, so an agent may be running and this row is its only
                    # durable trace. There is nowhere to preserve that obligation across the
                    # replacement, so the replacement is refused instead — the same direction
                    # every other uncertain case in this module takes. Settlement, request-time
                    # teardown or a recovery pass resolves it, and then this claim succeeds.
                    raise MissionError(
                        "an earlier attempt on this mission started a session that has not been "
                        "accounted for yet; it has to be settled before a new dispatch can "
                        "replace it",
                        status=409,
                    )
                # KEYLESS: nothing was ever spawned under it, so the obligation is dischargeable
                # by construction rather than by observation — and it is discharged HERE, in the
                # transaction that overwrites the row it belongs to, so the two cannot come apart.
                _reconcile_reservation_tx(con, str(prior["plan_id"]), stopped=True, now=ts)
            # THE INTENT, DURABLE BEFORE THE LAUNCH. `session_key` is NULL until the key is
            # minted; recovery reads that difference as "nothing was spawned" versus "something
            # may have been", which are the two crash outcomes that need different answers.
            con.execute(
                "INSERT INTO mission_dispatches "
                "(mission_id, plan_id, engine, cwd, session_key, started_at, project_id, "
                " engine_reason, brief, owner) "
                "VALUES (?,?,?,?,NULL,?,?,?,?,?) "
                "ON CONFLICT(mission_id) DO UPDATE SET plan_id=excluded.plan_id, "
                "engine=excluded.engine, cwd=excluded.cwd, session_key=NULL, "
                "started_at=excluded.started_at, project_id=excluded.project_id, "
                "engine_reason=excluded.engine_reason, brief=excluded.brief, "
                # EVERY primary-specific field, named. An upsert that lists only what it means to
                # change inherits the rest from whatever it landed on, which is how a primary
                # dispatch came to be parented to a child.
                "owner=excluded.owner, spawn_parent=NULL, "
                # A new attempt has typed nothing YET: `unknown`, never the last attempt's answer.
                "seed_outcome=NULL, teardown_confirmed=0",
                (
                    mission_id,
                    plan_id,
                    str(row["engine"]),
                    str(use_cwd),
                    ts,
                    use_project,
                    str(row["engine_reason"] or ""),
                    str(row["brief"] or ""),
                    owner or process_owner(),
                ),
            )
            # A NEW ATTEMPT SUPERSEDES THE LAST FAILURE'S EVIDENCE (#966).
            con.execute("DELETE FROM mission_dispatch_evidence WHERE mission_id=?", (mission_id,))
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {
        "plan_id": plan_id,
        "mission_id": mission_id,
        "project_id": use_project,
        "cwd": use_cwd,
        "engine": row["engine"],
        "engine_reason": str(row["engine_reason"] or ""),
        "brief": str(row["brief"] or ""),
    }


def note_dispatch_session(
    mission_id: str,
    session_key: str,
    *,
    expect_plan: str | None = None,
    nonce: str | None = None,
    path: Path | None = None,
) -> bool:
    """Stamp the key a dispatch is about to launch, BEFORE it exists (#904 review 2).

    `nonce` is the attempt's discriminator for a late-id launch (#989), stamped in the same write
    as its placeholder key. Omitted, the column is cleared, so a record never carries an earlier
    attempt's nonce beside a later attempt's key.

    Called from inside the launcher the instant the id is minted and before anything is spawned.
    The record is therefore allowed to name a session that never came to be — recovery probes the
    engine's own store rather than trusting it — and is never allowed to miss one that did, which
    is the only ordering under which a crashed dispatch can be reconciled at all.

    Returns whether a row was stamped. False means the dispatch has already been settled (or was
    never claimed), which the launcher treats as "this launch no longer has an owner".

    **`expect_plan` is the dispatch's IDENTITY, and without it this stamps whoever is there**
    (#904 review 15). `dispatching -> planned` is a legal retreat, so an attempt can still be in
    flight while the mission goes back, a NEW plan is proposed and claimed — overwriting this row
    through `ON CONFLICT(mission_id)` — and the old attempt then resumes and writes its session
    key onto the new attempt's row. Every mutation after the claim was keyed on `mission_id`
    alone, which is a name for the MISSION, not for the attempt. `plan_id` is minted per plan and
    is already on the row, so it is the generation; passing it makes each write a CAS.
    """
    validate_id(mission_id)
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            if expect_plan is None:
                n = con.execute(
                    "UPDATE mission_dispatches SET session_key=?, attempt_nonce=? "
                    "WHERE mission_id=?",
                    (session_key, nonce, mission_id),
                ).rowcount
            else:
                n = con.execute(
                    "UPDATE mission_dispatches SET session_key=?, attempt_nonce=? "
                    "WHERE mission_id=? AND plan_id=?",
                    (session_key, nonce, mission_id, expect_plan),
                ).rowcount
            # THE LEDGER MOVES IN THE SAME TRANSACTION (#894 review 4, carry-forward).
            #
            # These were two writes: this one, and a best-effort `note_spawn_session` beside it in
            # the launcher. A crash between them left a dispatch naming a session and a reservation
            # that did not — the divergence the record exists to make impossible, in the one window
            # where a process may already be starting. One `BEGIN IMMEDIATE` now covers both, so
            # either the launch is recorded everywhere or nowhere.
            #
            # Scoped to `launching`/`reserved` and never to `ended`: a reservation somebody has
            # already discharged is not reopened by a late key.
            if n:
                con.execute(
                    "UPDATE mission_spawns SET session_key=?, state='launching' "
                    "WHERE plan_id=? AND ended_at IS NULL AND state='reserved'",
                    (session_key, expect_plan or ""),
                )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return bool(n)


def _seed_outcome(value: object) -> str:
    """A stored seed outcome, with anything unrecognised — including NULL — read as `unknown`."""
    return value if isinstance(value, str) and value in SEED_OUTCOMES else "unknown"


def note_dispatch_evidence(
    mission_id: str,
    *,
    expect_plan: str | None,
    seed_outcome: str,
    teardown_confirmed: bool,
    path: Path | None = None,
) -> bool:
    """Record what this attempt typed and whether its teardown was proved (#966). True if stamped.

    Called by the dispatcher once the launcher has returned and BEFORE the mission is settled, so
    the settlement can copy it. Bound to the attempt by `expect_plan`, like every other write on
    this record: a superseded attempt's evidence must not land on the next attempt's row. An
    unrecognised outcome is stored as `unknown`, and only a literal `True` confirms a teardown.
    """
    validate_id(mission_id)
    seed = _seed_outcome(seed_outcome)
    confirmed = 1 if teardown_confirmed is True else 0
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            if expect_plan is None:
                n = con.execute(
                    "UPDATE mission_dispatches SET seed_outcome=?, teardown_confirmed=? "
                    "WHERE mission_id=?",
                    (seed, confirmed, mission_id),
                ).rowcount
            else:
                n = con.execute(
                    "UPDATE mission_dispatches SET seed_outcome=?, teardown_confirmed=? "
                    "WHERE mission_id=? AND plan_id=?",
                    (seed, confirmed, mission_id, expect_plan),
                ).rowcount
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return bool(n)


#: The operator-facing sentence for each outcome. "Nothing was typed" appears ONLY for the two
#: outcomes that establish it. No brief text, ever.
_SEED_MESSAGES = {
    "not_attempted": "The session never became ready, so nothing was typed.",
    "zero_write": (
        "Typing the brief failed before any of it reached the session, so nothing was typed."
    ),
    "partial": "Part of the brief may have been typed before the session stopped.",
    "delivered": "The brief was typed before the launch failed.",
    "unknown": "The brief may have been typed before the session stopped.",
}

#: Why Start again is refused for an outcome that does not establish nothing was written.
_SEED_REFUSALS = {
    "partial": "part of the brief may have been typed, so starting again could type it twice",
    "delivered": "the brief was typed, so the agent may already have acted on it",
    "unknown": "nothing shows the brief was not typed, so starting again could repeat it",
}


def _failure_message(seed: str, confirmed: bool) -> str:
    """The plain-language line a failure event carries beside its technical detail (#966)."""
    base = _SEED_MESSAGES.get(seed, _SEED_MESSAGES["unknown"])
    if seed not in RETRYABLE_SEED_OUTCOMES:
        return f"{base} It cannot be started again."
    if not confirmed:
        return (
            f"{base} The session could not be confirmed stopped, so it cannot be started again yet."
        )
    return f"{base} Start again restores the plan."


def _retry_verdict(con, mission_id: str) -> dict:
    """May this mission be started again? Read INSIDE the caller's transaction (#966).

    Returns ``{"eligible", "seed_outcome", "reason", "evidence"}``. Eligible only when ALL hold:

    * the mission is `failed` and not archived or mid-archive;
    * a failed primary launch left evidence, the mission never ran (`running`, `review` or `done`
      on its timeline), and that evidence establishes nothing was written (`not_attempted` or
      `zero_write`) with a confirmed teardown;
    * no dispatch record remains — neither an attempt in flight nor a retained teardown obligation,
      either of which means an agent may still be out there;
    * there is a plan to restore.

    The first failing condition names the reason. The caller's own evidence is never consulted.
    """
    ev = con.execute(
        "SELECT plan_id, session_key, seed_outcome, teardown_confirmed, project_id, cwd, engine, "
        "engine_reason, brief FROM mission_dispatch_evidence WHERE mission_id=?",
        (mission_id,),
    ).fetchone()
    seed = _seed_outcome(ev["seed_outcome"]) if ev is not None else None

    def verdict(reason: str) -> dict:
        return {"eligible": not reason, "seed_outcome": seed, "reason": reason, "evidence": ev}

    m = con.execute(
        "SELECT state, archived_at, archiving_at, unarchiving_at FROM missions WHERE id=?",
        (mission_id,),
    ).fetchone()
    if m is None:
        return verdict("the mission does not exist")
    state = str(m["state"] or "")
    if state != "failed":
        return verdict(f"only a failed mission can be started again, and this one is {state}")
    if m["archived_at"] is not None or m["archiving_at"] is not None or m["unarchiving_at"]:
        return verdict("an archived mission cannot be started again; unarchive it first")
    if ev is None:
        return verdict("there is no record of what the failed launch typed")
    ran = con.execute(
        "SELECT 1 FROM mission_events WHERE mission_id=? AND kind='state' AND "
        "(CASE WHEN json_valid(meta) THEN json_extract(meta, '$.to') END) "
        "IN ('running', 'review', 'done') LIMIT 1",
        (mission_id,),
    ).fetchone()
    if ran is not None:
        return verdict("this mission has run, so its agent may already have acted")
    if seed not in RETRYABLE_SEED_OUTCOMES:
        return verdict(_SEED_REFUSALS.get(str(seed), _SEED_REFUSALS["unknown"]))
    if not ev["teardown_confirmed"]:
        return verdict("the failed session could not be confirmed stopped")
    if con.execute("SELECT 1 FROM mission_dispatches WHERE mission_id=?", (mission_id,)).fetchone():
        return verdict("a launch for this mission is still in flight or not yet accounted for")
    has_plan = con.execute(
        "SELECT 1 FROM mission_plans WHERE mission_id=?", (mission_id,)
    ).fetchone()
    if has_plan is None and not (ev["engine"] and ev["cwd"] and ev["brief"]):
        return verdict("there is no plan to start again from")
    return verdict("")


def _restore_plan_for_start_again_tx(con, mission_id: str, ev, ts: float) -> str:
    """Put the failed launch's proposal back as the plan, `ready` under the CURRENT generation.

    **Caller holds the transaction.** Returns the plan id. A plan row that somehow exists is kept
    and re-bound to the current generation; otherwise the proposal is inserted under a NEW plan id,
    so a late write keyed on the failed attempt's id — `expect_plan` on a recovery pass — cannot
    land on the attempt Begin starts next. Never leaves `plan_state='ready'` without a plan row:
    `_retry_verdict` refuses when there is nothing to restore.
    """
    have = con.execute(
        "SELECT plan_id FROM mission_plans WHERE mission_id=?", (mission_id,)
    ).fetchone()
    if have is not None:
        plan_id = str(have["plan_id"])
        con.execute(
            "UPDATE mission_plans SET generation=(SELECT plan_generation FROM missions WHERE id=?) "
            "WHERE mission_id=?",
            (mission_id, mission_id),
        )
    else:
        plan_id = f"pln_{uuid.uuid4().hex}"
        con.execute(
            "INSERT INTO mission_plans "
            "(mission_id, plan_id, project_id, cwd, engine, engine_reason, brief, created_at, "
            " generation) "
            "VALUES (?,?,?,?,?,?,?,?,(SELECT plan_generation FROM missions WHERE id=?))",
            (
                mission_id,
                plan_id,
                ev["project_id"],
                ev["cwd"],
                ev["engine"],
                str(ev["engine_reason"] or ""),
                str(ev["brief"]),
                ts,
                mission_id,
            ),
        )
    con.execute(
        "UPDATE missions SET plan_state='ready', plan_at=?, plan_detail=NULL WHERE id=?",
        (ts, mission_id),
    )
    return plan_id


def open_spawn_count(mission_id: str, *, path: Path | None = None) -> int:
    """How many sub-agents this mission started that nobody has proven stopped (#894).

    The number the cap is enforced on, published so the console can display the SAME definition
    the claim transaction uses. The two disagreeing by one — the UI counting roster roles, the
    claim counting `spawned_by` — is exactly review 1, finding 5, and the fix is one source.

    Fails CLOSED for display purposes by raising: a count that silently reads 0 would render an
    empty budget over a mission that is holding agents, and "we could not look" is not "none".
    """
    validate_id(mission_id)
    con = _ready(path)
    try:
        return int(
            con.execute(
                "SELECT COUNT(*) AS n FROM mission_spawns "
                "WHERE mission_id=? AND ended_at IS NULL",
                (mission_id,),
            ).fetchone()["n"]
        )
    finally:
        con.close()


def open_spawns(mission_id: str, *, path: Path | None = None) -> list[dict]:
    """The open ledger rows, so a caller can probe their processes and close what is dead."""
    validate_id(mission_id)
    con = _ready(path)
    try:
        # ONLY `live` ROWS ARE PROBEABLE (#894 review 3, finding 1). A `reserved` row has no
        # process and a `launching` one has no socket YET — the identity is recorded before the
        # spawn so the record can never be behind reality, which means a reaper that probed it
        # would read "no socket" as "dead" and free a slot whose agent was about to start
        # successfully. Adoption never repairs that, because it only updates rows still open.
        #
        # The filter lives here rather than in the reaper so every caller inherits it.
        rows = con.execute(
            "SELECT plan_id, parent_key, session_key, started_at FROM mission_spawns "
            "WHERE mission_id=? AND ended_at IS NULL AND state='live' ORDER BY started_at",
            (mission_id,),
        ).fetchall()
        return [
            {
                "plan_id": str(r["plan_id"]),
                "parent_key": str(r["parent_key"]),
                "session_key": r["session_key"],
                "started_at": float(r["started_at"] or 0),
            }
            for r in rows
        ]
    finally:
        con.close()


def note_spawn_session(plan_id: str, session_key: str, *, path: Path | None = None) -> None:
    """Stamp the key onto the reservation the moment it is minted.

    Before this the row is a reservation with no process to point at; after it, the row is the
    only durable link between a slot and the agent occupying it. Written on the same ordering
    rule as `note_dispatch_session`: the record may be ahead of reality, never behind it.
    """
    con = _ready(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute(
            "UPDATE mission_spawns SET session_key=?, state='launching' "
            "WHERE plan_id=? AND ended_at IS NULL",
            (session_key, plan_id),
        )
        con.execute("COMMIT")
    except Exception:
        with contextlib.suppress(Exception):
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def close_spawn(
    plan_id: str, *, reason: str, now: float | None = None, path: Path | None = None
) -> bool:
    """Free a reserved slot. **Callers must have EVIDENCE, not an absence of contrary news.**

    Two things legitimately close a row: a launch that provably started nothing, and a process
    observed dead. A release, a detach, an archive and an unreadable probe are none of those — a
    slot is a statement about this host, and the mission letting go of a session does not stop it.

    Returns whether a row moved, so a caller can tell "closed it" from "it was already closed"
    rather than inferring either.
    """
    ts = float(now if now is not None else time.time())
    con = _ready(path)
    try:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute(
            "UPDATE mission_spawns SET ended_at=?, end_reason=?, state='ended' "
            "WHERE plan_id=? AND ended_at IS NULL",
            (ts, str(reason or "")[:200], plan_id),
        )
        moved = cur.rowcount > 0
        con.execute("COMMIT")
        return moved
    except Exception:
        with contextlib.suppress(Exception):
            con.execute("ROLLBACK")
        raise
    finally:
        con.close()


def get_dispatch(mission_id: str, *, path: Path | None = None) -> dict | None:
    """The in-flight dispatch record, or None."""
    validate_id(mission_id)
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT plan_id, engine, cwd, session_key, started_at, project_id, engine_reason, "
            "brief, owner, spawn_parent, seed_outcome, teardown_confirmed, attempt_nonce "
            "FROM mission_dispatches WHERE mission_id=?",
            (mission_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "mission_id": mission_id,
            "plan_id": str(row["plan_id"]),
            "engine": str(row["engine"]),
            "cwd": str(row["cwd"]),
            "session_key": row["session_key"],
            "started_at": float(row["started_at"] or 0),
            "project_id": row["project_id"],
            "engine_reason": str(row["engine_reason"] or ""),
            "brief": str(row["brief"] or ""),
            "owner": row["owner"],
            # NULL for the mission's own launch; the parent's session key for a spawn (#894).
            "spawn_parent": row["spawn_parent"],
            # What this attempt typed, and whether its teardown was proved (#966).
            "seed_outcome": _seed_outcome(row["seed_outcome"]),
            "teardown_confirmed": bool(row["teardown_confirmed"]),
            # The discriminator a late-id launch delivered (#989); None for a pinned-id engine.
            "attempt_nonce": row["attempt_nonce"],
        }
    finally:
        con.close()


def unsettled_dispatches(*, path: Path | None = None) -> list[dict]:
    """Every dispatch record still on disk, with its mission's state. For the recovery pass.

    **Not filtered on `dispatching`** (#904 review 4, finding 1). A record survives its settlement
    only when the teardown could not prove the boundary empty — and those paths move the mission
    to `failed`/`abandoned` FIRST, so a lifecycle filter skipped exactly the rows that represent
    a possibly-live unattended agent nobody has accounted for. Every pass ignored them for ever.

    A row is therefore one of THREE things, and `state` and `held` are how the caller tells them
    apart:

    * mission still `dispatching` — a launch that may be in flight. The owner lease decides.
    * `held` — **SOME open mission owns this session**, so nothing is owed. The row is
      bookkeeping, not an obligation: drop it, and never touch the session.
    * neither — a RETAINED CLEANUP OBLIGATION. An ordinary settlement deletes the row, so a row
      beside a session nobody owns exists only because a teardown could not prove the boundary
      empty and somebody asked to keep it.

    **`held` is a membership question, not a lifecycle one**, and that is the point twice over:

    * keying the obligation on "the mission is not `dispatching`" would have torn down the live
      agent of a dispatch that had just SUCCEEDED, because `running` is not `dispatching` either;
    * and asking only whether THIS mission holds it (#904 review 5, finding 1) would have torn
      down ANOTHER mission's agent: a failed dispatch can retain its row without owning the
      session, and once a second mission legitimately adopts that key, "my mission does not hold
      it" is true and "it is an orphan" is false.

    So the question is global — does anybody hold it — and the answer here is a SNAPSHOT, which
    is not sufficient on its own: an adoption can commit between this read and the teardown. That
    half is closed at the teardown itself, by `cleanup_runtime`'s `spare_if` guard, which is
    re-checked before every signal.
    """
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT d.mission_id, d.plan_id, d.engine, d.cwd, d.session_key, d.started_at, "
            "d.owner, m.state AS state, "
            # The mission's ACTIVE membership of this row's own session. `removed_at IS NULL` is
            # what "owns it now" means everywhere else in this store, so it means it here too.
            # ANY open mission, not this row's own: the question is whether the session has an
            # owner at all, because that is what makes it not an orphan.
            # A late-bound adoption needs nothing more here (#989): the settlement that adopts the
            # real key rewrites this record's `session_key` to it in the same commit, so the record
            # and the roster never name the session differently.
            "  (SELECT 1 FROM mission_sessions s "
            "     JOIN missions om ON om.id = s.mission_id "
            "   WHERE s.session_key = d.session_key AND s.removed_at IS NULL "
            "     AND om.archived_at IS NULL) AS held "
            "FROM mission_dispatches d "
            "JOIN missions m ON m.id = d.mission_id ORDER BY d.started_at"
        ).fetchall()
        return [
            {
                "mission_id": str(r["mission_id"]),
                "plan_id": str(r["plan_id"]),
                "engine": str(r["engine"]),
                "cwd": str(r["cwd"]),
                "session_key": r["session_key"],
                "started_at": float(r["started_at"] or 0),
                "owner": r["owner"],
                "state": str(r["state"] or ""),
                "held": bool(r["held"]),
            }
            for r in rows
        ]
    finally:
        con.close()


def settle_dispatch(
    mission_id: str,
    *,
    to: str,
    detail: str,
    session_key: str | None = None,
    keep_record: bool = False,
    expect_plan: str | None = None,
    discharge_resource: bool = True,
    physical_key: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """End a dispatch: adopt its session and leave `dispatching`, in ONE transaction.

    `physical_key` names the placeholder a late-bound `session_key` runs under (#989), recorded in
    `session_runtime_bindings` in this same transaction — a table of its own, never a column on the
    roster row, so the mapping outlives the mission that adopted it (#994 reviews 3 and 5). Omitted
    — the only shape before #989 — the session runs under its own key.

    **The adoption and the final state are one settlement, and that is the point** (#904 review
    3). Adopting first and transitioning after is two moments, and the gap is exactly long enough
    for the operator to abandon the mission: the terminal transition releases the roster, the late
    adopt re-attaches a live unattended agent to a mission that is already closed, and
    the `dispatching -> running` CAS then fails while the caller reports `running` anyway.

    So the state predicate governs BOTH halves. If the mission is no longer `dispatching`, nothing
    is adopted and the caller is told what the mission actually is — which is its cue to tear the
    session down, because a session nobody owns is the outcome this whole path exists to prevent.

    **`keep_record` leaves the durable row in place** (#904 review 3, finding 1). Deleting it is
    right when the dispatch is genuinely over — but a teardown that reported `leaked`, or raised,
    has not proved the boundary empty: something in that session's process group survived
    SIGKILL, and the record is the only durable trace of an unattended agent that may still be
    running. Deleting it there turns a retryable obligation into an orphan nobody will look for
    again. The mission still settles; the obligation outlives it.

    **`expect_plan` says WHICH ATTEMPT is settling, and `dispatching` alone does not** (#904
    review 15). The state predicate answers "is a dispatch in flight", never "is it MINE".
    `dispatching -> planned` is a legal retreat, so attempt A can still be running while the
    mission goes back, plan B is proposed and claimed, and the mission is `dispatching` again —
    at which point A's settlement satisfies the predicate, adopts A's session under B's approval,
    and deletes B's record. The operator's newer approval is consumed by an agent they did not
    approve. `plan_id` is minted per plan and rides on the row, so it is the attempt's identity;
    a mismatch means this settlement belongs to a superseded dispatch and it touches NOTHING —
    not the state, not the roster, not the record — and says so with `stale`.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    if to not in _ALLOWED.get("dispatching", frozenset()):
        raise MissionError(f"dispatching cannot become {to}", status=422)
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            cur = con.execute("SELECT state FROM missions WHERE id=?", (mission_id,)).fetchone()
            if cur is None:
                con.execute("COMMIT")
                return {"settled": False, "state": "gone", "adopted": False}
            state = str(cur["state"] or "")
            if expect_plan is not None:
                # WHOSE DISPATCH IS THIS? Asked before anything is read as ours. A row that is
                # absent or carries another plan means this attempt was superseded — and a
                # superseded attempt has no claim on the record, so `keep_record` does not enter
                # into it: the row it would keep is somebody else's.
                d = con.execute(
                    "SELECT plan_id FROM mission_dispatches WHERE mission_id=?", (mission_id,)
                ).fetchone()
                if d is None or str(d["plan_id"] or "") != expect_plan:
                    con.execute("COMMIT")
                    return {
                        "settled": False,
                        "state": state,
                        "adopted": False,
                        "stale": True,
                    }
            if state != "dispatching":
                # NOT OURS ANY MORE. The record goes, because this dispatch is over however it
                # ended; the mission's own state is left exactly as whoever moved it left it.
                #
                # …UNLESS THE TEARDOWN COULD NOT PROVE THE BOUNDARY EMPTY. `keep_record` means an
                # unattended agent may still be running, and this row is its only durable trace —
                # "the mission moved on" is not a reason to forget about a process nobody has
                # stopped (#904 review 3, finding 1).
                # …OR WHEN IT REFUSED AN ADOPTION IT WAS ASKED TO MAKE (#894 review 9).
                #
                # `session_key` on this call names a child the caller has just started and cannot
                # own, because the mission moved and the adoption below will never run. Deleting
                # the record here throws away the only durable trace of a running agent nobody
                # has stopped — and the caller has not even ATTEMPTED its teardown yet, so
                # `keep_record` was decided against a different question and cannot speak for
                # this one. A reapable charge is not a cleanup queue: the reaper observes death,
                # it never stops an orphan.
                #
                # So the record outlives this exit and the caller clears it once — and only once
                # — somebody answers for the child: a proved stop, or another mission's ownership.
                # On anything else it stays, and the next recovery pass looks again.
                orphaned = session_key is not None
                if not keep_record and not orphaned:
                    # THE OBLIGATION GOES WITH THE RECORD, HERE TOO (#894 review 6, finding 1).
                    # This exit deleted the dispatch and returned before the reconciliation below
                    # ever ran, so a mission moved out of `dispatching` while recovery awaited
                    # teardown — with the child spared to another mission — lost its only recovery
                    # record and kept an unreachable charge. Nothing could reclaim that slot
                    # afterwards, and reopening the mission did not repair its budget.
                    d0 = con.execute(
                        "SELECT plan_id FROM mission_dispatches WHERE mission_id=?",
                        (mission_id,),
                    ).fetchone()
                    if d0 is not None:
                        _reconcile_reservation_tx(
                            con,
                            str(d0["plan_id"]),
                            # `keep_record=False` on this path means the teardown discharged the
                            # obligation — but `spared` discharges it too, WITHOUT the process
                            # stopping. Only an explicit `discharge_resource` says it stopped.
                            stopped=discharge_resource,
                            now=ts,
                        )
                    con.execute("DELETE FROM mission_dispatches WHERE mission_id=?", (mission_id,))
                con.execute("COMMIT")
                return {
                    "settled": False,
                    "state": state,
                    "adopted": False,
                    # WHETHER THE CALLER STILL OWES SOMETHING. It decides its teardown from this
                    # rather than from `settled`, which is false on both shapes of this exit.
                    "retained": bool(keep_record or orphaned),
                }
            # WHOSE ATTEMPT THIS WAS, read before anything is decided from `to` (#894 review 1,
            # finding 2). `settle_dispatch` was written for the mission's own launch and every
            # branch below assumed it: a child's ordinary refusal — an over-limit brief, a
            # pre-launch policy withdrawal — therefore settled the WHOLE MISSION to `planned`,
            # rewound a parent that was legitimately `running` with a live roster, and republished
            # the CHILD's brief as the mission's own proposal. The next DISPATCH would then have
            # launched the sub-agent's brief as a primary, with `spawn_parent` gone.
            #
            # A spawn is a dispatch, which is the whole design — but it is not the mission's
            # dispatch, and only the settlement can tell the difference.
            spawn_row = con.execute(
                "SELECT spawn_parent, plan_id FROM mission_dispatches WHERE mission_id=?",
                (mission_id,),
            ).fetchone()
            is_spawn = bool(spawn_row and spawn_row["spawn_parent"])
            # THE ATTEMPT'S OWN IDENTITY. Every discharge below names this and nothing else.
            this_plan = str(spawn_row["plan_id"]) if spawn_row else ""
            # A CHILD'S OUTCOME IS NOT THE MISSION'S STATE (review 2, finding 2). The first pass
            # covered only `planned`, which was the refusal path — but a start-evidence timeout, a
            # post-launch exception and a cancellation all settle `failed`, and those took an
            # otherwise-healthy `running` parent terminal with them. Adding an optional reviewer
            # to a mission could therefore END that mission, remove every running-only control,
            # and leave its original session held by a record that says the work stopped.
            #
            # Both non-terminal outcomes now come back to `running`. What is NOT changed is the
            # teardown obligation: `keep_record` and the event text below still carry the child's
            # real outcome, so a possibly-live agent is still tracked and the timeline still says
            # the attempt failed. The mission's lifecycle and the attempt's outcome are two facts,
            # and only one of them belonged to `state`.
            if is_spawn and to in ("planned", "failed"):
                # THE PARENT WAS RUNNING AND STILL IS. For the mission's own launch these are
                # the honest answers — "nothing started, offer the plan again" and "this failed".
                # For a child they would un-run a mission whose sessions never stopped. The
                # attempt ends; the mission does not move.
                #
                # Re-targeting AFTER the `_ALLOWED` guard at the top is safe only because
                # `running` is itself a legal exit from `dispatching` — see `_ALLOWED`, which
                # lists `abandoned`, `failed`, `planned` and `running` for that state. Left as a
                # note rather than an `assert`, because asserts vanish under `python -O` and a
                # guard that matters must not be one. The transition is exercised end to end by
                # `test_a_CHILD_refusal_does_not_rewind_the_running_parent`.
                to = "running"
            if to == "planned" and not is_spawn:
                # THE PROPOSAL GOES BACK, verbatim and under its own id, so the card the operator
                # is looking at still matches and DISPATCH works on the next tap. Restored from
                # the dispatch record because the claim deleted the plan row — the record is the
                # only copy while a dispatch is in flight, which is why it carries the whole
                # proposal rather than only what the launch needed.
                d = con.execute(
                    "SELECT plan_id, project_id, cwd, engine, engine_reason, brief "
                    "FROM mission_dispatches WHERE mission_id=?",
                    (mission_id,),
                ).fetchone()
                if d is not None:
                    con.execute(
                        # Restored under the mission's CURRENT planning generation (#967): it is
                        # the plan the operator approved, and recovery must read it as the result
                        # of the attempt that is on record rather than as a stale one.
                        "INSERT INTO mission_plans "
                        "(mission_id, plan_id, project_id, cwd, engine, engine_reason, brief, "
                        " created_at, generation) VALUES (?,?,?,?,?,?,?,?,"
                        " (SELECT plan_generation FROM missions WHERE id=?)) "
                        "ON CONFLICT(mission_id) DO UPDATE SET plan_id=excluded.plan_id, "
                        "project_id=excluded.project_id, cwd=excluded.cwd, "
                        "engine=excluded.engine, engine_reason=excluded.engine_reason, "
                        "brief=excluded.brief, created_at=excluded.created_at, "
                        "generation=excluded.generation",
                        (
                            mission_id,
                            str(d["plan_id"]),
                            d["project_id"],
                            d["cwd"],
                            d["engine"],
                            str(d["engine_reason"] or ""),
                            str(d["brief"] or ""),
                            ts,
                            mission_id,
                        ),
                    )
            adopted = False
            if session_key:
                # WHOSE SESSION THIS IS, read from the row rather than assumed (#894). A spawn is
                # settled by this very function — that is the point of making it a dispatch — so
                # the one thing that must differ is the adoption: `subagent`, parented to the
                # session it was started to work alongside. Adopting it as another `primary` with
                # `spawned_by='dispatch'` would erase the tree the moment it was created, and the
                # roster is where "which of these did the mission start for itself" is answered.
                d = con.execute(
                    "SELECT spawn_parent, plan_id FROM mission_dispatches WHERE mission_id=?",
                    (mission_id,),
                ).fetchone()
                parent = str(d["spawn_parent"]) if d and d["spawn_parent"] else ""
                if parent:
                    _adopt_tx(con, mission_id, session_key, "sub", parent, ts, path)
                    # THE SLOT NOW POINTS AT A PROCESS. Written in the settlement transaction so
                    # the ledger row and the roster entry become true together — a key on one and
                    # not the other is the torn state the whole record exists to avoid.
                    con.execute(
                        "UPDATE mission_spawns SET session_key=?, state='live' "
                        "WHERE plan_id=? AND ended_at IS NULL",
                        (session_key, str(d["plan_id"])),
                    )
                else:
                    _adopt_tx(con, mission_id, session_key, "primary", "dispatch", ts, path)
                # WHERE ITS RUNTIME LIVES, in the adopting transaction (#989). A late-bound session
                # is adopted under the real id it revealed and runs under the placeholder it was
                # launched with; recording that here means the store knows the mapping from the
                # instant it owns the session, and the alias published after the commit is only a
                # projection of it. It is keyed by the SESSION, not by this mission, so it survives
                # the mission being released, closed and eventually deleted (#994 review 3).
                if physical_key and physical_key != session_key:
                    _bind_runtime_tx(con, session_key, physical_key, ts)
                adopted = True
            con.execute(
                "UPDATE missions SET state=?, updated_at=? WHERE id=? AND state='dispatching'",
                (to, ts, mission_id),
            )
            if to in TERMINAL_STATES:
                con.execute(
                    "UPDATE missions SET closed_at=? WHERE id=? AND closed_at IS NULL",
                    (ts, mission_id),
                )
            # THE EVIDENCE OF A FAILED PRIMARY LAUNCH OUTLIVES ITS RECORD (#966). The dispatch row
            # may be deleted below, so what the dispatcher recorded on it is copied in this commit.
            # A record with no evidence copies `unknown`. Any other outcome supersedes old evidence.
            failure_meta: dict = {}
            if to == "failed" and not is_spawn:
                ev = con.execute(
                    "SELECT plan_id, session_key, seed_outcome, teardown_confirmed, project_id, "
                    "cwd, engine, engine_reason, brief FROM mission_dispatches WHERE mission_id=?",
                    (mission_id,),
                ).fetchone()
                seed = _seed_outcome(ev["seed_outcome"]) if ev is not None else "unknown"
                confirmed = bool(ev["teardown_confirmed"]) if ev is not None else False
                if ev is not None:
                    con.execute(
                        "INSERT INTO mission_dispatch_evidence "
                        "(mission_id, plan_id, session_key, seed_outcome, teardown_confirmed, "
                        " failed_at, project_id, cwd, engine, engine_reason, brief) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(mission_id) DO UPDATE SET plan_id=excluded.plan_id, "
                        "session_key=excluded.session_key, seed_outcome=excluded.seed_outcome, "
                        "teardown_confirmed=excluded.teardown_confirmed, "
                        "failed_at=excluded.failed_at, project_id=excluded.project_id, "
                        "cwd=excluded.cwd, engine=excluded.engine, "
                        "engine_reason=excluded.engine_reason, brief=excluded.brief",
                        (
                            mission_id,
                            str(ev["plan_id"]),
                            session_key or ev["session_key"],
                            seed,
                            1 if confirmed else 0,
                            ts,
                            ev["project_id"],
                            ev["cwd"],
                            ev["engine"],
                            ev["engine_reason"],
                            ev["brief"],
                        ),
                    )
                else:
                    con.execute(
                        "DELETE FROM mission_dispatch_evidence WHERE mission_id=?", (mission_id,)
                    )
                # WHICH SESSION THE LAUNCH STARTED, for the thread's Open session link (#967 P4, PR
                # #986 review). A launch that came up and failed settles through
                # `_orphaned_after_launch`, which passes no `session_key`, so nothing on this event
                # named the session; the key is on the dispatch row `note_dispatch_session` stamped
                # before the spawn, the same row the evidence above is copied from. DISPLAY ONLY: it
                # is never the `session_key` parameter, so it adopts nothing and changes no
                # ownership. Absent when no key was stamped (the launcher failed before minting one)
                # and for anything that is not a plain `engine:native` key, a launch placeholder
                # included, so the browser is never handed a link it would have to distrust.
                launched = str(ev["session_key"] or "") if ev is not None else ""
                launch_link = (
                    {"launch_session_key": launched}
                    if re.fullmatch(r"[a-z0-9_-]{1,32}:[A-Za-z0-9._-]{1,128}", launched)
                    and ":new-" not in launched
                    else {}
                )
                # A SNAPSHOT for the thread. The state write re-checks everything, including
                # conditions that settle after this commit (a retained record being discharged).
                failure_meta = {
                    "seed_outcome": seed,
                    "teardown_confirmed": confirmed,
                    "retry_eligible": seed in RETRYABLE_SEED_OUTCOMES and confirmed,
                    "message": _failure_message(seed, confirmed),
                    **launch_link,
                }
            else:
                con.execute(
                    "DELETE FROM mission_dispatch_evidence WHERE mission_id=?", (mission_id,)
                )
            _append_event(
                con,
                mission_id,
                "state",
                at=ts,
                text=f"dispatching -> {to}" + (f": {detail}" if detail else ""),
                meta={
                    "from": "dispatching",
                    "to": to,
                    "detail": detail,
                    **({"session_key": session_key} if session_key else {}),
                    **failure_meta,
                },
            )
            if keep_record:
                # The dispatch is over and the TEARDOWN is not. Marked rather than deleted, so a
                # later pass can find it and try again — and so the operator's timeline is not
                # the only place a possibly-live agent is mentioned.
                con.execute(
                    "UPDATE mission_dispatches SET session_key=COALESCE(?, session_key) "
                    "WHERE mission_id=?",
                    (session_key, mission_id),
                )
            else:
                # THE ATTEMPT IS OVER AND THE BOUNDARY WAS PROVED EMPTY — that is what
                # `keep_record=False` means, and it is the ONLY thing that frees a reserved
                # sub-agent slot here (#894 review 1, finding 4).
                #
                # `discharge_resource=False` is how a caller says "the record is over but the
                # PROCESS is not" (#894 review 3, finding 3). Startup recovery needs it: a
                # `spared` child was adopted by another mission between this pass's probe and its
                # fence, so it is still running under a new owner. `keep_record` cannot express
                # that — somebody does answer for the agent, so the record is legitimately
                # discharged — and conflating the two freed the originating mission's slot for a
                # process still on this host.
                #
                # Deliberately conditioned on `adopted`: a spawn that reached a live, adopted
                # child is occupying its slot exactly as intended and must keep it. The row is
                # closed only where nothing survived the attempt. The mirror case —
                # `keep_record=True`, an agent that may still be out there — falls to the branch
                # above and keeps BOTH records, because a slot is a statement about this host and
                # "we could not prove it stopped" is not "it stopped".
                if not adopted and this_plan:
                    # THE SAME ONE RULE as every other exit (#894 review 6). `discharge_resource`
                    # is the caller's statement that the process stopped; `spared` says the record
                    # is over but the agent is not, and the helper keeps that charge while making
                    # the row reapable. A `reserved` row with no key is discharged either way,
                    # because nothing can be running behind a row that never named a session.
                    _reconcile_reservation_tx(con, this_plan, stopped=discharge_resource, now=ts)
                con.execute("DELETE FROM mission_dispatches WHERE mission_id=?", (mission_id,))
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {"settled": True, "state": to, "adopted": adopted}


def instantiate_objectives(
    mission_id: str,
    rows: list[dict],
    *,
    expect_binding: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> list[dict]:
    """Write a mixed playbook/model objective batch in ONE transaction (#883).

    **This is the trusted boundary, and it exists because authority is per ROW.**
    `patch_objectives` takes one `source` for a whole batch, and the tempting fix — widening it
    to accept a per-row source — would put authority in the caller's hands, which is the shape
    of every privilege bug. So instantiation is its own path: each row's `source` is assigned
    HERE, from where the row came from, and the public `PATCH /objectives` route keeps
    `source="operator"` with no way to say otherwise.

    Atomic on purpose. A rejected batch leaves the mission with the objective list it had, because
    a half-instantiated plan the operator cannot tell is partial is worse than no plan at all.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock, _playbook_policy_held(mission_id, expect_binding, path=path):
        # THE REVOCATION FENCE, and it is a real fence rather than a narrow window (#883 review).
        #
        # An earlier version checked the digest and THEN took the write lock, which is
        # check-then-write across two stores: a revocation could commit under the prefs flock
        # after the equality check and before the insert, and the revoked target was persisted
        # anyway. Reproduced by forcing that ordering.
        #
        # `_playbook_policy_held` holds the PREFS write lock across the check AND this
        # transaction, so a concurrent `set_mission_playbooks` blocks until the mission write
        # finishes. The revocation therefore lands strictly before the check (and it fails) or
        # strictly after the commit (and it is a revocation of something already written, which
        # is what revocation means). There is no in-between left.
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            state_row = con.execute(
                "SELECT state FROM missions WHERE id=?", (mission_id,)
            ).fetchone()
            if state_row is None:
                raise MissionNotFound(mission_id)
            # THE LIFECYCLE FENCE (#883 review). A proposal spans a model call, and the mission
            # can reach a terminal state inside that window. Inserting then leaves a `done`
            # mission carrying a fresh PENDING gating objective — a checklist that contradicts
            # the mission's own outcome, and one `_finish_objective_write` cannot repair, because
            # it reopens `review` and nothing else.
            #
            # Refused rather than reopened: a mission the operator closed is not something a late
            # background task gets to reopen on their behalf.
            if state_row["state"] in TERMINAL_STATES:
                raise MissionError(
                    f"mission {mission_id} is {state_row['state']}; objectives are not "
                    f"instantiated into a closed mission",
                    status=409,
                )
            added_unmet_gate = False
            applied: list[dict] = []
            for row in rows:
                if not isinstance(row, dict):
                    raise MissionError("each objective must be an object", status=422)
                src = row.get("source")
                if src not in ("playbook", "model"):
                    # Not a client-facing message: reaching it means a caller inside this module
                    # tried to mint an authority it does not have.
                    raise MissionError(f"instantiation cannot write source {src!r}", status=500)
                added_unmet_gate |= _op_add(con, mission_id, row, src, ts)
                applied.append({"op": "add", "key": row.get("key"), "source": src})
            # `by` names the BOUNDARY, not a source, because this batch is deliberately mixed:
            # per-row authority is the whole reason this path exists, so a single `by` naming one
            # source would be a lie about half the rows. Each row carries its own in `applied`.
            _finish_objective_write(
                con,
                mission_id,
                by="instantiation",
                applied=applied,
                added_unmet_gate=added_unmet_gate,
                prior_state=state_row["state"],
                ts=ts,
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return objectives(mission_id, path=path)


def objectives(mission_id: str, *, path: Path | None = None) -> list[dict]:
    validate_id(mission_id)
    con = _ready(path)
    try:
        # An unknown mission is a 404, not an empty list: "this mission has no objectives" and
        # "this mission does not exist" are different answers and the client acts on them
        # differently.
        if con.execute("SELECT 1 FROM missions WHERE id=?", (mission_id,)).fetchone() is None:
            raise MissionNotFound(mission_id)
        rows = con.execute(
            "SELECT * FROM mission_objectives WHERE mission_id=? ORDER BY ord ASC",
            (mission_id,),
        ).fetchall()
        return [_objective_row(r) for r in rows]
    finally:
        con.close()


def _validate_probe(probe: object, probe_args: object, gate: bool) -> tuple[str, str | None]:
    kind = probe if isinstance(probe, str) else ""
    if kind not in PROBE_KINDS:
        raise MissionError(f"unknown probe {probe!r}", status=422)
    if gate and kind in NON_GATING_PROBES:
        # "the agent believes it wrote tests" is not evidence that it did.
        raise MissionError(f"probe {kind} may not be a gate", status=422)
    if probe_args is not None and not isinstance(probe_args, dict):
        raise MissionError("probe_args must be an object", status=422)
    _validate_probe_args(kind, probe_args)
    # A SERIALIZATION backstop, and — since the per-field value contracts landed — no longer
    # reachable for any known kind: every field is length-capped, and the widest schema-valid set
    # serializes to well under `PROBE_ARGS_MAX`. It stays because it guards the COLUMN rather than
    # the fields: a kind added to `PROBE_ARG_SCHEMA` with generous caps of its own would otherwise
    # discover this budget by writing invalid truncated JSON that reads back as None. Kept
    # deliberately rather than left by accident, and pinned directly on `_json_or_none` — asserting
    # it through `_validate_probe` is what a dead branch looks like when nobody says so.
    return kind, _json_or_none(probe_args, PROBE_ARGS_MAX)


def validate_probe_args(kind: str, probe_args: object) -> None:
    """Check one probe's arguments against :data:`PROBE_ARG_SCHEMA`. Raises, never repairs.

    **The one shared validator**, called by every path that makes a probe durable: the public
    `PATCH /objectives` route, `instantiate_objectives`, and — through `prefs` — playbook writes
    and read-time normalization. `prefs` used to re-implement the name half of this inline, which
    is how the two paths came to disagree about values while agreeing about names.

    **Unknown keys are rejected rather than dropped.** Silently ignoring one turns a typo into a
    probe that checks something other than what was written, and leaves a future field looking
    accepted while it is discarded. Rejecting makes the operator's mistake visible at the moment
    they make it, which is the only moment they can fix it cheaply.

    **Values are checked, not just names.** A name-only schema accepted a `url` that was a list
    and an `expect_status` that was an object; both persisted, and both would have reached the
    Phase 5 runner as a target it cannot probe.
    """
    spec = PROBE_ARG_SCHEMA.get(kind)
    if spec is None:
        raise MissionError(f"unknown probe {kind!r}", status=422)
    if probe_args is None:
        got: dict = {}
    elif isinstance(probe_args, dict):
        got = probe_args
    else:
        raise MissionError("probe_args must be an object", status=422)
    missing = {n for n, (req, _) in spec.items() if req} - set(got)
    if missing:
        raise MissionError(f"probe {kind} requires {', '.join(sorted(missing))}", status=422)
    unknown = set(got) - set(spec)
    if unknown:
        raise MissionError(f"probe {kind} does not take {', '.join(sorted(unknown))}", status=422)
    for name, value in got.items():
        spec[name][1](kind, name, value)


#: The private spelling stays as an alias so nothing internal has to change name to gain the
#: value checks; the public one is what `prefs` imports.
_validate_probe_args = validate_probe_args


def patch_objectives(
    mission_id: str,
    ops: list[dict],
    *,
    source: str = "operator",
    now: float | None = None,
    path: Path | None = None,
) -> list[dict]:
    """Apply operator edits — ``add`` / ``drop`` / ``retitle`` / ``waive`` / ``reorder``.

    Three rules make edits honest, and each one is a test:

    * **An edit never retroactively marks an objective met.** ``state`` / ``met_at`` / ``observed``
      are not writable on this path at all; the only state an operator may set is ``waived``,
      which is a decision not to require the objective, not a claim that it holds.
    * **Adding an unmet gating objective to a mission in ``review`` moves it back to ``running``**
      — in the same transaction as the insert, so the list and the state can never disagree.
    * ``agent_judged`` may not gate, rejected at write time.

    **…and a mission being DISPATCHED is closed to edits entirely** (#904 review 5, finding 2).
    `claim_plan` compares the approved checklist inside its own transaction, and that transaction
    ends before the spawn — so an edit landing in the window between them started an unattended
    agent against a checklist other than the one the operator approved, with the comparison having
    passed. A digest cannot fence a window it has already left; the state can. `dispatching` is
    short, bounded by the launch, and settled by recovery if the process dies, so refusing here
    costs the operator a retry seconds later and closes the window completely.
    """
    validate_id(mission_id)
    source = _require_str(source, "source")
    if source not in OBJECTIVE_SOURCES:
        raise MissionError(f"unknown source {source!r}", status=422)
    if not isinstance(ops, list) or not ops:
        raise MissionError("ops (a non-empty list) is required", status=422)
    ts = time.time() if now is None else now
    # RESET copies the playbook's CURRENT direction (#983), so the templates are resolved once, up
    # front, and pinned for the write the way instantiation pins them: `_playbook_policy_held`
    # holds the prefs lock and refuses if the playbook moved in between. Only when a reset is asked
    # for — nothing else here reads a playbook, and a global lock for it would be for nothing.
    resets = any(isinstance(op, dict) and op.get("op") == "reset_direction" for op in ops)
    templates: dict[str, dict] = {}
    binding: str | None = None
    if resets:
        status, tlist, binding = templates_and_binding(mission_id, path=path)
        if status == "ok":
            templates = {str(t.get("key")): t for t in tlist}
    with _write_lock, _playbook_policy_held(mission_id, binding, path=path):
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            _fence_busy(con, mission_id)
            state_row = con.execute(
                "SELECT state FROM missions WHERE id=?", (mission_id,)
            ).fetchone()
            if state_row is None:
                raise MissionNotFound(mission_id)
            if str(state_row["state"] or "") == "dispatching":
                con.execute("ROLLBACK")
                raise MissionError(
                    "this mission is being dispatched; what done means is fixed until the agent "
                    "has started",
                    status=409,
                )
            added_unmet_gate = False
            applied: list[dict] = []
            for op in ops:
                if not isinstance(op, dict):
                    raise MissionError("each op must be an object", status=422)
                kind = op.get("op")
                _check_op_fields(op)
                if kind == "add":
                    added_unmet_gate |= _op_add(con, mission_id, op, source, ts)
                elif kind == "drop":
                    _op_drop(con, mission_id, op)
                elif kind == "retitle":
                    _op_retitle(con, mission_id, op)
                elif kind == "waive":
                    _op_waive(con, mission_id, op, ts)
                elif kind == "reorder":
                    _op_reorder(con, mission_id, op)
                elif kind == "set_direction":
                    _op_set_direction(con, mission_id, op, source)
                elif kind == "reset_direction":
                    _op_reset_direction(con, mission_id, op, source, templates)
                elif kind == "clear_direction":
                    _op_clear_direction(con, mission_id, op)
                else:
                    raise MissionError(f"unknown objective op {kind!r}", status=422)
                # The op and key only — never the direction's text, which the row already holds.
                applied.append({"op": kind, "key": op.get("key")})
            _finish_objective_write(
                con,
                mission_id,
                by=source,
                applied=applied,
                added_unmet_gate=added_unmet_gate,
                prior_state=state_row["state"],
                ts=ts,
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return objectives(mission_id, path=path)


def _validate_direction(direction: object, probe: str) -> str | None:
    """`mission_directions.validate`, as a 422 on this path (#983)."""
    from . import mission_directions

    try:
        return mission_directions.validate(direction, probe)
    except mission_directions.DirectionError as e:
        raise MissionError(str(e), status=422) from None


def _direction_target(con, mission_id: str, op: dict, source: str) -> tuple[str, str]:
    """`(key, probe)` of the objective a direction op names. Only the operator writes directions."""
    if source != "operator":
        # A direction is typed into a session. The route passes `operator`; any other caller
        # reaching this is minting an authority it does not have.
        raise MissionError("only the operator may write an objective's direction", status=403)
    key = _cap(op.get("key"), OBJECTIVE_KEY_MAX)
    row = con.execute(
        "SELECT probe FROM mission_objectives WHERE mission_id=? AND key=?", (mission_id, key)
    ).fetchone()
    if row is None:
        raise MissionError(f"unknown objective {key}", status=404)
    return key, str(row["probe"] or "")


def _op_set_direction(con, mission_id: str, op: dict, source: str) -> None:
    """The operator writes this mission's own direction for one objective."""
    key, probe = _direction_target(con, mission_id, op, source)
    direction = _validate_direction(op.get("direction"), probe)
    if direction is None:
        raise MissionError("a direction needs text; use clear_direction to remove one", status=422)
    con.execute(
        "UPDATE mission_objectives SET direction=?, direction_source='operator' "
        "WHERE mission_id=? AND key=?",
        (direction, mission_id, key),
    )


def _op_reset_direction(
    con, mission_id: str, op: dict, source: str, templates: dict[str, dict]
) -> None:
    """COPY the playbook's CURRENT direction for this objective — again, and still not a link.

    The template is the one with the same key in the mission's playbook, resolved before the
    transaction and pinned by `_playbook_policy_held`. A template that no longer exists, or that now
    checks something different from this objective, has nothing to reset to and is refused rather
    than guessed at.
    """
    key, probe = _direction_target(con, mission_id, op, source)
    t = templates.get(key)
    if t is None:
        raise MissionError(
            f"the mission's playbook has no objective {key} to reset the direction from",
            status=409,
        )
    if str(t.get("probe") or "none") != probe:
        raise MissionError(
            f"the playbook's objective {key} now checks something different; nothing was reset",
            status=409,
        )
    direction = _validate_direction(t.get("direction"), probe)
    con.execute(
        "UPDATE mission_objectives SET direction=?, direction_source=? "
        "WHERE mission_id=? AND key=?",
        (direction, None if direction is None else "template", mission_id, key),
    )


def _op_clear_direction(con, mission_id: str, op: dict) -> None:
    """No direction: a supervisor nudge for this objective types the global nudge again."""
    key = _cap(op.get("key"), OBJECTIVE_KEY_MAX)
    cur = con.execute(
        "UPDATE mission_objectives SET direction=NULL, direction_source=NULL "
        "WHERE mission_id=? AND key=?",
        (mission_id, key),
    )
    if not cur.rowcount:
        raise MissionError(f"unknown objective {key}", status=404)


#: What an operator edit may name. Everything else is refused rather than silently dropped —
#: `state` / `met_at` / `observed` in particular, so "an edit never marks an objective met" is an
#: answer the caller GETS rather than a field that quietly did nothing.
_OP_FIELDS: dict[str, frozenset[str]] = {
    "add": frozenset({"op", "key", "title", "gate", "probe", "probe_args", "direction"}),
    "drop": frozenset({"op", "key"}),
    "retitle": frozenset({"op", "key", "title"}),
    "waive": frozenset({"op", "key"}),
    "reorder": frozenset({"op", "keys"}),
    # #983. Write this mission's own direction, copy the playbook's current one, or remove it.
    "set_direction": frozenset({"op", "key", "direction"}),
    "reset_direction": frozenset({"op", "key"}),
    "clear_direction": frozenset({"op", "key"}),
}
#: Named separately so the refusal can say *why* rather than "unknown field".
_SETTLED_BY_OBSERVATION = frozenset({"state", "met_at", "observed"})


def _check_op_fields(op: dict) -> None:
    kind = op.get("op")
    allowed = _OP_FIELDS.get(kind, frozenset())
    extra = set(op) - allowed
    settled = extra & _SETTLED_BY_OBSERVATION
    if settled:
        raise MissionError(
            f"{', '.join(sorted(settled))} is set from a server observation, never from an edit",
            status=422,
        )
    if extra:
        raise MissionError(f"unknown field(s) for {kind}: {', '.join(sorted(extra))}", status=422)


def _op_add(con, mission_id: str, op: dict, source: str, ts: float) -> bool:
    """Insert one objective. Returns True iff it is an unmet **gate** (which can reopen review)."""
    count = con.execute(
        "SELECT COUNT(*) FROM mission_objectives WHERE mission_id=?", (mission_id,)
    ).fetchone()[0]
    if int(count) >= OBJECTIVES_MAX:
        raise MissionError(f"a mission may hold at most {OBJECTIVES_MAX} objectives", status=422)
    key = _cap(op.get("key"), OBJECTIVE_KEY_MAX)
    if not key or not re.match(r"^[a-z0-9][a-z0-9_-]*$", key):
        raise MissionError("objective key must be a lowercase slug", status=422)
    title = _cap(op.get("title"), OBJECTIVE_TITLE_MAX)
    if not title:
        raise MissionError("objective title is required", status=422)
    # NO CONTROL CHARACTERS (#904 review 10, finding 3). The digest that carries "the checklist
    # you approved" is length-prefixed and no longer relies on these being absent — this is the
    # belt-and-braces half. An objective title is one line the operator reads on a card; a C0
    # control in it renders as nothing and exists only to be confusing.
    if any(ch < " " or ch == "\x7f" for ch in title):
        raise MissionError("objective title may not contain control characters", status=422)
    gate = strict_bool(op.get("gate"), "gate", default=False)
    # AUTHORITY BY SOURCE (#883). A `model` row is a NOTE: it may name an objective and nothing
    # else. `probe` and `probe_args` are operator-authored — from a playbook the operator wrote,
    # or from their own edit — and a model row carrying either would launder model text into an
    # operator-authored field, which is the SSRF surface this phase closes by construction.
    #
    # Checked here rather than at the caller, so a future caller fails loudly instead of quietly
    # widening it. It is belt-and-braces: `instantiate_objectives` cannot produce such a row.
    if source == "model" and (
        op.get("probe", "none") not in (None, "none") or op.get("probe_args")
    ):
        raise MissionError("a model-proposed objective may not carry a probe", status=422)
    # …and the same for a DIRECTION (#983): it is typed into a session, so it is operator text —
    # from a playbook template or the operator's own edit — and a model row carrying one is refused.
    if source == "model" and op.get("direction") is not None:
        raise MissionError("a model-proposed objective may not carry a direction", status=422)
    probe, args = _validate_probe(op.get("probe", "none"), op.get("probe_args"), gate)
    direction = _validate_direction(op.get("direction"), probe)
    nxt = con.execute(
        "SELECT COALESCE(MAX(ord), -1) + 1 FROM mission_objectives WHERE mission_id=?",
        (mission_id,),
    ).fetchone()[0]
    try:
        con.execute(
            "INSERT INTO mission_objectives "
            "(mission_id, key, ord, title, probe, probe_args, gate, state, source, incarnation, "
            "direction, direction_source) "
            "VALUES (?,?,?,?,?,?,?, 'pending', ?, ?, ?, ?)",
            (
                mission_id,
                key,
                int(nxt),
                title,
                probe,
                args,
                1 if gate else 0,
                source,
                # A FRESH ONE, every time. Re-adding a dropped key is a new objective wearing an
                # old name, and this is what says so to anything holding a reference.
                uuid.uuid4().hex,
                direction,
                None if direction is None else ("template" if source == "playbook" else "operator"),
            ),
        )
    except sqlite3.IntegrityError:
        raise MissionError(f"objective {key} already exists", status=409) from None
    return gate


def _op_drop(con, mission_id: str, op: dict) -> None:
    key = _cap(op.get("key"), OBJECTIVE_KEY_MAX)
    cur = con.execute(
        "DELETE FROM mission_objectives WHERE mission_id=? AND key=?", (mission_id, key)
    )
    if not cur.rowcount:
        raise MissionError(f"unknown objective {key}", status=404)
    # The supervisor's lifecycle rows key on `(mission_id, objective_key)` and are NOT reachable by
    # the FK cascade, which only follows `missions(id)`. Left behind, a re-added key inherits the
    # dead objective's episode, its stand-down and its spend — so a fresh objective could arrive
    # already silenced with no budget, for a reason no longer visible anywhere. Same transaction as
    # the delete: a half-applied drop is what would make the two disagree.
    _forget_objective(con, mission_id, key)


def _objective_is_current(con, mission_id: str, objective_key: str, episode: int) -> bool:
    """Does this objective exist, and is `episode` its current one? Call INSIDE a transaction.

    Supervisor lifecycle rows key on `(mission_id, objective_key)` and are cleaned by
    `_forget_objective` when the objective is dropped. Without this check an in-flight pass could
    insert one back afterwards — resurrecting a binding or an escalation for an objective that no
    longer exists, and handing a later re-add of the same key an inherited episode and an instant
    `needs_you` (#888 review, finding 2). Validating in the same transaction as the insert is what
    makes the drop's cleanup final.
    """
    obj = con.execute(
        "SELECT state FROM mission_objectives WHERE mission_id=? AND key=?",
        (mission_id, objective_key),
    ).fetchone()
    if obj is None:
        return False
    row = con.execute(
        "SELECT episode, stood_down, question_seq FROM mission_objective_episode "
        "WHERE mission_id=? AND objective_key=?",
        (mission_id, objective_key),
    ).fetchone()
    current = int(row["episode"]) if row else 1
    if int(episode) != current:
        return False
    # AN OPEN QUESTION IS A HOLD, and it belongs in the RESERVATION, not only in the pass that
    # decides to nudge (#900 review, finding 1). `may_nudge` reads the hold, but a question opened
    # between that read and this insert would otherwise still get a reservation — and the
    # supervisor would type into a session while it is on screen asking the operator what to do.
    # Checking it in the same transaction as the insert is what leaves the race no window.
    if row is not None and int(row["question_seq"] or 0):
        return False
    # STOOD DOWN is part of "current authority", not a separate question. `escalate()` reads the
    # episode, the operator's stand-down commits, and the stale pass then announces after they
    # explicitly asked for silence — the exact thing the feature exists to prevent. Checking it
    # here puts it in the same transaction as the insert, where the race cannot get between them.
    if row is not None and int(row["stood_down"] or 0):
        return False
    # …and an objective that has already settled is not a legitimate target for a new lifecycle
    # row either: there is nothing left to nudge toward or escalate about.
    return str(obj["state"] or "") not in ("met", "waived")


def _forget_objective(con, mission_id: str, key: str) -> None:
    """Erase every supervisor row bound to one objective identity.

    Deliberately NOT a foreign key: `mission_objectives` is edited by ordinary operator ops, and a
    cascade there would silently delete audit rows (the escalations) as a side effect of a retitle
    refactor. Dropping the objective is the one transition where forgetting is correct, so it is
    spelled once, here, and called from exactly that place.
    """
    # Spelled out rather than looped over an interpolated table name: the three statements are the
    # complete, greppable inventory of what is bound to an objective identity, so a table added
    # later shows up as a missing line here instead of hiding behind a loop variable.
    con.execute(
        "DELETE FROM mission_objective_episode WHERE mission_id=? AND objective_key=?",
        (mission_id, key),
    )
    con.execute(
        "DELETE FROM mission_supervisor_actions WHERE mission_id=? AND objective_key=?",
        (mission_id, key),
    )
    con.execute(
        "DELETE FROM mission_escalations WHERE mission_id=? AND objective_key=?",
        (mission_id, key),
    )
    # …and the autonomous-AI-direction reservation (#983 P4). Keying it by incarnation already
    # makes a leftover row unmatchable, so this is housekeeping rather than the fix — but this
    # function documents itself as the complete inventory of what is bound to an objective
    # identity, and a table missing from it is exactly what that claim exists to prevent.
    con.execute(
        "DELETE FROM mission_ai_directions WHERE mission_id=? AND objective_key=?",
        (mission_id, key),
    )


def _op_retitle(con, mission_id: str, op: dict) -> None:
    key = _cap(op.get("key"), OBJECTIVE_KEY_MAX)
    title = _cap(op.get("title"), OBJECTIVE_TITLE_MAX)
    if not title:
        raise MissionError("objective title is required", status=422)
    cur = con.execute(
        "UPDATE mission_objectives SET title=? WHERE mission_id=? AND key=?",
        (title, mission_id, key),
    )
    if not cur.rowcount:
        raise MissionError(f"unknown objective {key}", status=404)


def _op_waive(con, mission_id: str, op: dict, ts: float) -> None:
    """Waive = "I have decided this is not required", which is NOT "this holds".

    ``met_at`` and ``observed`` stay untouched precisely so a waived objective can never be read
    back as one a probe settled.
    """
    key = _cap(op.get("key"), OBJECTIVE_KEY_MAX)
    cur = con.execute(
        "UPDATE mission_objectives SET state='waived' WHERE mission_id=? AND key=? "
        "AND state != 'met'",
        (mission_id, key),
    )
    if not cur.rowcount:
        row = con.execute(
            "SELECT state FROM mission_objectives WHERE mission_id=? AND key=?",
            (mission_id, key),
        ).fetchone()
        if row is None:
            raise MissionError(f"unknown objective {key}", status=404)
        raise MissionError(f"objective {key} is already met", status=409)
    # THE OBJECTIVE'S OWN STATE CHANGED, so this is the reset boundary the episode table exists to
    # mark. Without it `bump_episode` had no production caller at all: a waived objective kept the
    # episode that its earlier nudges were charged to, so its spend and — worse — a stand-down from
    # the previous episode survived a transition that was supposed to end them. Same transaction as
    # the waive: an episode that advanced without the waive, or a waive without the advance, is the
    # inconsistency this pairing rules out.
    _bump_episode_con(con, mission_id, key, ts)


def _op_reorder(con, mission_id: str, op: dict) -> None:
    keys = op.get("keys")
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        raise MissionError("reorder needs keys (a list of strings)", status=422)
    have = {
        r["key"]
        for r in con.execute(
            "SELECT key FROM mission_objectives WHERE mission_id=?", (mission_id,)
        ).fetchall()
    }
    # Set equality alone is not "exactly once": ['a','b','b'] on {'a','b'} passes it and leaves a
    # gapped order. Cardinality is the half that catches a duplicate.
    if len(keys) != len(set(keys)) or set(keys) != have:
        raise MissionError("reorder must list every objective exactly once", status=422)
    # Two passes through a disjoint range: `ord` has no UNIQUE constraint today, but a one-pass
    # renumber would still transiently collide if one ever gains it.
    for i, key in enumerate(keys):
        con.execute(
            "UPDATE mission_objectives SET ord=? WHERE mission_id=? AND key=?",
            (i + 10_000, mission_id, key),
        )
    for i, key in enumerate(keys):
        con.execute(
            "UPDATE mission_objectives SET ord=? WHERE mission_id=? AND key=?",
            (i, mission_id, key),
        )


def unmet_gates(mission_id: str, *, path: Path | None = None) -> list[str]:
    """Gating objectives that do not hold. ``likely_done`` may not even be proposed while this
    is non-empty (Phase 5) — stated here so the rule lives with the data it reads."""
    return [
        o["key"]
        for o in objectives(mission_id, path=path)
        if o["gate"] and o["state"] in _UNMET_OBJECTIVE_STATES
    ]


# ---------------------------------------------------------------- settlement projection


#: Bounds on the projection. It is a *summary* of a ledger row, not a copy of it.
#: The projection's ``state`` when the ledger row was already gone when the reference was made.
#: Deliberately not a ledger state — it is the honest answer to "what did this decision say?" when
#: the answer no longer exists, and it maps onto §16's "absent from the ledger ⇒ historical, no
#: controls, no outcome asserted" rather than rendering an empty decision forever.
SETTLEMENT_LOST = "source_compacted"

SETTLEMENT_RATIONALE_MAX = 600
SETTLEMENT_OUTCOME_MAX = 300


def record_settlement(action_id: str, record: dict, *, path: Path | None = None) -> int:
    """Freeze a bounded projection of a settled ledger action. Returns rows written (0 or 1).

    **Why this exists.** The ledger compacts terminal actions down to a globally bounded tail
    (``HISTORY_MAX``), which is right for a feed and wrong for a mission that outlives it: a
    six-week-old mission would keep its ``approval`` event while the row carrying the verb,
    rationale and outcome had already been compacted away, and the timeline would render a
    decision with no content.

    **Written once, never updated** — ``INSERT OR IGNORE`` on the ``action_id`` primary key. The
    ledger stays authoritative while its row exists; this is what keeps the mission readable after
    it does not.

    **In its own table, owned by no mission.** On the event row it was hostage to the event cap:
    bounding the timeline deleted the very durability promise the cap was protecting. Here the
    feed can be bounded freely.

    Called from ``orchestrator_ledger._settled`` (per-settlement) and from
    ``orchestrator_ledger.compact`` (for everything about to fall out of the tail), under that
    call site's two documented rules: after the durable append and **outside** the ledger lock,
    and best-effort. Hence the short busy timeout — this runs inside somebody else's transition
    and may not park a thread for five seconds on our account.
    """
    if not isinstance(action_id, str) or not action_id:
        return 0
    with _write_lock:
        con = _ready(path, busy_timeout_ms=_SETTLEMENT_BUSY_TIMEOUT_MS)
        try:
            # ONLY for an action a mission actually references. The hook fires on every ledger
            # settlement in the app, most of which belong to no mission at all — copying those in
            # put bounded model text (`rationale`, `outcome`) into this store with no mission to
            # own it, no path that could ever name it for collection, and no retention window
            # over it. Referenced-only means an orphan cannot be created in the first place.
            if not _is_referenced(con, action_id):
                return 0
            # Written once — except over a `source_compacted` marker, which is precisely the
            # "we could not find out" state. If the truth turns up later it must win; anything
            # else stays immutable.
            con.execute(
                "DELETE FROM mission_settlements WHERE action_id=? AND state=?",
                (action_id, SETTLEMENT_LOST),
            )
            return (
                con.execute(
                    "INSERT OR IGNORE INTO mission_settlements "
                    "(action_id, verb, state, rationale, outcome, at) VALUES (?,?,?,?,?,?)",
                    (
                        action_id,
                        _cap(record.get("verb"), 40) or None,
                        _cap(record.get("state"), 40) or None,
                        _cap(record.get("rationale"), SETTLEMENT_RATIONALE_MAX) or None,
                        _cap(record.get("outcome") or record.get("detail"), SETTLEMENT_OUTCOME_MAX)
                        or None,
                        float(record.get("ts") or time.time()),
                    ),
                ).rowcount
                or 0
            )
        finally:
            con.close()


def _is_referenced(con, action_id: str) -> bool:
    return (
        con.execute(
            "SELECT 1 FROM mission_events WHERE action_id=? LIMIT 1", (action_id,)
        ).fetchone()
        is not None
    )


def referenced_action_ids(action_ids: list[str], *, path: Path | None = None) -> set[str]:
    """Which of these actions a mission timeline points at.

    The ledger asks this before compacting, so it projects exactly the rows a mission would
    otherwise be left unable to render — and nothing else.
    """
    ids = [a for a in dict.fromkeys(action_ids) if a]
    if not ids:
        return set()
    # The SHORT wait, like every other step on this path: the ledger calls this while holding its
    # own lock, so a five-second block here would stall every orchestrator append behind it.
    con = _ready(path, busy_timeout_ms=_SETTLEMENT_BUSY_TIMEOUT_MS)
    try:
        sql = (  # noqa: S608
            "SELECT DISTINCT action_id FROM mission_events WHERE action_id IN ({m})"
        ).format(m=",".join("?" * len(ids)))
        return {r["action_id"] for r in con.execute(sql, tuple(ids)).fetchall()}
    finally:
        con.close()


def record_settlements(records: list[dict], *, path: Path | None = None) -> int:
    """Freeze many projections in one hold — the pre-compaction pass.

    The ledger is about to drop these rows, which is the one moment at which "this is about to
    become unavailable" is knowable. Doing it per-row through :func:`record_settlement` would take
    and drop the lock once per action for no reason.
    """
    rows = [r for r in records if isinstance(r.get("id"), str) and r["id"]]
    if not rows:
        return 0
    with _write_lock:
        con = _ready(path, busy_timeout_ms=_SETTLEMENT_BUSY_TIMEOUT_MS)
        try:
            con.execute("BEGIN IMMEDIATE")
            written = 0
            for r in rows:
                if not _is_referenced(con, r["id"]):
                    continue  # never create an orphan — see `record_settlement`
                # Replace a `source_compacted` marker, exactly as the single-row writer does.
                # Without this the bulk pass counted the marker as a projection and compaction
                # went on to delete the row that could still have told the truth.
                con.execute(
                    "DELETE FROM mission_settlements WHERE action_id=? AND state=?",
                    (r["id"], SETTLEMENT_LOST),
                )
                written += (
                    con.execute(
                        "INSERT OR IGNORE INTO mission_settlements "
                        "(action_id, verb, state, rationale, outcome, at) VALUES (?,?,?,?,?,?)",
                        (
                            r["id"],
                            _cap(r.get("verb"), 40) or None,
                            _cap(r.get("state"), 40) or None,
                            _cap(r.get("rationale"), SETTLEMENT_RATIONALE_MAX) or None,
                            _cap(r.get("outcome") or r.get("detail"), SETTLEMENT_OUTCOME_MAX)
                            or None,
                            float(r.get("ts") or time.time()),
                        ),
                    ).rowcount
                    or 0
                )
            con.execute("COMMIT")
            return written
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def settlements_for(action_ids: list[str], *, path: Path | None = None) -> dict[str, dict]:
    """The frozen projections for these actions, by id."""
    ids = [a for a in dict.fromkeys(action_ids) if a]
    if not ids:
        return {}
    con = _ready(path)
    try:
        sql = (  # noqa: S608
            "SELECT * FROM mission_settlements WHERE action_id IN ({placeholders})"
        ).format(placeholders=",".join("?" * len(ids)))
        return {r["action_id"]: dict(r) for r in con.execute(sql, tuple(ids)).fetchall()}
    finally:
        con.close()


def _gc_settlements(con, action_ids: list[str]) -> int:
    """Drop projections nothing references any more.

    ``rationale`` is bounded model text about the operator's work, so it is deleted with the
    mission like everything else here — the normalized table must not become the one place
    sensitive text outlives its mission. Scoped to the ids just orphaned, so this never becomes a
    table scan on an ordinary append.
    """
    ids = [a for a in dict.fromkeys(action_ids) if a]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    sql = (
        f"DELETE FROM mission_settlements WHERE action_id IN ({marks}) AND action_id NOT IN ("  # noqa: S608
        "  SELECT action_id FROM mission_events WHERE action_id IS NOT NULL)"
    )
    return con.execute(sql, tuple(ids)).rowcount or 0


# ---------------------------------------------------------------- derived attention


def derive_needs_you(mission_ids: list[str], *, path: Path | None = None) -> dict[str, dict]:
    """Compute the attention flag per mission — **at read time, never stored**.

    Storing it would create a second source of truth that goes stale exactly when it matters:
    the operator decides an action in the bell, and the rail keeps saying "needs you" because
    nothing told it. Three terms, ORed:

    1. a live ledger action in ``OPERATOR_PENDING_STATES`` on one of the mission's ACTIVE
       sessions — the ledger is what states that an action is pending, never the model;
    2. ``intervention_required`` on one of those sessions, read from the metadata sidecar, which
       is where ``pulse.build_cards`` reads the card's own flag from (identical source, and no
       ``scan_all()`` on a list request);
    3. an **open question** — a ``question`` event with no later ``answer`` on the mission. The
       producer landed in #892: the supervisor asks after a terminal escalation, and because
       `open_question` stands the objective down, term 4 below drops for that objective in the
       same moment — so the answerable reason REPLACES the vague one rather than stacking on it.

    Best-effort per term: a ledger or sidecar hiccup degrades the flag, it never fails the list.
    """
    out: dict[str, dict] = {m: {"needs_you": False, "why": []} for m in mission_ids}
    if not mission_ids:
        return out

    con = _ready(path)
    try:
        rows, open_q, escalated = _attention_rows(con, mission_ids)
    finally:
        con.close()
    return _attention_merge(out, mission_ids, rows, open_q, escalated)


def _attention_rows(con, mission_ids: list[str]):
    """The three STORE terms of the attention flag, from one connection (#900 rev 7, finding 4).

    Split out so the mission-detail read can take them and the open question from the SAME
    snapshot. Two connections answered a torn state — `needs_you: ["question"]` beside
    `question: null`, or the reverse — which is the one disagreement this whole phase exists to
    prevent: a mission flagged for an answer with nothing on screen to answer.
    """
    placeholders = ",".join("?" for _ in mission_ids)
    rows = con.execute(
        # noqa justification: `placeholders` is a run of `?` sized by len(mission_ids); every
        # id is BOUND, never interpolated. Same for the grouped query below.
        f"SELECT mission_id, session_key FROM mission_sessions "  # noqa: S608
        f"WHERE mission_id IN ({placeholders}) AND removed_at IS NULL",
        tuple(mission_ids),
    ).fetchall()
    # AN OPEN QUESTION IS A HOLD, not a comparison of two MAX(seq) values. The first version
    # asked whether the mission's newest `question` was newer than its newest `answer`, which
    # cannot express a question on one objective while another is answered, a superseded
    # question, or a late answer to an older one (#892 issue review). The hold names the
    # question that is actually waiting, per objective.
    open_q = con.execute(
        f"SELECT DISTINCT mission_id FROM mission_objective_episode "  # noqa: S608
        f"WHERE mission_id IN ({placeholders}) AND question_seq IS NOT NULL",
        tuple(mission_ids),
    ).fetchall()
    # ESCALATIONS ARE AN ATTENTION SOURCE. The supervisor's terminal "this needs you" is
    # durable and arbitrated, and it was reaching the timeline and the bell while
    # `needs_you` stayed false — so the console's own "waiting on you" filter, and the
    # supervisor's own gate against nudging a mission that needs its operator, both looked
    # straight past it. An escalation IS the mission needing the operator; that is what the
    # word means (#888 review, finding 5).
    # CURRENT, UNRESOLVED escalations only. Selecting any historical row meant a mission
    # stayed "needs you" forever: waiving the objective advanced the episode and the old row
    # kept the flag set, and "Stop telling me" — the whole point of a stand-down — did not
    # quiet it either (#888 review, finding 3). The rows stay as history; attention is a claim
    # about NOW, so it joins the objective's live state, its current episode and its
    # stand-down. An escalation for an objective that has since been dropped resolves too,
    # because the inner join finds nothing.
    escalated = con.execute(
        f"SELECT DISTINCT e.mission_id AS mission_id FROM mission_escalations e "  # noqa: S608
        f"JOIN mission_objectives o "
        f"  ON o.mission_id = e.mission_id AND o.key = e.objective_key "
        f"LEFT JOIN mission_objective_episode ep "
        f"  ON ep.mission_id = e.mission_id AND ep.objective_key = e.objective_key "
        f"WHERE e.mission_id IN ({placeholders}) "
        f"  AND o.state NOT IN ('met','waived') "
        f"  AND e.episode = COALESCE(ep.episode, 1) "
        f"  AND COALESCE(ep.stood_down, 0) = 0 "
        # …AND NOT WHILE A QUESTION IS HOLDING IT (#892). The supervisor escalates and then
        # asks about the same objective, and the question SUPERSEDES the escalation: one says
        # something is wrong, the other says what would fix it, and showing both would name
        # the same situation twice with only one of them answerable.
        f"  AND ep.question_seq IS NULL",
        tuple(mission_ids),
    ).fetchall()
    return rows, open_q, escalated


def _attention_merge(out, mission_ids, rows, open_q, escalated):
    """The LEDGER and SIDECAR terms, folded onto the store's. Both are outside the store, so they
    are read after the snapshot and degrade rather than fail — a hiccup dims the flag, it never
    fails the list."""
    from . import metadata, orchestrator_ledger

    by_mission: dict[str, list[str]] = {}
    for r in rows:
        by_mission.setdefault(r["mission_id"], []).append(r["session_key"])

    pending: set[str] = set()
    try:
        for a in orchestrator_ledger.live_actions():
            if a.get("state") in orchestrator_ledger.OPERATOR_PENDING_STATES:
                sid = str(a.get("session_id") or "")
                if sid:
                    pending.add(sid)
    except Exception:  # noqa: BLE001 — a ledger hiccup degrades the flag, never the list
        pending = set()

    try:
        meta_index = metadata.load()
    except Exception:  # noqa: BLE001 — same rule for the sidecar
        meta_index = {}

    for mid in mission_ids:
        why = out[mid]["why"]
        for key in by_mission.get(mid, []):
            if key in pending:
                why.append("decision")
                break
        for key in by_mission.get(mid, []):
            m = meta_index.get(key)
            if m is not None and getattr(m, "intervention_required", False):
                why.append("intervention")
                break
        out[mid]["needs_you"] = bool(why)

    for r in open_q:
        out[r["mission_id"]]["why"].append("question")
        out[r["mission_id"]]["needs_you"] = True
    for r in escalated:
        out[r["mission_id"]]["why"].append("escalation")
        out[r["mission_id"]]["needs_you"] = True
    return out


# ---------------------------------------------------------------- archive (durable, 3-step)


def begin_archive(
    mission_id: str,
    *,
    abandon: bool = False,
    resume: bool = False,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Step 1 — one transaction, no awaits inside it. Returns the sessions to tear down.

    **Terminal-state-only.** ``POST …/archive`` on a live mission is a **409**, not a prompt:
    there is no path that leaves a mission simultaneously running and archived. A live mission is
    archived by *abandoning it first*, in one explicitly confirmed step — ``{"abandon": true}``
    performs ``→ abandoned`` and stamps ``archiving_at`` in the same transaction. Two distinct
    transitions, one deliberate request, rather than one endpoint that sometimes means "tidy up"
    and sometimes means "kill my work".
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT * FROM missions WHERE id=?", (mission_id,)).fetchone()
            if row is None:
                raise MissionNotFound(mission_id)
            if row["archived_at"] is not None:
                con.execute("ROLLBACK")
                raise MissionError(f"mission {mission_id} is already archived", status=409)
            if row["archiving_at"] is not None:
                # An archive is already in flight and it has an OWNER. Handing a second caller
                # the worklist looked idempotent and was not: once the first worker leased every
                # row, the second saw an empty pending list, walked straight to
                # `finish_archive`, and marked the mission archived while that worker was still
                # inside `cleanup_runtime` killing the session.
                if not resume:
                    con.execute("ROLLBACK")
                    raise MissionError(
                        f"mission {mission_id}: archive already in progress", status=409
                    )
                # Boot recovery may take the MISSION-level claim over. That is safe without
                # proving the previous owner died, because it is not what guards the destructive
                # effect: each session's teardown lease is, and `claim_session_teardown` still
                # answers "taken" for every session whose lease is being kept alive. Taking this
                # over gets recovery as far as the per-session gate and no further.
                token = _new_op_token()
                con.execute("UPDATE missions SET op_token=? WHERE id=?", (token, mission_id))
                # Recovery re-drives FAILED sessions too, not just the ones left mid-flight. A
                # worker that dies mid-teardown settles its own lease as `failed` (so it is never
                # stranded), and that failure may have come *after* the provider effect landed —
                # which only a retry can discover, and which `_archive_idempotent` makes safe by
                # answering `already_archived` rather than repeating the effect. Scoped to the
                # resume path: an ordinary request never reaches this branch.
                con.execute(
                    "UPDATE mission_sessions SET archive_state='pending', archive_error=NULL "
                    "WHERE mission_id=? AND archive_state='failed'",
                    (mission_id,),
                )
                pending = _pending_sessions(con, mission_id)
                # EVERY session the mission ever held, not just the ones still awaiting teardown.
                # The abandon sweep settles live ACTIONS, and an action can name a session that
                # this archive has already torn down — sweeping only `pending` would leave that
                # one claimable, which is the same hole one session narrower.
                roster = [
                    r["session_key"]
                    for r in con.execute(
                        "SELECT session_key FROM mission_sessions WHERE mission_id=? "
                        "ORDER BY added_at ASC",
                        (mission_id,),
                    ).fetchall()
                ]
                con.execute("COMMIT")
                return {
                    "mission_id": mission_id,
                    "sessions": pending,
                    "resumed": True,
                    "op_token": token,
                    # RECOVERY HAS TO KNOW WHICH KIND OF ARCHIVE IT IS FINISHING (#871). An
                    # abandon owes its sessions a live-action sweep before teardown; a plain
                    # archive of an already-terminal mission does not. Without this the resume
                    # path could not tell them apart and finished every pending archive as
                    # though it were the second kind — completing an abandon while an approved
                    # action stayed claimable (review on #881).
                    "state": row["state"],
                    "roster": roster,
                }
            # DECISION 1 (#871): a turn still in flight fences the archive.
            #
            # A turn past its ledger append may already have put bytes on a live PTY, and there
            # is no un-sending them — so a "cancelled" turn that actually delivered would be a
            # lie in the timeline. Refusing costs the operator a retry five seconds later;
            # cancelling costs an archived mission whose agent just received an instruction.
            #
            # Checked INSIDE the transaction, like `_fence_busy`, so it cannot be raced by a
            # claim landing between the read and the write. Bounded, not indefinite: `approved`
            # is in `EXPIRABLE_STATES`, so the TTL sweep settles an approved-but-undelivered
            # action within `proposal_ttl_minutes` and the turn resolves with it.
            unresolved = con.execute(
                "SELECT turn_id FROM mission_turns WHERE mission_id=? AND state='in_progress' "
                "LIMIT 1",
                (mission_id,),
            ).fetchone()
            if unresolved is not None and not abandon:
                con.execute("ROLLBACK")
                raise MissionError(
                    f"mission {mission_id} has an unresolved turn ({unresolved['turn_id']}); "
                    "archive waits for it to settle, or use an explicit abandon",
                    status=409,
                )
            if row["state"] not in TERMINAL_STATES:
                if not abandon:
                    con.execute("ROLLBACK")
                    raise MissionError(
                        f"mission {mission_id} is {row['state']}; archive requires a terminal "
                        f"state or an explicit abandon",
                        status=409,
                    )
                cur = con.execute(
                    "UPDATE missions SET state='abandoned', outcome='abandoned', updated_at=?, "
                    "closed_at=? WHERE id=? AND state=?",
                    (ts, ts, mission_id, row["state"]),
                )
                if not cur.rowcount:
                    con.execute("ROLLBACK")
                    raise MissionError(
                        f"mission {mission_id} is no longer {row['state']}", status=409
                    )
                # …AND THE QUESTION HOLDS GO WITH IT (#900 review 7, finding 3). This is the
                # THIRD entrance to an unanswerable state and it writes the row directly rather
                # than going through `set_state`, so it inherited none of that path's cleanup: an
                # explicit `{"abandon": true}` archive left `question_seq` set on an `abandoned`
                # mission, `derive_needs_you` kept reporting `question`, and every answer was
                # then refused by `_question_answerable`. Flagged for a decision the server will
                # not take, for ever.
                #
                # Unconditional and in the same transaction as the transition: the destination
                # is `abandoned`, which is in `TERMINAL_STATES` and therefore in
                # `UNQUESTIONABLE_STATES` — there is no version of this write that lands somewhere
                # a question could still be answered.
                con.execute(
                    "UPDATE mission_objective_episode SET question_seq=NULL "
                    "WHERE mission_id=? AND question_seq IS NOT NULL",
                    (mission_id,),
                )
                _append_event(
                    con,
                    mission_id,
                    "state",
                    at=ts,
                    meta={"from": row["state"], "to": "abandoned", "why": "archive"},
                )
            # Re-evaluate the skip decision from scratch on every attempt. A row marked
            # `skipped` because another mission held the key must not STAY skipped once that
            # holder releases it — otherwise a re-archive after an unarchive silently omits a
            # session that is now safe to reap.
            con.execute(
                "UPDATE mission_sessions SET archive_state=NULL, archive_error=NULL "
                "WHERE mission_id=? AND archive_state='skipped'",
                (mission_id,),
            )
            # Target the mission's whole ROSTER, not just what it still actively holds.
            # Reaching a terminal state already released ownership (`removed_at` set), so keying
            # the teardown on `removed_at IS NULL` would archive the record of a `done` mission
            # and quietly leave every one of its agents running — the exact runtime cost #840 §11
            # exists to reclaim.
            #
            # The one exception is the session somebody else now holds: it was released and
            # legitimately re-adopted, and tearing it down would kill another mission's agent.
            # Those are marked `skipped` rather than silently omitted, so `finish_archive` can
            # say what it did not touch instead of implying a clean sweep.
            con.execute(
                "UPDATE mission_sessions SET archive_state='skipped', archive_error=NULL "
                "WHERE mission_id=? AND COALESCE(release_reason,'closed') != 'detached' "
                "AND session_key IN ("
                "  SELECT session_key FROM mission_sessions"
                "  WHERE removed_at IS NULL AND mission_id != ?)",
                (mission_id, mission_id),
            )
            # Everything on the roster EXCEPT what the operator explicitly detached. Reaching a
            # terminal state releases ownership and those sessions are still the mission's to
            # reap; a detach is the operator removing the session from the mission, and both set
            # `removed_at` — so without the reason, archiving the roster terminated work that had
            # been deliberately taken out of it.
            con.execute(
                "UPDATE mission_sessions SET archive_state='pending', archive_error=NULL "
                "WHERE mission_id=? AND COALESCE(archive_state,'') != 'skipped' "
                "AND COALESCE(release_reason,'closed') != 'detached'",
                (mission_id,),
            )
            token = _new_op_token()
            con.execute(
                "UPDATE missions SET archiving_at=?, op_token=? WHERE id=?",
                (ts, token, mission_id),
            )
            pending = _pending_sessions(con, mission_id)
            _append_event(
                con,
                mission_id,
                "archive",
                at=ts,
                meta={"phase": "begin", "sessions": [p["session_key"] for p in pending]},
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {
        "mission_id": mission_id,
        "sessions": pending,
        "resumed": False,
        "op_token": token,
    }


def _archive_outcome(con, mission_id: str) -> dict:
    """What this archive actually achieved, read from the roster rather than assumed."""
    return {
        "not_archived": [
            dict(r)
            for r in con.execute(
                "SELECT session_key, archive_error FROM mission_sessions "
                "WHERE mission_id=? AND archive_state='failed'",
                (mission_id,),
            ).fetchall()
        ],
        "skipped": [
            r["session_key"]
            for r in con.execute(
                "SELECT session_key FROM mission_sessions "
                "WHERE mission_id=? AND archive_state='skipped'",
                (mission_id,),
            ).fetchall()
        ],
    }


def _pending_sessions(con, mission_id: str) -> list[dict]:
    rows = con.execute(
        "SELECT session_key, role FROM mission_sessions "
        "WHERE mission_id=? AND archive_state='pending' ORDER BY added_at ASC",
        (mission_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def claim_session_teardown(
    mission_id: str, session_key: str, *, now: float | None = None, path: Path | None = None
) -> tuple[str, str | None]:
    """Take the exclusive lease on one session's external teardown. Returns what to do.

    Three things have to be decided together, under one transaction, or the destructive effect
    runs when it should not:

    * ``"claimed"`` — this worker owns the teardown. ``pending → in_progress`` is a CAS, so a
      concurrent request (or a retry of the same one, which ``begin_archive`` hands the *same*
      pending rows) loses and does nothing. ``prov.archive`` is not idempotent in general, so
      without this the external effect runs twice.
    * ``"skipped"`` — **another open mission holds this session now.** The roster snapshot taken
      at ``begin_archive`` is stale by the time teardown runs: reaching a terminal state released
      the key, and the partial unique index only guards ``removed_at IS NULL``, so mission B can
      legitimately adopt it in between. Tearing it down then kills a live agent belonging to a
      different mission. Re-checked HERE, at the write boundary, not at plan time.
    * ``"taken"`` — somebody else already holds the lease; do nothing.

    Returns ``(verdict, token)``. The token is the **fencing token** and the worker must present it
    at settlement: a lease reclaimed after expiry mints a new one, so a worker that finishes late
    cannot settle the operation that replaced it.
    """
    ts = time.time() if now is None else now
    # The reservation is the mutex the sibling session routes also take, so the two paths exclude
    # each other rather than merely checking each other. Taken FIRST: if somebody else is changing
    # this session's provider state, there is nothing to claim.
    try:
        token = reserve_session(session_key, f"mission:{mission_id}", now=now, path=path)
    except SessionBusy:
        return "taken", None
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT archive_state FROM mission_sessions WHERE mission_id=? AND session_key=?",
                (mission_id, session_key),
            ).fetchone()
            if row is None:
                con.execute("COMMIT")
                release_session(session_key, token, path=path)
                return "gone", None
            if row["archive_state"] != "pending":
                con.execute("COMMIT")
                release_session(session_key, token, path=path)
                return "taken", None
            holder = con.execute(
                "SELECT mission_id FROM mission_sessions "
                "WHERE session_key=? AND removed_at IS NULL AND mission_id != ? LIMIT 1",
                (session_key, mission_id),
            ).fetchone()
            if holder is not None:
                con.execute(
                    "UPDATE mission_sessions SET archive_state='skipped', archive_error=? "
                    "WHERE mission_id=? AND session_key=?",
                    (f"held by mission {holder['mission_id']}", mission_id, session_key),
                )
                _append_event(
                    con,
                    mission_id,
                    "archive",
                    at=ts,
                    session_key=session_key,
                    meta={"phase": "session", "outcome": "skipped", "holder": holder["mission_id"]},
                )
                con.execute("COMMIT")
                release_session(session_key, token, path=path)
                return "skipped", None
            con.execute(
                "UPDATE mission_sessions SET archive_state='in_progress', lease_owner=?, "
                "lease_at=?, lease_token=? WHERE mission_id=? AND session_key=? "
                "AND archive_state='pending'",
                (PROCESS_EPOCH, ts, token, mission_id, session_key),
            )
            con.execute("COMMIT")
            return "claimed", token
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def reopen_stale_leases(
    mission_id: str, *, now: float | None = None, path: Path | None = None
) -> int:
    """Reopen leases held by a process that is gone. Returns how many.

    Covers both kinds — a teardown lease (``in_progress``) goes back to ``pending``, a restore
    lease (``restoring``) back to ``done`` — because missing the restore half made recovery skip
    the session, invoke no provider call, and then report the mission unarchived.

    **Expiry is the only proof of death, and it is now worth something.** Two earlier premises
    were wrong in the same way — each assumed a fact it had not established:

    * *"any lease found here belongs to a crashed worker"* — false the moment recovery moved from
      boot to a background task running beside live requests.
    * *"a lease not stamped with our own :data:`PROCESS_EPOCH` outlived its process"* — false
      whenever a second app instance is serving the same store, which this app supports (the
      single-writer lock is explicitly cross-instance). A foreign epoch means *another* process,
      not a *dead* one, and reclaiming it immediately put two live workers inside the same
      irreversible external effect. A fencing token cannot fix that: it makes the loser's database
      write fail, and says nothing about the process it already terminated or the file it moved.

    So a lease is reclaimed only once it has gone :data:`LEASE_MAX_AGE_S` without a heartbeat.
    That is a real signal rather than a timing assumption, because a live holder renews (see
    :func:`holding`) — an expired lease means the holder stopped beating, in any process. The
    owner stamp stays, but it is now diagnostic, not a licence to reclaim.
    """
    cutoff = (time.time() if now is None else now) - LEASE_MAX_AGE_S
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            # Exactly which sessions are being reclaimed, decided ONCE and reused below. The
            # reservation is half of the same claim, so it must be released for these rows and
            # ONLY these: a blanket delete would strip the mutex from a sibling session whose
            # worker is still beating, and an independent age check on the reservation would give
            # one claim two clocks — the lease reopening while the mutex it needs stayed held.
            dead = [
                r["session_key"]
                for r in con.execute(
                    "SELECT session_key FROM mission_sessions WHERE mission_id=? "
                    "AND archive_state IN ('in_progress','restoring') "
                    "AND COALESCE(lease_at, 0) < ?",
                    (mission_id, cutoff),
                ).fetchall()
            ]
            n = (
                con.execute(
                    "UPDATE mission_sessions SET archive_state='pending', lease_owner=NULL, "
                    "lease_at=NULL, lease_token=NULL "
                    "WHERE mission_id=? AND archive_state='in_progress' "
                    "AND COALESCE(lease_at, 0) < ?",
                    (mission_id, cutoff),
                ).rowcount
                or 0
            )
            n += (
                con.execute(
                    "UPDATE mission_sessions SET archive_state='done', lease_owner=NULL, "
                    "lease_at=NULL, lease_token=NULL "
                    "WHERE mission_id=? AND archive_state='restoring' "
                    "AND COALESCE(lease_at, 0) < ?",
                    (mission_id, cutoff),
                ).rowcount
                or 0
            )
            # Reclaiming a lease reclaims its RESERVATION too — they are one claim, stamped
            # together and renewed together, so the lease's expiry is proof for both.
            for key in dead:  # one statement per key: no SQL assembled from a Python expression
                con.execute(
                    "DELETE FROM session_reservations WHERE holder=? AND session_key=?",
                    (f"mission:{mission_id}", key),
                )
            con.execute("COMMIT")
            return n
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


#: How long a turn claim may go without a heartbeat before it is treated as orphaned. Same
#: reasoning as the session lease: a live holder renews, so reaching this means the owner stopped.
TURN_OWNER_MAX_AGE_S = 300

#: What a claim attempt learned. Four answers, because collapsing any two of them is how a
#: duplicated instruction gets issued (#852).
TURN_CLAIMED = "claimed"  # this caller owns it; proceed to the model
TURN_LIVE = "live"  # someone else is running it right now; answer in-progress, call nothing
TURN_DONE = "done"  # already completed; replay the stored result identically
TURN_CONFLICT = "conflict"  # same id, different message — a different turn wearing a used key
TURN_RECOVER = "recover"  # orphaned and nothing was ever appended; the ONE re-entry path
TURN_RECONCILE = "reconcile"  # orphaned but a write may have landed; settle from provenance


# NOTE ON THE OPERATOR EVENT, which used to be written here.
#
# Committing it with the claim was right about ordering (the question must be durable before the
# model call) and wrong about lifetime: a claim can be RELEASED — a busy flight, an unconfigured
# endpoint — and releasing then had to delete the event again. Append-then-delete cannot be undone,
# because `_append_event` trims the mission to its soft cap on the way in, and the delete cannot
# restore whichever older recap that eviction dropped. Repeated transient 409s drained history for
# turns that never ran.
#
# So the event is written by the caller AFTER the transient window closes and before `ask` — same
# ordering guarantee, no deletion, nothing to undo. `append_turn_event` keeps it exactly-once
# across a recovery that re-enters the turn.
def claim_turn(
    mission_id: str,
    turn_id: str,
    msg_sha: str,
    *,
    text: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> tuple[str, dict | None]:
    """Take, replay or recover one operator turn. Returns ``(verdict, row)``.

    **The INSERT is the claim.** A concurrent duplicate loses at the database rather than at a
    check, which is what makes this idempotent rather than merely ordered — two requests carrying
    the same new ``turn_id`` cannot both reach the model.

    The verdicts are deliberately six, not two. ``in_progress`` alone cannot distinguish a request
    that is still running from a claim whose process died, and answering both the same way either
    strands the turn forever or duplicates a live instruction. Ownership separates them; the
    write receipt then separates *recoverable* from *already-acted*:

    * :data:`TURN_LIVE` — the owner is beating. Call nothing.
    * :data:`TURN_RECONCILE` — orphaned, but ``write_reserved_at`` is set, so an append may have
      landed. Settle from provenance; **never** re-enter the model.
    * :data:`TURN_RECOVER` — orphaned with **no** receipt, which positively means nothing was ever
      appended. The only path on which the model is called a second time.

    **`text` writes the operator's message IN THIS TRANSACTION** (#871 decision 3). It used to be
    appended by the caller after a configuration preflight, and that ordering is the source of
    #852's five consecutive fix-caused-the-next-defect rounds: a check before the call and a check
    inside it are two moments, so configuration could vanish between them, and the handler then
    released the claim but not the event. Every attempt to repair that from one side created the
    next defect on the other — release-leaves-the-event, delete-loses-trimmed-history.

    One transaction removes the state instead of making it rarer: either the claim and the
    operator event both exist, or neither does. There is nothing left to release and nothing left
    to delete, which is what makes the forward-only rule ("a turn never deletes anything, and
    never re-sends anything") actually hold rather than nearly hold.

    Only on :data:`TURN_CLAIMED`, deliberately — a replay must not append a second copy of a
    message the timeline already carries.
    """
    ts = time.time() if now is None else now
    fence = uuid.uuid4().hex
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            # The SAME fence every other mutation takes, in the same place: inside the
            # transaction, so it cannot be raced. A turn is an ordinary mutation of a mission —
            # it writes the timeline and can issue a live instruction — and skipping it let an
            # ARCHIVED mission mutate its sensitive timeline and reach the actuator with no
            # unarchive predecessor, while racing retention's deletion of that same record.
            _fence_busy(con, mission_id)
            try:
                n = con.execute(
                    "INSERT INTO mission_turns "
                    "(mission_id, turn_id, msg_sha, state, owner, owner_at, fence, created_at, "
                    " message) "
                    "VALUES (?,?,?,'in_progress',?,?,?,?,?) ON CONFLICT DO NOTHING",
                    (
                        mission_id,
                        turn_id,
                        msg_sha,
                        PROCESS_EPOCH,
                        ts,
                        fence,
                        ts,
                        _cap(text, EVENT_TEXT_MAX) if text else None,
                    ),
                ).rowcount
            except sqlite3.IntegrityError:
                # The mission went away between the caller's existence check and this insert —
                # check-then-act, one layer down. The foreign key is the AUTHORITATIVE answer, so
                # it becomes the caller's 404 rather than escaping as a 500 that says "we broke"
                # about a request that was merely aimed at something gone.
                con.execute("ROLLBACK")
                raise MissionError("unknown mission", status=404) from None
            if n:
                if text is not None:
                    seq = _append_event(
                        con,
                        mission_id,
                        "operator_msg",
                        at=ts,
                        text=text,
                        meta={"turn_id": turn_id},
                    )
                    con.execute(
                        "UPDATE mission_turns SET operator_seq=? "
                        "WHERE mission_id=? AND turn_id=?",
                        (seq, mission_id, turn_id),
                    )
                con.execute("COMMIT")
                return TURN_CLAIMED, {"fence": fence, "mission_id": mission_id, "turn_id": turn_id}
            row = con.execute(
                "SELECT * FROM mission_turns WHERE mission_id=? AND turn_id=?",
                (mission_id, turn_id),
            ).fetchone()
            if row is None:  # deleted between the insert and the read; treat as a lost race
                con.execute("COMMIT")
                return TURN_LIVE, None
            rec = dict(row)
            if rec["msg_sha"] != msg_sha:
                con.execute("COMMIT")
                return TURN_CONFLICT, rec
            if rec["state"] != "in_progress":
                con.execute("COMMIT")
                return TURN_DONE, rec
            if float(rec["owner_at"] or 0) >= ts - TURN_OWNER_MAX_AGE_S:
                con.execute("COMMIT")
                return TURN_LIVE, rec
            # Orphaned. Take ownership under a NEW fence, so the previous owner can neither
            # settle this turn nor reserve a write against it.
            con.execute(
                "UPDATE mission_turns SET owner=?, owner_at=?, fence=? "
                "WHERE mission_id=? AND turn_id=?",
                (PROCESS_EPOCH, ts, fence, mission_id, turn_id),
            )
            con.execute("COMMIT")
            rec["fence"] = fence
            # The receipt decides, not the clock: a write may already have happened.
            return (TURN_RECONCILE if rec["write_reserved_at"] else TURN_RECOVER), rec
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def reserve_turn_write(
    mission_id: str,
    turn_id: str,
    fence: str,
    intended_action_ids: list[str] | None = None,
    *,
    now: float | None = None,
    path: Path | None = None,
) -> bool:
    """Record that this turn is **about to** append to the ledger. False if the fence is stale.

    **This is the linearization point, and it is a reservation rather than a check.** Validating
    the fence and then appending is check-then-write: the owner can pass validation, be reclaimed,
    and append afterwards — landing a second instruction that the fence never sees, because by
    then the irreversible half has happened.

    So the receipt is written **first**, in its own committed transaction, and a stale fence fails
    here so the append never happens at all.

    It commits before the ledger lock is ever taken, deliberately. Holding a missions transaction
    across the append would close the same race while nesting missions → ledger, inverting the
    order `orchestrator_ledger.compact()` already establishes (it holds the ledger lock and then
    writes this store) — a deadlock between an ordinary chat turn and a routine compaction pass.
    """
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            # THE MISSION FENCE, HERE — not only at the claim (#871, review on #881).
            #
            # `{"abandon": true}` commits the archive and THEN scans the ledger once. A model
            # call already in flight reaches this reservation after that scan, and the turn fence
            # alone says nothing about the mission — so the append landed a fresh `approved`
            # action on a mission that had just been abandoned, after the sweep that was supposed
            # to have settled everything.
            #
            # This is the linearization point for the append, so it is the right place to ask:
            # an archived or mid-operation mission refuses the reservation, and the append never
            # happens at all.
            _fence_busy(con, mission_id)
            # The receipt records WHAT is about to be written, not merely that something is.
            # Recovery can then look for exactly those actions instead of scanning for anything
            # that looks related — deterministic identity rather than an inference.
            n = con.execute(
                "UPDATE mission_turns SET write_reserved_at=COALESCE(write_reserved_at, ?), "
                "action_ids=COALESCE(action_ids, ?) "
                "WHERE mission_id=? AND turn_id=? AND fence=? AND state='in_progress'",
                (ts, json.dumps(intended_action_ids or []), mission_id, turn_id, fence),
            ).rowcount
            con.execute("COMMIT")
            return bool(n)
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def park_turn(mission_id: str, turn_id: str, fence: str, *, path: Path | None = None) -> bool:
    """Release a turn's OWNER while leaving it `in_progress`. False on a stale fence.

    Decision 2 keeps a turn open until its action is terminal, so a frame can finish its work and
    still not settle. Leaving its owner lease held would make the turn read TURN_LIVE — "someone
    is running this right now" — for the full `TURN_OWNER_MAX_AGE_S` while nobody is, so the next
    request could neither reconcile it nor report anything but "still running".

    Parking states what is actually true: the turn is unfinished and unowned, waiting on its
    action rather than on a worker. The next request reconciles it immediately instead of waiting
    out a lease nobody holds.

    NOT `release_turn`, which DELETES the claim: the operator's message is in that claim's
    transaction, and the receipt is what recovery reads. Parking keeps both.
    """
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            n = con.execute(
                "UPDATE mission_turns SET owner=NULL, owner_at=NULL "
                "WHERE mission_id=? AND turn_id=? AND fence=? AND state='in_progress'",
                (mission_id, turn_id, fence),
            ).rowcount
            con.execute("COMMIT")
            return bool(n)
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def note_turn_delivery_error(
    mission_id: str, turn_id: str, fence: str, error: str, *, path: Path | None = None
) -> bool:
    """Record a delivery failure on a turn that is NOT settling. False on a stale fence.

    Since decision 2 is enforced on every path, a turn whose delivery failed keeps its action
    `approved` and therefore stays `in_progress` — it does not reach `settle_turn`, which is the
    only thing that used to persist `delivery_error`. So the failure was reported on the first
    response and lost on every one after it.

    "A failure that is recorded and never shown is not a failure that was reported" is the
    contract that made `delivery_error` a top-level field in the first place; this is what keeps
    it true now that the turn stays open.
    """
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT result_meta FROM mission_turns "
                "WHERE mission_id=? AND turn_id=? AND fence=? AND state='in_progress'",
                (mission_id, turn_id, fence),
            ).fetchone()
            if row is None:
                con.execute("ROLLBACK")
                return False
            try:
                meta = json.loads(row["result_meta"] or "{}") or {}
            except (TypeError, ValueError):
                meta = {}
            meta["delivery_error"] = error
            con.execute(
                "UPDATE mission_turns SET result_meta=? "
                "WHERE mission_id=? AND turn_id=? AND fence=?",
                (json.dumps(meta), mission_id, turn_id, fence),
            )
            con.execute("COMMIT")
            return True
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def _turn_row(row) -> dict | None:
    """One `mission_turns` row as the console reads it, or None. Shared by both readers."""
    if row is None:
        return None
    meta = _loads(row["result_meta"]) or {}
    if not isinstance(meta, dict):
        meta = {}
    return {
        "turn_id": str(row["turn_id"]),
        "state": str(row["state"]),
        "text": str(row["message"] or ""),
        "delivery_error": str(meta.get("delivery_error") or ""),
        "created_at": float(row["created_at"] or 0),
    }


def open_turn(mission_id: str, *, path: Path | None = None) -> dict | None:
    """The turn the operator is still owed an outcome for, or None. Read at DETAIL time.

    The composer used to hold this in component state, which meant a reload lost it: an
    `in_progress` turn came back as an ordinary message with no "still working", and an
    `indeterminate` one — the state that exists precisely because nobody can say whether the
    instruction went out — came back as a settled Answer with no way to ask again. A durable turn
    whose only representation is a React ref is not durable (#902 review, finding 1).

    Two states qualify, and for different reasons: `in_progress` because the work is genuinely
    unfinished, and `indeterminate` because it is terminal and *ambiguous*, so the operator has a
    decision to make and must be able to find it after a reload. `done` never qualifies — its
    answer is on the timeline, which is where a finished turn lives.

    The TEXT comes from the TURN, not from the timeline (#902 review 2, finding 2). The first
    version joined to the operator's event on `operator_seq`, which reads well and is wrong: the
    timeline is a capped FEED, so past the soft cap an open turn came back with no text — the
    question missing after a reload, and CHECK AGAIN resending an empty message and getting a 422
    instead of replaying the stable id. Durable state does not live in a feed. `msg_sha` still
    stores the hash, because that is what the idempotency check compares.
    """
    validate_id(mission_id)
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT turn_id, state, result_meta, created_at, message "
            "FROM mission_turns "
            "WHERE mission_id=? AND state IN ('in_progress','indeterminate') "
            "  AND acked_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1",
            (mission_id,),
        ).fetchone()
        return _turn_row(row)
    finally:
        con.close()


def ack_turn(
    mission_id: str, turn_id: str, *, now: float | None = None, path: Path | None = None
) -> bool:
    """The operator dismissed an ambiguous turn. True if a row moved.

    ONLY `indeterminate`. Dismissing an `in_progress` turn would hide work that is still running,
    and dismissing a `done` one is meaningless — its answer is on the timeline. The predicate is
    in the UPDATE rather than in a prior read, so a turn that settles between the two cannot be
    dismissed by a request that was authorised against its earlier state.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            n = con.execute(
                "UPDATE mission_turns SET acked_at=? "
                "WHERE mission_id=? AND turn_id=? AND state='indeterminate' AND acked_at IS NULL",
                (ts, mission_id, turn_id),
            ).rowcount
            con.commit()
            return bool(n)
        finally:
            con.close()


def settle_turn(
    mission_id: str,
    turn_id: str,
    fence: str,
    *,
    state: str = "done",
    result: str | None = None,
    result_meta: dict | None = None,
    action_ids: list[str] | None = None,
    action_snapshot: list[dict] | None = None,
    assistant_text: str | None = None,
    assistant_meta: object = None,
    now: float | None = None,
    path: Path | None = None,
) -> bool:
    """Complete a turn under its fence. False when the fence is stale — a reclaimed owner may not
    land its result over the recovery that replaced it.

    The assistant event is written **in this transaction**, for the reason `append_event`
    documents: an event that is part of an operator-visible change belongs inside that change, or
    the timeline can disagree with the state it describes. Appending it afterwards and suppressing
    the failure left a *terminal* turn with no answer in its timeline and no path that ever
    repaired it — the replay returns the stored response and looks perfectly healthy.
    """
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            n = con.execute(
                "UPDATE mission_turns SET state=?, result=?, result_meta=?, action_ids=?, "
                "settled_at=?, owner=NULL, owner_at=NULL "
                "WHERE mission_id=? AND turn_id=? AND fence=? AND state='in_progress'",
                (
                    state,
                    result,
                    # `action_snapshot or []` conflated the two answers decision 4 exists to
                    # keep apart: `None` means NO SNAPSHOT WAS TAKEN (a recovery settled this
                    # turn from provenance), and `[]` means one was taken and there were no
                    # actions. `or` maps both to `[]`, so a reader could not tell "we never
                    # looked" from "we looked and found nothing" — the same lesson #862 landed
                    # twice (`unknown` is not `historical`; `states is None` is not `{}`). Three
                    # instances make it the codebase's rule, not a special case.
                    json.dumps({**(result_meta or {}), "actions": action_snapshot}),
                    json.dumps(action_ids or []),
                    ts,
                    mission_id,
                    turn_id,
                    fence,
                ),
            ).rowcount
            if n and assistant_text is not None:
                row = con.execute(
                    "SELECT assistant_seq FROM mission_turns WHERE mission_id=? AND turn_id=?",
                    (mission_id, turn_id),
                ).fetchone()
                if row is not None and row["assistant_seq"] is None:
                    # THE TURN ID RIDES ON THE ANSWER, exactly as it rides on the operator's own
                    # message (#902 review 2, finding 1). It is what lets a client correlate a
                    # request whose response was LOST with the answer the server already stored —
                    # without it, "my request failed" and "the turn never happened" are the same
                    # observation, and the composer showed TRY AGAIN beside a stored answer for
                    # ever. Stamped here rather than left to the caller, because a correlation
                    # key that depends on a call site is one a later call site will omit.
                    meta = dict(assistant_meta) if isinstance(assistant_meta, dict) else {}
                    meta.setdefault("turn_id", turn_id)
                    seq = _append_event(
                        con,
                        mission_id,
                        "assistant_msg",
                        at=ts,
                        text=assistant_text,
                        meta=meta,
                    )
                    con.execute(
                        "UPDATE mission_turns SET assistant_seq=? WHERE mission_id=? AND turn_id=?",
                        (seq, mission_id, turn_id),
                    )
            con.execute("COMMIT")
            return bool(n)
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def renew_turn(
    mission_id: str, turn_id: str, fence: str, *, now: float | None = None, path: Path | None = None
) -> bool:
    """Prove the owner of ``fence`` is still running this turn."""
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            n = con.execute(
                "UPDATE mission_turns SET owner_at=? WHERE mission_id=? AND turn_id=? AND fence=?",
                (ts, mission_id, turn_id, fence),
            ).rowcount
            con.execute("COMMIT")
            return bool(n)
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


@contextlib.asynccontextmanager
async def holding_turn(
    mission_id: str,
    turn_id: str,
    fence: str,
    *,
    interval: float = RESERVATION_RENEW_S,
    path: Path | None = None,
) -> AsyncIterator[None]:
    """Keep a turn claim alive for as long as the model call actually runs.

    Same mechanism and same reason as :func:`holding`: the beat runs on its own thread, so it is
    not starved by whatever the request is doing, and a renewal makes the claim's expiry mean
    *the owner stopped* rather than *the owner is slow*. A model call is exactly the kind of long,
    externally-paced wait that would otherwise look like death.
    """
    if not fence:
        yield
        return
    stop = threading.Event()

    def _beat() -> None:
        wait = interval
        while not stop.wait(wait):
            try:
                alive = renew_turn(mission_id, turn_id, fence, path=path)
            except Exception:  # transient — retry soon, not on the ordinary cadence
                wait = beat_wait(False, interval)
                continue
            wait = beat_wait(True, interval)
            if not alive:
                # Fenced out or already settled. Either way this beat has nothing left to hold.
                return

    beat = threading.Thread(target=_beat, name="mission-turn-heartbeat", daemon=True)
    beat.start()
    try:
        yield
    finally:
        stop.set()
        beat.join(timeout=5.0)


def release_turn(mission_id: str, turn_id: str, fence: str, *, path: Path | None = None) -> bool:
    """Give a claim back **without settling it**, so the same `turn_id` can be retried.

    `abandon_turn` is for a turn that reached an honest dead end; this is for one that never
    started — a busy single-flight, an unconfigured endpoint. Those are transient conditions of
    the *system*, not outcomes of the *turn*, and settling them `indeterminate` permanently
    consumes the id: the operator's retry replays a terminal record and never calls the model.

    **Refuses once a write was reserved.** A reservation means an append may already have
    happened, and deleting the claim would erase the only evidence recovery has — so a turn past
    that point can only ever be settled, never released.
    """
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            # Nothing to undo: the operator event is no longer written at claim time, so a
            # released claim leaves no event behind and no trimmed history to restore.
            n = con.execute(
                "DELETE FROM mission_turns WHERE mission_id=? AND turn_id=? AND fence=? "
                "AND state='in_progress' AND write_reserved_at IS NULL",
                (mission_id, turn_id, fence),
            ).rowcount
            con.execute("COMMIT")
            return bool(n)
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def abandon_turn(
    mission_id: str,
    turn_id: str,
    fence: str,
    *,
    turn_id_meta: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> bool:
    """Settle a turn `indeterminate` — reached, and never re-asked.

    The receipt is a **one-way no-reask barrier**, and that leaves exactly one window it cannot
    resolve: the process commits `write_reserved_at` and dies before the ledger append. On restart
    there is no provenance to replay, and the receipt correctly forbids calling the model again —
    so without this the turn sits `in_progress` for ever, holding a compaction pin and answering
    every replay with "still running" about a request that no longer exists.

    `indeterminate` is the honest terminal state: we cannot say whether the instruction went out,
    we will not send a second one to find out, and the operator sees an unanswered turn rather
    than a silent hang. Deterministic, so a replay after it is a plain `done`-style read.
    """
    return settle_turn(
        mission_id,
        turn_id,
        fence,
        state="indeterminate",
        result=None,
        action_ids=[],
        # Terminal is terminal: an abandoned turn carries its event like any other, so the
        # timeline never has a settled turn it cannot account for.
        assistant_text="",
        assistant_meta={"turn_id": turn_id_meta or turn_id, "indeterminate": True},
        now=now,
        path=path,
    )


def append_turn_event(
    mission_id: str,
    turn_id: str,
    slot: str,
    kind: str,
    *,
    text: str | None = None,
    meta: object = None,
    now: float | None = None,
    path: Path | None = None,
) -> int | None:
    """Append one of a turn's two timeline events **exactly once**. Returns its ``seq``.

    ``slot`` is ``"operator"`` or ``"assistant"``.

    Exactly-once is achievable here and it would not be anywhere else, because `mission_events`
    and `mission_turns` are **the same database**: the "have I already written this" check and the
    append commit in ONE transaction, so a crash cannot land between them. Appending and then
    recording the fact separately is the two-store problem this whole feature is about, in
    miniature — and the crash window it opens is real: recovery re-enters the turn and writes a
    second `operator_msg` for a message the operator sent once.

    Returns the existing ``seq`` when the slot is already filled, so a replay is a no-op rather
    than a duplicate.
    """
    # Two literal statements per slot rather than one with the column name interpolated. `slot`
    # is validated and could be spelled into SQL safely, but "safe dynamic SQL" is a claim a
    # reviewer has to re-derive every time, and there are exactly two cases.
    if slot == "operator":
        read_sql = "SELECT operator_seq AS seq FROM mission_turns WHERE mission_id=? AND turn_id=?"
        write_sql = "UPDATE mission_turns SET operator_seq=? WHERE mission_id=? AND turn_id=?"
    elif slot == "assistant":
        read_sql = "SELECT assistant_seq AS seq FROM mission_turns WHERE mission_id=? AND turn_id=?"
        write_sql = "UPDATE mission_turns SET assistant_seq=? WHERE mission_id=? AND turn_id=?"
    else:
        raise MissionError(f"unknown turn event slot {slot!r}", status=500)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(read_sql, (mission_id, turn_id)).fetchone()
            if row is None:
                con.execute("COMMIT")
                return None
            if row["seq"] is not None:
                con.execute("COMMIT")
                return int(row["seq"])
            seq = _append_event(con, mission_id, kind, at=ts, text=text, meta=meta)
            con.execute(write_sql, (seq, mission_id, turn_id))
            con.execute("COMMIT")
            return seq
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def get_turn(mission_id: str, turn_id: str, *, path: Path | None = None) -> dict | None:
    """One turn record, or None. The store's own answer, for when a frame must not speak for it."""
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT * FROM mission_turns WHERE mission_id=? AND turn_id=?", (mission_id, turn_id)
        ).fetchone()
        return dict(row) if row is not None else None
    finally:
        con.close()


def unresolved_turn_keys(*, path: Path | None = None) -> set[tuple[str, str]]:
    """`(mission_id, turn_id)` for every claim still `in_progress`, for compaction to pin against.

    Without this, "the ledger is readable and holds no action for my turn" cannot be told apart
    from "it held one and compaction removed it" — and the safe reading of that ambiguity is to
    never re-ask, which strands every recoverable turn. Pinning makes the absence positive.

    **Both halves of the key.** `turn_id` is client-generated and may legitimately repeat across
    missions, so pinning on it alone let one long-running turn in mission A retain unrelated
    terminal history from mission B — defeating the ledger's global bound and holding on to
    sensitive action records nothing was waiting for.
    """
    con = _ready(path)
    try:
        return {
            (str(r["mission_id"]), str(r["turn_id"]))
            for r in con.execute(
                "SELECT mission_id, turn_id FROM mission_turns WHERE state='in_progress'"
            ).fetchall()
        }
    finally:
        con.close()


def orphaned_turns(
    *, mission_id: str | None = None, now: float | None = None, path: Path | None = None
) -> list[dict]:
    """`in_progress` turns whose owner has stopped heartbeating, with their reserved action ids.

    The worklist for :func:`mission_turn_reconcile.reconcile`. An owner that is still renewing its
    lease is genuinely mid-flight and must never be reconciled — that would settle a turn whose
    model call is about to return its own answer. So the cutoff is the same
    :data:`TURN_OWNER_MAX_AGE_S` that `claim_turn` reclaims on: past it, the owner has stopped,
    whatever the reason.

    `action_ids` is the reservation `reserve_turn_write` wrote BEFORE appending, so it names what
    the turn was about to put in the ledger even when the process died mid-append. That is what
    makes reconciliation deterministic — looking for exactly those ids — rather than a search for
    anything that looks related.
    """
    ts = time.time() if now is None else now
    cutoff = ts - TURN_OWNER_MAX_AGE_S
    con = _ready(path)
    try:
        sql = (
            "SELECT mission_id, turn_id, fence, action_ids, write_reserved_at, owner_at "
            "FROM mission_turns WHERE state='in_progress' "
            "AND (owner_at IS NULL OR owner_at < ?)"
        )
        args: list[object] = [cutoff]
        if mission_id is not None:
            sql += " AND mission_id=?"
            args.append(mission_id)
        rows = con.execute(sql, args).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        try:
            ids = json.loads(r["action_ids"] or "[]")
        except (TypeError, ValueError):
            ids = []
        out.append(
            {
                "mission_id": str(r["mission_id"]),
                "turn_id": str(r["turn_id"]),
                "fence": str(r["fence"]),
                "action_ids": [str(a) for a in ids if a],
                "reserved": r["write_reserved_at"] is not None,
            }
        )
    return out


def next_lease_expiry(*, path: Path | None = None) -> float | None:
    """When the earliest still-held lease becomes reclaimable, or ``None`` if none is held.

    Recovery needs this because reclamation is now expiry-only: after a crash, a lease left by the
    dead process is untouchable until it ages out, so a retry loop with a delay shorter than
    :data:`LEASE_MAX_AGE_S` would burn its whole budget re-reading a worklist it cannot act on and
    then report the mission unrecoverable. Waiting the *remaining* time is the difference between
    patient and stuck.
    """
    con = _ready(path)
    try:
        row = con.execute(
            "SELECT MIN(lease_at) AS t FROM mission_sessions "
            "WHERE lease_at IS NOT NULL AND archive_state IN ('in_progress','restoring')"
        ).fetchone()
    finally:
        con.close()
    return None if row is None or row["t"] is None else float(row["t"]) + LEASE_MAX_AGE_S


def settle_session_archive(
    mission_id: str,
    session_key: str,
    outcome: str,
    *,
    reason: str = "",
    token: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> None:
    """Step 2's settlement — one tiny transaction per session, outside any teardown.

    **Requires the fencing token the claim minted.** A lease reclaimed after expiry mints a new
    token, so a worker that finishes late cannot settle the operation that replaced it — otherwise
    the stale result wins, the fresh lease is cleared, and the external effect can run twice.
    A token that no longer matches settles nothing.
    """
    if outcome not in ARCHIVE_STATES:
        raise MissionError(f"unknown archive outcome {outcome!r}", status=422)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            won = con.execute(
                "UPDATE mission_sessions SET archive_state=?, archive_error=?, lease_owner=NULL, "
                "lease_at=NULL, lease_token=NULL, "
                "removed_at=CASE WHEN ? THEN COALESCE(removed_at, ?) ELSE removed_at END, "
                "release_reason=CASE WHEN ? THEN COALESCE(release_reason,'archived') "
                "  ELSE release_reason END "
                "WHERE mission_id=? AND session_key=? "
                "AND archive_state IN ('pending','in_progress') "
                "AND (? IS NULL OR lease_token IS NULL OR lease_token = ?)",
                (
                    outcome,
                    _cap_or_none(reason, 300),
                    1 if outcome != "failed" else 0,
                    ts,
                    1 if outcome != "failed" else 0,
                    mission_id,
                    session_key,
                    token,
                    token,
                ),
            ).rowcount
            # **Only the winner publishes.** The guarded UPDATE already refuses a stale worker's
            # settlement, but appending the event regardless let a worker whose CAS LOST write
            # `outcome: done` into an append-only timeline — a completion claimed by somebody who
            # completed nothing. The event and the release belong to whoever actually won.
            if not won:
                con.execute("COMMIT")
                return
            _append_event(
                con,
                mission_id,
                "archive",
                at=ts,
                session_key=session_key,
                meta={"phase": "session", "outcome": outcome, "reason": _cap(reason, 300)},
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    if token and won:
        release_session(session_key, token, path=path)


def finish_archive(
    mission_id: str,
    *,
    op_token: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Step 3 — stamp ``archived_at``, release anything still held, and name what did not archive.

    **Refuses unless it owns the operation and no lease is still open.** Both guards exist because
    the same reproduction breaks without either: a second caller that did not start this archive
    used to walk in, find no ``pending`` rows (the first worker had leased them all), mark the
    mission archived and then unarchive it — while the first worker was still inside
    ``cleanup_runtime`` killing a session, which it then settled into a now-live mission.

    So finalisation is conditional on *this* being the attempt that began, and on every session
    having actually settled. An open lease is not a failure to report — it is a reason not to
    finish yet.

    **Honest about partial failure**, once it does finish: the mission archives and the record
    names the session that survived, rather than reporting a clean sweep.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT archiving_at, archived_at, op_token FROM missions WHERE id=?",
                (mission_id,),
            ).fetchone()
            if row is None:
                raise MissionNotFound(mission_id)
            if row["archiving_at"] is None and row["archived_at"] is not None:
                # Already finished; idempotent for a retry. Report the REAL outcome rather than
                # an empty one — a second call must not turn a partial archive into a clean sweep.
                done = _archive_outcome(con, mission_id)
                con.execute("COMMIT")
                return {"mission_id": mission_id, "archived": True, **done}
            if op_token is not None and row["op_token"] != op_token:
                con.execute("ROLLBACK")
                raise MissionError(
                    f"mission {mission_id}: this is not the archive attempt in flight", status=409
                )
            open_leases = [
                r["session_key"]
                for r in con.execute(
                    "SELECT session_key FROM mission_sessions "
                    "WHERE mission_id=? AND archive_state IN ('pending','in_progress')",
                    (mission_id,),
                ).fetchall()
            ]
            if open_leases:
                con.execute("ROLLBACK")
                raise MissionError(
                    f"mission {mission_id}: {len(open_leases)} session teardown(s) still open",
                    status=409,
                )
            outcome = _archive_outcome(con, mission_id)
            failures, skipped = outcome["not_archived"], outcome["skipped"]
            con.execute(
                "UPDATE mission_sessions SET removed_at=?, release_reason='archived' "
                "WHERE mission_id=? AND removed_at IS NULL",
                (ts, mission_id),
            )
            con.execute(
                "UPDATE missions SET archived_at=?, archiving_at=NULL, op_token=NULL, "
                "updated_at=? WHERE id=?",
                (ts, ts, mission_id),
            )
            _append_event(
                con,
                mission_id,
                "archive",
                at=ts,
                meta={
                    "phase": "finish",
                    "not_archived": [f["session_key"] for f in failures],
                    "skipped": skipped,
                },
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {
        "mission_id": mission_id,
        "archived": True,
        "not_archived": failures,
        "skipped": skipped,
    }


def begin_unarchive(
    mission_id: str,
    *,
    sessions: bool = True,
    resume: bool = False,
    now: float | None = None,
    path: Path | None = None,
) -> dict:
    """Claim the unarchive **before** any external effect. One transaction.

    Unarchive used to restore every provider session first and clear ``archived_at`` afterwards.
    A crash between those two left live, unarchived sessions under a mission still recorded as
    archived — and boot recovery only looked at ``archiving_at``, so the torn state was invisible
    and permanent. Worse, a repeated or concurrent request mutated the providers and only *then*
    discovered it had lost, so the losing caller still moved real files.

    Two things the first version got wrong, both fixed here:

    * **The claim is exclusive.** A second *request* is refused (409) rather than handed
      ``resumed=True`` and allowed to walk the same provider restores in parallel — which is how
      one caller reported success while the other reported a provider failure for the same
      session. Only :func:`resume=True`, which boot recovery passes, may take over a claim —
      safe for the reason `begin_archive` gives: the mission-level claim is not the gate on the
      destructive effect, the per-session lease is, and that one is taken over only on a proven
      expiry.
    * **The claim carries the mode.** ``sessions`` is stored with it, so recovery finishes *this*
      operation rather than a differently-shaped one. Without it, a crash from ``sessions=False``
      came back with the sessions unarchived anyway.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT archived_at, archiving_at, unarchiving_at, unarchive_sessions "
                "FROM missions WHERE id=?",
                (mission_id,),
            ).fetchone()
            if row is None:
                raise MissionNotFound(mission_id)
            if row["archiving_at"] is not None and row["archived_at"] is None:
                con.execute("ROLLBACK")
                raise MissionError(f"mission {mission_id}: archive in progress", status=409)
            if row["unarchiving_at"] is not None:
                if not resume:
                    con.execute("ROLLBACK")
                    raise MissionError(
                        f"mission {mission_id}: unarchive already in progress", status=409
                    )
                token = _new_op_token()
                con.execute("UPDATE missions SET op_token=? WHERE id=?", (token, mission_id))
                con.execute("COMMIT")
                return {
                    "mission_id": mission_id,
                    "resumed": True,
                    # The mode the CLAIM was made with, not the one this caller asked for.
                    "sessions": bool(row["unarchive_sessions"]),
                    "op_token": token,
                    "retry_only": row["archived_at"] is None,
                }
            outstanding = con.execute(
                "SELECT COUNT(*) FROM mission_sessions "
                "WHERE mission_id=? AND archive_state='restore_failed'",
                (mission_id,),
            ).fetchone()[0]
            if row["archived_at"] is None and not outstanding:
                con.execute("ROLLBACK")
                raise MissionError(f"mission {mission_id} is not archived", status=409)
            # A mission that is already back but still owes restores may claim again, to retry
            # exactly those. Without this the partial-failure contract stranded them: the
            # mission-level gate is what lets a restore be attempted, and clearing it on the way
            # out meant "retry" answered "not archived" forever. Fencing the mission until every
            # session came back is the other trap — one un-restorable session would park the
            # record for the life of the process.
            retry_only = row["archived_at"] is None
            token = _new_op_token()
            cur = con.execute(
                "UPDATE missions SET unarchiving_at=?, unarchive_sessions=?, op_token=? "
                "WHERE id=? AND unarchiving_at IS NULL",
                (ts, 1 if sessions else 0, token, mission_id),
            )
            if not cur.rowcount:
                con.execute("ROLLBACK")
                raise MissionError(f"mission {mission_id}: unarchive already claimed", status=409)
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {
        "mission_id": mission_id,
        "resumed": False,
        "sessions": sessions,
        "op_token": token,
        "retry_only": retry_only,
    }


def claim_session_restore(
    mission_id: str, session_key: str, *, now: float | None = None, path: Path | None = None
) -> tuple[str, str | None]:
    """Lease one session's provider restore, mirroring :func:`claim_session_teardown`.

    Restores are external effects too, and the reproduction that motivated this had two callers
    running ``prov.unarchive`` on the same session — one reporting success, the other a provider
    failure for work that had already happened.
    """
    ts = time.time() if now is None else now
    try:
        token = reserve_session(session_key, f"mission:{mission_id}", now=now, path=path)
    except SessionBusy:
        return "taken", None
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute(
                "SELECT archive_state FROM mission_sessions WHERE mission_id=? AND session_key=?",
                (mission_id, session_key),
            ).fetchone()
            if row is None:
                con.execute("COMMIT")
                release_session(session_key, token, path=path)
                return "gone", None
            # `restore_failed` is eligible on purpose: that is what a retry pass picks up, and
            # it is the reason the row stays reserved rather than being released as "done with".
            if row["archive_state"] not in ("done", "already_archived", "restore_failed"):
                con.execute("COMMIT")
                release_session(session_key, token, path=path)
                return "taken", None
            # Ownership is re-checked HERE too, not just on the teardown side: restoring a
            # session another mission has since taken would move the provider files underneath
            # a live holder.
            holder = con.execute(
                "SELECT mission_id FROM mission_sessions "
                "WHERE session_key=? AND removed_at IS NULL AND mission_id != ? LIMIT 1",
                (session_key, mission_id),
            ).fetchone()
            if holder is not None:
                con.execute(
                    "UPDATE mission_sessions SET archive_state='skipped', archive_error=? "
                    "WHERE mission_id=? AND session_key=?",
                    (f"held by mission {holder['mission_id']}", mission_id, session_key),
                )
                con.execute("COMMIT")
                release_session(session_key, token, path=path)
                return "skipped", None
            con.execute(
                "UPDATE mission_sessions SET archive_state='restoring', lease_owner=?, "
                "lease_at=?, lease_token=? WHERE mission_id=? AND session_key=?",
                (PROCESS_EPOCH, ts, token, mission_id, session_key),
            )
            _append_event(
                con,
                mission_id,
                "archive",
                at=ts,
                session_key=session_key,
                meta={"phase": "restore", "outcome": "claimed"},
            )
            con.execute("COMMIT")
            return "claimed", token
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()


def settle_session_restore(
    mission_id: str,
    session_key: str,
    outcome: str,
    *,
    reason: str = "",
    token: str | None = None,
    now: float | None = None,
    path: Path | None = None,
) -> None:
    """Settle one restore. Requires the claim's fencing token, for the reason in
    :func:`settle_session_archive`: a worker that finishes late must not settle the operation
    that replaced it."""
    if outcome not in ("restored", "restore_failed"):
        raise MissionError(f"unknown restore outcome {outcome!r}", status=422)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            won = con.execute(
                "UPDATE mission_sessions SET archive_state=?, archive_error=?, lease_owner=NULL, "
                "lease_at=NULL, lease_token=NULL "
                "WHERE mission_id=? AND session_key=? AND archive_state='restoring' "
                "AND (? IS NULL OR lease_token IS NULL OR lease_token = ?)",
                (
                    None if outcome == "restored" else "restore_failed",
                    None if outcome == "restored" else _cap_or_none(reason, 300),
                    mission_id,
                    session_key,
                    token,
                    token,
                ),
            ).rowcount
            if not won:  # a fenced-out worker publishes nothing — see `settle_session_archive`
                con.execute("COMMIT")
                return
            _append_event(
                con,
                mission_id,
                "archive",
                at=ts,
                session_key=session_key,
                meta={"phase": "restore", "outcome": outcome, "reason": _cap(reason, 300)},
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    if token and won:
        release_session(session_key, token, path=path)


def finish_unarchive(
    mission_id: str, *, now: float | None = None, path: Path | None = None
) -> dict:
    """Clear the archive stamps and report what did not come back.

    **The explicit partial-failure contract.** A session that will not restore does not hold the
    mission hostage: the mission un-archives and the roster names the session, exactly as archive
    is honest about a session it could not reap. Keeping the whole mission fenced on one
    un-restorable session is the *other* failure this phase already fixed once — a transient
    error must never park a record for the life of the process. Those rows keep
    ``archive_state='restore_failed'``, so a later unarchive retries precisely them.
    """
    validate_id(mission_id)
    ts = time.time() if now is None else now
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            open_leases = [
                r["session_key"]
                for r in con.execute(
                    "SELECT session_key FROM mission_sessions "
                    "WHERE mission_id=? AND archive_state='restoring'",
                    (mission_id,),
                ).fetchall()
            ]
            if open_leases:
                # An open restore lease is not a failure to record — it is a reason not to finish.
                # Converting it to `restore_failed` and finishing anyway is how recovery came to
                # report a mission unarchived after invoking no provider restore at all.
                con.execute("ROLLBACK")
                raise MissionError(
                    f"mission {mission_id}: {len(open_leases)} session restore(s) still open",
                    status=409,
                )
            not_restored = [
                dict(r)
                for r in con.execute(
                    "SELECT session_key, archive_error FROM mission_sessions "
                    "WHERE mission_id=? AND archive_state='restore_failed'",
                    (mission_id,),
                ).fetchall()
            ]
            # `archived_at` is cleared only on the pass that was actually un-archiving. A
            # retry-only pass (the mission is already back, and is finishing outstanding restores)
            # must leave the rest of the record alone.
            con.execute(
                "UPDATE missions SET archived_at=NULL, archiving_at=NULL, unarchiving_at=NULL, "
                "unarchive_sessions=NULL, op_token=NULL, updated_at=? WHERE id=?",
                (ts, mission_id),
            )
            _append_event(
                con,
                mission_id,
                "archive",
                at=ts,
                meta={
                    "phase": "unarchive",
                    "not_restored": [r["session_key"] for r in not_restored],
                },
            )
            con.execute("COMMIT")
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        finally:
            con.close()
    return {"mission_id": mission_id, "archived": False, "not_restored": not_restored}


def pending_unarchives(*, path: Path | None = None) -> list[str]:
    """Unarchives claimed and never finished — the other half of the boot worklist."""
    con = _ready(path)
    try:
        return [
            r["id"]
            for r in con.execute(
                "SELECT id FROM missions WHERE unarchiving_at IS NOT NULL ORDER BY unarchiving_at"
            ).fetchall()
        ]
    finally:
        con.close()


def archive_roster_preview(mission_id: str, *, path: Path | None = None) -> list[str]:
    """The sessions a NEW :func:`begin_archive` would tear down — its rules, not the last one's.

    :func:`sessions_governed_by_archive` answers a different question (what the archive that
    already ran governs) and excludes rows a previous attempt left ``skipped``. ``begin_archive``
    **clears those skips and re-evaluates them**, so a preview built on that helper can promise
    "0 sessions, 0 live terminals" and then archive a session and stop its terminal — the
    lifecycle Hermes reproduced on PR #1000 (archive A while B holds its session → unarchive A
    without its sessions → finish B → bulk-archive A).

    So this mirrors exactly the two predicates ``begin_archive`` applies when it builds the
    pending set: the whole roster except what the operator DETACHED, minus whatever another
    mission still actively holds (those are marked ``skipped`` and left to that mission).
    Read-only.
    """
    validate_id(mission_id)
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT session_key FROM mission_sessions "
            "WHERE mission_id=? AND COALESCE(release_reason,'closed') != 'detached' "
            "AND session_key NOT IN ("
            "  SELECT session_key FROM mission_sessions "
            "  WHERE removed_at IS NULL AND mission_id != ?) "
            "ORDER BY added_at ASC",
            (mission_id, mission_id),
        ).fetchall()
        return [r["session_key"] for r in rows]
    finally:
        con.close()


def archive_candidates(
    older_than_s: float, *, now: float | None = None, path: Path | None = None
) -> list[dict]:
    """Missions Settings → Maintenance may offer to archive in bulk (#993), oldest first.

    Terminal state only, not archived and not mid-archive, and last closed (or, failing that,
    updated) more than ``older_than_s`` ago. Each row carries ``unresolved`` — an in-progress
    turn, which ``begin_archive`` will refuse — so a dry run can say so before anything runs.
    Read-only; the archive itself still goes through ``begin_archive``'s own fences.
    """
    cutoff = (time.time() if now is None else now) - older_than_s
    states = sorted(TERMINAL_STATES)
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT id, state, COALESCE(closed_at, updated_at) AS ended_at FROM missions "  # noqa: S608 — placeholders only
            "WHERE archived_at IS NULL AND archiving_at IS NULL "
            f"AND state IN ({', '.join('?' for _ in states)}) "
            "AND COALESCE(closed_at, updated_at) < ? "
            "ORDER BY ended_at ASC",
            (*states, cutoff),
        ).fetchall()
        unresolved = {
            r["mission_id"]
            for r in con.execute(
                "SELECT DISTINCT mission_id FROM mission_turns WHERE state='in_progress'"
            ).fetchall()
        }
    finally:
        con.close()
    return [
        {
            "id": r["id"],
            "state": r["state"],
            "ended_at": r["ended_at"],
            "unresolved": r["id"] in unresolved,
        }
        for r in rows
    ]


def pending_archives(*, path: Path | None = None) -> list[str]:
    """Missions whose archive began and never finished — the boot reconciliation worklist."""
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT id FROM missions WHERE archiving_at IS NOT NULL AND archived_at IS NULL "
            "ORDER BY archiving_at ASC"
        ).fetchall()
        return [r["id"] for r in rows]
    finally:
        con.close()


def archive_sessions_for(mission_id: str, *, path: Path | None = None) -> list[dict]:
    con = _ready(path)
    try:
        rows = con.execute(
            "SELECT session_key, role, archive_state, archive_error, lease_owner "
            "FROM mission_sessions WHERE mission_id=? ORDER BY added_at ASC",
            (mission_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


# ---------------------------------------------------------------- retention


#: The predicate that decides what retention may delete. Repeated verbatim at BOTH the selection
#: and the mutation, because a mission can enter an operation between the two — and deleting one
#: mid-archive destroys the journal that recovery needs, along with the roster naming the agents
#: it was about to reap.
class _NoVictims(Exception):
    """Nothing old enough to prune — but an outstanding scrub may still be owed."""


# …AND NOTHING IS STILL OWED FOR IT (#904 review 17, finding 1). `mission_dispatches` is
# `ON DELETE CASCADE`, and a row sitting beside an ALREADY-TERMINAL mission means exactly one
# thing: a teardown could not prove the process boundary empty, so that row is the only durable
# trace of an agent that may still be running. Retention deleting the mission took the obligation
# with it, and recovery can only retry what it can still find — so the leak became permanent and
# invisible at the moment the record was needed most.
#
# Expressed IN the eligibility predicate rather than beside it, because this constant is repeated
# on the DELETE precisely so selection and deletion cannot disagree; a guard applied only at
# selection would be the check-then-act that comment is about.
_RETENTION_ELIGIBLE = (
    "closed_at IS NOT NULL AND closed_at < ? "
    "AND archiving_at IS NULL AND unarchiving_at IS NULL "
    "AND NOT EXISTS (SELECT 1 FROM mission_dispatches d WHERE d.mission_id = missions.id)"
)


def retention_pass(
    *, now: float | None = None, days: int | None = None, path: Path | None = None
) -> int:
    """Delete closed missions older than the retention window. Bounded; returns rows deleted.

    A **row delete**, not a tombstone: the rows carry the operator's verbatim instruction, so
    "deleted" has to mean the bytes are gone. Bounded per pass so a long-idle install cannot turn
    one request into a table scan.

    **Selection and deletion happen in one transaction**, with the eligibility predicate repeated
    on the DELETE. Selecting outside it and deleting by captured id was a check-then-act: a
    mission could be reopened, or an archive could begin on it, between the two — and the delete
    would still take it, destroying the operation journal that boot recovery needs.

    Raises :class:`ScrubFailed` when the rows are gone but the log could not be truncated: this
    deletes sensitive text, so it reports what it actually achieved.
    """
    window = _retention_days() if days is None else max(1, int(days))
    cutoff = (time.time() if now is None else now) - window * 86400
    with _write_lock:
        con = _ready(path)
        try:
            con.execute("BEGIN IMMEDIATE")
            victims = [
                r["id"]
                for r in con.execute(
                    f"SELECT id FROM missions WHERE {_RETENTION_ELIGIBLE} "  # noqa: S608
                    "ORDER BY closed_at ASC LIMIT ?",
                    (cutoff, RETENTION_BATCH),
                ).fetchall()
            ]
            if not victims:
                con.execute("COMMIT")
                deleted = 0
                referenced = []
                raise _NoVictims
            referenced = [
                r["action_id"]
                for r in con.execute(
                    "SELECT DISTINCT action_id FROM mission_events "  # noqa: S608
                    f"WHERE action_id IS NOT NULL AND mission_id IN ({_marks(victims)})",
                    tuple(victims),
                ).fetchall()
            ]
            deleted = (
                con.execute(
                    f"DELETE FROM missions WHERE id IN ({_marks(victims)}) "  # noqa: S608
                    f"AND {_RETENTION_ELIGIBLE}",
                    (*victims, cutoff),
                ).rowcount
                or 0
            )
            _gc_settlements(con, referenced)
            con.execute("COMMIT")
        except _NoVictims:
            pass
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                con.execute("ROLLBACK")
            raise
        try:
            # Scrub when this pass deleted something OR when an earlier one left the obligation.
            owed = deleted or _flag_get(con, SCRUB_PENDING) is not None
            if owed and not _scrub(con):
                raise ScrubFailed(f"{deleted} retired mission(s)" if deleted else "an earlier one")
        finally:
            con.close()
    return deleted


def _marks(items) -> str:
    """A `?` run sized by a list — every value is still bound."""
    return ",".join("?" * len(items))


# ---------------------------------------------------------------- fail-soft read wrappers


def safe_list_missions(**kw) -> dict:
    """:func:`list_missions`, degraded rather than fatal.

    A locked or corrupt store empties the rail and **says why**, exactly as opencode's reader
    degrades the sidebar — it never takes the console down. Writes deliberately do NOT get this
    treatment: a silently dropped adopt or archive is indistinguishable from one that worked.
    """
    try:
        out = list_missions(**kw)
        out["store_error"] = None
        return out
    except (sqlite3.Error, MissionError, OSError) as e:
        return {
            "missions": [],
            "total": 0,
            "limit": kw.get("limit") or LIST_LIMIT_DEFAULT,
            "offset": kw.get("offset") or 0,
            "facets": {"projects": [], "states": []},
            "store_error": _store_reason(e),
            # NO SNAPSHOT, because there was no read. A client stitching pages must treat a
            # missing digest as "cannot prove one snapshot" rather than as one more page that
            # happened to match — the degraded answer is the one case where it certainly did not.
            "snapshot": None,
        }


def safe_get_mission(mission_id: str, **kw) -> dict | None:
    try:
        return get_mission(mission_id, **kw)
    except (sqlite3.Error, OSError):
        return None


def _store_reason(exc: BaseException) -> str:
    """A stated reason that never carries mission content.

    ``sqlite3`` messages are about the *file* ("database is locked", "database disk image is
    malformed"), never about a row, so they are safe to surface. Anything else is reported by
    type only.
    """
    if isinstance(exc, sqlite3.Error):
        return f"missions store unavailable: {exc}"
    if isinstance(exc, MissionError):
        return str(exc)
    return f"missions store unavailable: {type(exc).__name__}"


__all__ = [
    "MissionError",
    "MissionNotFound",
    "MissionsBusy",
    "ScrubFailed",
    "SessionHeld",
    "STATES",
    "TERMINAL_STATES",
    "CWD_OPTIONAL_STATES",
    "EVENT_KINDS",
    "PROBE_KINDS",
    "LEASE_MAX_AGE_S",
    "PROCESS_EPOCH",
    "RESERVED_ARCHIVE_STATES",
    "OBJECTIVE_STATES",
    "SCHEMA_VERSION",
    "SETTLEMENT_LOST",
    "acquire",
    "active_session_keys",
    "adopt",
    "append_event",
    "all_active_memberships",
    "OwnershipUnknown",
    "RESERVATION_MAX_AGE_S",
    "SessionBusy",
    "release_session",
    "reservation_of",
    "reserve_session",
    "archive_sessions_for",
    "begin_archive",
    "begin_unarchive",
    "create_mission",
    "delete_mission",
    "derive_needs_you",
    "detach",
    "event_count",
    "executor",
    "claim_session_restore",
    "claim_session_teardown",
    "finish_archive",
    "finish_unarchive",
    "get_mission",
    "holder_of",
    "inflight_for_test",
    "list_missions",
    "new_id",
    "objectives",
    "patch_objectives",
    "pending_archives",
    "pending_unarchives",
    "record_settlement",
    "record_settlements",
    "referenced_action_ids",
    "settlements_for",
    "reopen_stale_leases",
    "reset_schema_cache_for_test",
    "retention_pass",
    "run_admitted",
    "safe_get_mission",
    "scrub_if_pending",
    "safe_list_missions",
    "set_state",
    "settle_session_archive",
    "settle_session_restore",
    "strict_bool",
    "shutdown_executor_for_test",
    "unmet_gates",
    "validate_id",
]
