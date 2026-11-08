"""Pulse orchestrator action ledger (#726 Phase 1).

An autonomous system with no audit trail is unreviewable, so the ledger is a Phase-1
deliverable rather than an afterthought: every proposal, decision and delivery outcome lands
here with its rationale, and the Pulse feed renders straight off it.

**Append-only event log, reduced on read.** Each line is one immutable event carrying an
``id`` (the action) and a ``state``; the current state of an action is its newest event. That
shape is what makes the crash semantics honest — a state transition is a single ``write()`` of
a single line, so a process that dies mid-append leaves a *torn tail* rather than a corrupted
record, and :func:`read_all` discards it. Nothing is ever rewritten in place, so no crash can
half-apply a transition.

**The state machine** (Phase 2 drives the delivery half)::

    proposed ─┬─► claimed ─┬─► delivered        the PTY writes succeeded (see below)
              │            ├─► failed           write refused / aborted mid-write
              │            └─► indeterminate    crashed after the write, before the record
              ├─► approved ─► claimed …         operator tapped approve
              ├─► rejected                      operator declined
              ├─► escalated                     the model asked a QUESTION — reject-only
              ├─► escalated_low_confidence ─► claimed …
              │                                 it wanted to ACT and was unsure — approvable
              ├─► stale                         precondition moved before delivery
              └─► expired                       TTL elapsed untouched

**``delivered`` is a statement about the WRITE, not about the agent** (#801 Phase 5). It means
every byte of the payload was accepted by the PTY and nothing refused, aborted or raced the
write. It does **not** mean the agent read those bytes, submitted a turn, or did anything at
all — the ledger has no way to observe that, and must not be read as claiming it.

That gap is measured, not theoretical. The #801 harness drives a real engine through this exact
path and then counts committed user turns in the engine's own transcript store: **gemini 0.57.0
reports ``delivered``, puts the nudge text on screen, and never commits a turn** — reproduced
across trusted and untrusted folders, with ``bypass=True``, and with both the bundled and split
carriage-return forms. The same run shows claude 2.1.247 submitting normally, so this is a
per-engine property that a delivery outcome cannot express. Two engines, same ``delivered``,
opposite results.

The practical consequence: **an autonomous nudge that reports ``delivered`` may have done
nothing**, and a caller that treats the state as proof of a turn will report success for a
no-op. Whether the agent acted has to be established from the engine's own store — never from
here. `Outcome.ok` in :mod:`session_input` carries the same warning at the other end.

``indeterminate`` is the load-bearing one. Terminal I/O cannot be exactly-once: if the process
dies after bytes reach the PTY but before the ``delivered`` event is durable, nothing on disk
can prove whether they landed. So a ``claimed`` action recovered at startup is **never**
auto-retried — :func:`recover_claimed` moves it to ``indeterminate`` for manual resolution.
The guarantee this ledger supports is **at-most-once**, and it says so rather than implying an
exactly-once it cannot deliver.

Evidence is recorded by *kind* only, never content: the panel re-fetches it live, so the
ledger never becomes a transcript archive (and never a place transcript text can leak from).
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from .atomicjson import fsync_dir

log = logging.getLogger("agent_sessions.orchestrator_ledger")

# Terminal states — an action here will never transition again, so compaction may drop it
# once it falls out of the history tail.
TERMINAL_STATES: frozenset[str] = frozenset(
    # `observed` is terminal: it is a note in the feed, and nothing further ever happens to it.
    # It was previously in NEITHER set, so it fell outside compaction's live/done partition.
    {"delivered", "failed", "indeterminate", "rejected", "stale", "expired", "observed"}
)
# States an action can sit in while still awaiting something (an operator tap, a delivery).
LIVE_STATES: frozenset[str] = frozenset(
    {"proposed", "approved", "claimed", "escalated", "escalated_low_confidence"}
)
ALL_STATES: frozenset[str] = TERMINAL_STATES | LIVE_STATES

#: The TWO ROADS INTO AN ESCALATION, which used to be one state carrying two meanings (#877).
#:
#: * ``escalated`` — the MODEL asked a question. Its verb is ``escalate``; there is nothing to
#:   run, so the only answers are dismiss or ignore.
#: * ``escalated_low_confidence`` — the model wanted to ACT and was not sure. Its verb is a real
#:   delivering one, so "yes" is a meaningful answer and the operator may give it.
#:
#: Conflating them was the bug: the operator was shown a runnable action, asked to look at it,
#: and given no way to say yes — the delivery path refused `escalated` and the button 409'd.
#:
#: **This set exists so the meaning is named once rather than string-matched an eighth time.**
#: Seven exact `== "escalated"` comparisons already decided things across the server and the
#: client — whether a row is announced, whether it is counted, its tone, its ARIA label, whether
#: its reason is shown. Each one that missed a new state would fail SILENTLY and differently.
#: Anything asking "is this row an escalation, whichever kind?" asks this; the two-kind
#: distinction is made only where it genuinely differs — the controls (from the projection) and
#: the operator-facing wording.
ESCALATION_STATES: frozenset[str] = frozenset({"escalated", "escalated_low_confidence"})

# States in which an action is waiting on the OPERATOR — the only ones that should ever put
# decision controls on a Pulse card or a row under "Needs a decision". Deliberately excludes
# `claimed`: a claimed action is already being delivered, so offering Approve/Reject for it
# invites a tap that cannot be honoured. It coincides with `REJECTABLE_STATES` below, and for
# the same reason, but they answer different questions — keep both named.
OPERATOR_PENDING_STATES: frozenset[str] = frozenset({"proposed", "approved"} | ESCALATION_STATES)

#: The verbs whose DELIVERY ends the session's current HQ report (#1882): text the agent reads as
#: an answer (a report option's `Decision: …`, HQ's proposed text, the operator's own words).
REPORT_ENDING: frozenset[str] = frozenset({"answer", "relay"})


class LedgerUnreadable(Exception):
    """The ledger exists but could not be read: whether an answer was sent is UNKNOWN (#1882)."""


def report_answer_spent(
    report_id: str, path: Path | None = None, *, exclude: str | None = None
) -> bool:
    """Has an answer to this HQ report already been SENT, or may it have been? (#1882)

    The ledger's half of the one-answer rule; the durable half is the report's own reservation
    (`agent_reports.mark_answered`), which compaction cannot evict. Once a `source: "report"`
    action bound to ``report_id`` reached ``claimed`` (its ``claim_owner`` survives every later
    event), its bytes may be on the pty —
    unless it settled ``stale``, which every post-claim path only reaches before byte one (a
    report check, a guard or a fingerprint that refused). ``delivered``, ``failed`` (a partial
    write included), ``indeterminate`` and a claim still in flight all count. ``exclude`` skips
    the asking action itself. A plain read; called from an ``append_batch_for_free_sessions``
    ``record_guard`` it is evaluated inside the writer lock, so two taps cannot both pass.

    STRICT: a ledger that exists and cannot be read raises `LedgerUnreadable` — an admission
    decision never reads "unknown" as "no earlier answer"."""
    if not report_id:
        return False
    status, latest = latest_by_id_checked(path)
    if status != "ok":
        raise LedgerUnreadable("the decision ledger could not be read")
    for rec in latest.values():
        if rec.get("source") != "report" or rec.get("report_id") != report_id:
            continue
        if exclude is not None and rec.get("id") == exclude:
            continue
        if not rec.get("claim_owner") and rec.get("state") != "claimed":
            continue  # never claimed: nothing was ever on its way to the pty
        if rec.get("state") == "stale":
            continue  # refused before byte one
        return True
    return False


def _proposed_at(rec: dict, first_ts: float | None) -> float | None:
    """When an action was PROPOSED, as the report end binds it: the earliest of its recorded
    ``proposed_ts`` (compaction keeps it, `_compact_locked`), its first surviving event and its
    precondition's screen read."""
    pre = rec.get("precondition")
    cands = [
        rec.get("proposed_ts"),
        first_ts,
        pre.get("observed_at") if isinstance(pre, dict) else None,
    ]
    times = [float(t) for t in cands if isinstance(t, int | float) and not isinstance(t, bool)]
    return min(times) if times else None


def _spends_report(rec: dict, first_ts: float | None, phys_key: str, report: dict) -> bool:
    """Could this TERMINAL action spend ``report`` (the session's current one)? Its own option's
    answer that the one-answer rule counts as spent, or an ordinary answer/relay delivered to the
    session and proposed after the report."""
    if rec.get("source") == "report":
        # Exactly what the one-answer rule counts as spent (`report_answer_spent`): claimed, and
        # not settled as a clean pre-byte `stale`. A clean refusal, an expiry or a never-claimed
        # failure spends nothing — pinning it would grow the ledger with every refused tap.
        return (
            rec.get("report_id") == report.get("id")
            and bool(rec.get("claim_owner") or rec.get("state") == "claimed")
            and rec.get("state") != "stale"
        )
    if rec.get("state") != "delivered" or rec.get("verb") not in REPORT_ENDING:
        return False
    if str((rec.get("authority") or {}).get("physical_key") or "") != phys_key:
        return False
    when = _proposed_at(rec, first_ts)
    return when is not None and when > float(report.get("at") or 0)


def answered_after(
    phys_key: str, report_at: float, path: Path | None = None, *, exclude: str | None = None
) -> bool:
    """Did an ordinary `answer` / `relay` (HQ's text, the orchestrator's — not a report option)
    get DELIVERED to this session after a report filed at ``report_at`` — proposed after it?
    (#1882, Hermes on #1884.) Such a delivery ends the report (`_end_answered_report`), but
    asynchronously and best-effort; until that commits, or if it fails, this is what keeps an
    option tap from sending a SECOND answer. Proposal time as the end binds it: the earliest of
    the action's first surviving event and its precondition's screen read. STRICT: raises
    `LedgerUnreadable`."""
    p = _path(path)
    if not p.exists():
        return False
    try:
        raw = p.read_text(errors="replace")
    except OSError:
        raise LedgerUnreadable("the decision ledger could not be read") from None
    records = _parse_records(raw)
    first: dict[str, float] = {}
    for r in records:
        t = r.get("ts")
        if r["id"] not in first and isinstance(t, int | float) and not isinstance(t, bool):
            first[r["id"]] = float(t)
    for rec in _latest_of(records).values():
        if rec.get("state") != "delivered" or rec.get("verb") not in REPORT_ENDING:
            continue
        if rec.get("source") == "report" or rec.get("id") == exclude:
            continue  # a report's own answer is the one-answer rule's (`report_answer_spent`)
        if str((rec.get("authority") or {}).get("physical_key") or "") != phys_key:
            continue
        when = _proposed_at(rec, first.get(rec["id"]))
        if when is not None and when > report_at:
            return True
    return False


def delivered_report_ids(path: Path | None = None) -> set[str]:
    """The report ids a `source: "report"` answer was DELIVERED for — the needs-you sync's
    self-heal ends any of them still current (`hq_report_broker.reconcile_reports`). STRICT: an
    unreadable ledger raises `LedgerUnreadable` (it would otherwise read as "none delivered")."""
    status, latest = latest_by_id_checked(path)
    if status != "ok":
        raise LedgerUnreadable("the decision ledger could not be read")
    return {
        str(rec["report_id"])
        for rec in latest.values()
        if rec.get("source") == "report"
        and rec.get("state") == "delivered"
        and isinstance(rec.get("report_id"), str)
        and rec["report_id"]
    }


# The only states a reject may move FROM. Deliberately excludes `claimed`: once a delivery has
# claimed an action the bytes are already going out, so "rejected" would be a lie the operator
# acts on. It also excludes every terminal state — rejecting a `delivered` action would rewrite
# history into something that never happened.
REJECTABLE_STATES: frozenset[str] = frozenset({"proposed", "approved"} | ESCALATION_STATES)

# --- the operator-facing projection -----------------------------------------------------------
#
# One action state, ONE projection of it, computed HERE and consumed everywhere (#852/#840 §16).
#
# The three sets above overlap by accident rather than by design: `OPERATOR_PENDING_STATES` and
# `REJECTABLE_STATES` are literally identical today, and `actuator.CLAIMABLE_STATES` is a third,
# different set. Gating controls on any one of them alone gets `approved` wrong (it is reject-only
# yet still deliverable) and `claimed` wrong (it is live, not terminal). Three surfaces re-deriving
# the answer from three sets that already disagree is how they drift, and the drift is invisible
# until a control appears that cannot be honoured.
#
# "Compute it server-side" is not sufficient, because it still lets three *server* handlers derive
# it separately. So it is one function, and every producer calls it.

#: The five projections. `historical` is deliberately distinct from `settled`: an action the ledger
#: no longer holds has no outcome to assert, and claiming one would invent a fact.
ACTIONABLE = "actionable"
IN_FLIGHT_REVOCABLE = "in_flight_revocable"
IN_FLIGHT_LOCKED = "in_flight_locked"
SETTLED = "settled"
HISTORICAL = "historical"
#: The ledger could not be READ. Distinct from `historical` (read fine, action not there) —
#: see :func:`project_for_operator`. #840 §16 has no row for this because it assumes the store
#: always answers.
UNKNOWN = "unknown"


#: The only states an action may be CLAIMED (delivered) from. Lives here, not in
#: :mod:`actuator`, because :func:`project_for_operator` must gate ``can_approve`` on the very
#: set the delivery path enforces — and ``actuator`` already imports this module, so the reverse
#: import is impossible. Two copies of this set is precisely the drift that shipped an Approve
#: button the backend answers with ``409 NotDeliverable``.
#:
#: ``escalated_low_confidence`` is a member and plain ``escalated`` is NOT, which is the whole
#: of #877: a low-confidence action is a real runnable one the operator was asked about, so
#: their yes must be honourable; a model-escalated question has no verb to run. This widens what
#: may be CLAIMED and deliberately does not widen what may be claimed **without a decision** —
#: `orchestrator._decide` reaches the new state only on a path that by construction did not return
#: `approved`, so nothing auto-approves into it.
CLAIMABLE_STATES: frozenset[str] = frozenset({"proposed", "approved", "escalated_low_confidence"})


def project_for_operator(state: str | None, *, known: bool = True) -> dict:
    """How one action should be rendered wherever a decision is shown. The whole contract.

    ``state`` is the ledger state, or ``None`` when the action is absent from the ledger — which
    is a real answer, not a missing one: a compacted action is *historical*, and a surface that
    renders it must offer no controls and assert no outcome.

    Returns a fixed wire shape so the contract is checkable rather than inferred::

        {"projection": ..., "can_approve": bool, "can_reject": bool, "state": str | None}

    ``known=False`` is the *other* kind of missing: the ledger could not be read at all, so no
    conclusion about this action is available. Pass it rather than ``state=None``, which asserts
    the action is genuinely gone.

    Callers **must not** re-derive any part of this — that is the entire point of it being here.
    """
    if not known:
        # The store did not answer. That is NOT the same as answering "absent", and collapsing
        # the two is how a transient permission error disarmed a live escalation: absent projects
        # as `historical` — no controls, no badge — so an unreadable ledger silently retired
        # every decision the operator had waiting.
        #
        # NO controls — not even Reject, which an earlier revision of this offered.
        #
        # The reasoning that offered it was: the row still wants the operator, so give them
        # something to do. That is right about the row and wrong about the control, and the
        # deciding fact is the rule this very module established one commit earlier — **the
        # projection may not advertise a control the backend refuses.** Reject cannot be
        # honoured during this outage either: `compare_and_set` and `get` read through the same
        # `_read_all_at`, which turns the same `OSError` into an empty ledger, so the route finds
        # no record and answers **404 "unknown action"**. Offering it produces a tap that reports
        # the action never existed — strictly worse than offering nothing, because it invites the
        # operator to conclude the decision is gone.
        #
        # What the operator gets instead is the truth: the row stays visible, it keeps counting
        # toward the badge (`_counts_toward_badge` treats an outage as "show, do not guess"), and
        # `unknown` tells the surface to say *why* it cannot be acted on. When the store reads
        # again the controls come back on their own, with no operator action.
        #
        # This is NOT the un-clearable badge of #852 rule 5. That rule is about rows which are
        # permanently finished — delivered, claimed, settled — inflating a count for ever. An
        # outage is transient and the count is true while it lasts: something *is* outstanding.
        return {"projection": UNKNOWN, "can_approve": False, "can_reject": False, "state": None}
    if state is None:
        return {"projection": HISTORICAL, "can_approve": False, "can_reject": False, "state": None}
    if state == "proposed" or state in ESCALATION_STATES:
        # All three want the operator, so all three are ACTIONABLE and count toward the badge.
        # Whether APPROVE is offered is decided by one thing and one thing only: membership of
        # :data:`CLAIMABLE_STATES`, the very set `actuator.deliver` enforces. That identity is
        # what makes "the console never advertises a control the backend refuses" a structural
        # property rather than a convention two files have to keep agreeing on.
        #
        # #840 §16 tabled `escalated` as "Approve + Reject" while the same paragraph stated
        # `CLAIMABLE_STATES` was `{proposed, approved}` — a contradiction four lines apart. The
        # console shipped the button, the tap 409'd, and the operator learned the console lies.
        #
        # #877 fixed the underlying conflation rather than the button. There were two roads into
        # `escalated` and they wanted different answers: a model QUESTION (`verb == "escalate"`)
        # has nothing to run, while a low-confidence action kept a real delivering verb — so
        # "yes" was always a meaningful answer to the second and there was no way to give it.
        # They are now two states, and this stays a pure state → controls table: the verb was
        # consulted once, where the state was decided, rather than at every surface that reads
        # it. A projection that took the verb too would be checkable only by enumerating a
        # product, which is exactly the property #840 §16 exists to have.
        return {
            "projection": ACTIONABLE,
            "can_approve": state in CLAIMABLE_STATES,
            "can_reject": True,
            "state": state,
        }
    if state == "approved":
        # In flight and still revocable. Approving again is a no-op, so the control is withdrawn;
        # rejecting is not, because delivery has not claimed it yet.
        return {
            "projection": IN_FLIGHT_REVOCABLE,
            "can_approve": False,
            "can_reject": True,
            "state": state,
        }
    if state == "claimed":
        # The bytes are going out. Neither control can be honoured, so neither is offered.
        return {
            "projection": IN_FLIGHT_LOCKED,
            "can_approve": False,
            "can_reject": False,
            "state": state,
        }
    return {"projection": SETTLED, "can_approve": False, "can_reject": False, "state": state}


# States expiry may act on. Excludes `claimed` for the same reason as reject: once a delivery
# has claimed an action, the bytes are on their way and "expired" would be a lie.
EXPIRABLE_STATES: frozenset[str] = frozenset({"proposed", "approved"} | ESCALATION_STATES)

# Compaction bounds. The live set is kept in full (it is small by construction — bounded by
# `max_actions_per_pass` per pass), plus a bounded tail of terminal actions for the feed.
HISTORY_MAX = 500
# Hard ceiling on lines before a read triggers compaction, so an append-only file cannot grow
# without bound between explicit compactions.
COMPACT_AT_LINES = 4000


@contextlib.contextmanager
def _locked(path: Path, *, shared: bool = False):
    """Serialise every ledger mutation through one persistent lock file.

    Append-only is crash-safe but NOT concurrency-safe on its own: ``compact()`` reads the
    ledger and later ``os.replace()``s it, so an ``append()`` landing in between writes to the
    now-unlinked old inode and vanishes — silently losing an expiry, approval, claim or
    recovery transition. The lock lives in a SIDECAR file, never the ledger itself, because the
    ledger's inode is exactly what compaction swaps.
    """
    lock = path.with_name(path.name + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _path(path: Path | None = None) -> Path:
    if path is not None:
        return path
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_ORCHESTRATOR_LEDGER",
            str(Path.home() / ".config" / "agent-sessions" / "orchestrator-ledger.jsonl"),
        )
    )


def append(record: dict, path: Path | None = None) -> dict:
    """Append one event. Returns the record as written (with ``ts`` filled in).

    One ``write()`` of one line, then ``fsync``. The single-write shape is the crash contract:
    a partial line is a torn tail the reader drops, never a mangled record it might act on.
    """
    p = _path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = dict(record)
    rec.setdefault("ts", time.time())
    line = json.dumps(rec, sort_keys=True) + "\n"
    with _locked(p):
        return _append_locked(p, rec, line)


def _append_locked(p: Path, rec: dict, line: str) -> dict:
    # 0600 from creation, not chmod-after: the ledger carries rationales about the operator's
    # work, and a widened-then-narrowed window is still a window.
    payload = line.encode("utf-8")
    # Whether THIS call creates the file decides if the directory needs syncing below (#728).
    created = not p.exists()
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        # POSIX permits a SHORT write, and a single `os.write` that returns fewer bytes leaves
        # a torn record — `{"id": "a1", "state": "claime` — which reads back as nothing. That
        # matters most for `claim()`: the caller is told the claim succeeded, delivery
        # proceeds, and after a restart the ledger has no durable record of it, which defeats
        # the at-most-once guarantee this file exists to provide. Loop to completion.
        written = 0
        try:
            while written < len(payload):
                n = os.write(fd, payload[written:])
                if n <= 0:
                    raise OSError("short write to the ledger made no progress")
                written += n
        except BaseException:
            # A partial record is worse than none: truncate back to the last good boundary so
            # the file stays parseable rather than ending mid-JSON.
            with contextlib.suppress(OSError):
                os.ftruncate(fd, os.lseek(fd, 0, os.SEEK_END) - written)
            raise
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        # `fsync(fd)` makes the BYTES durable; it says nothing about the directory entry that
        # names them. Without this, power loss right after the very first append can leave a
        # ledger whose contents were synced and whose link never existed — an at-most-once
        # guarantee that survives process death but not power loss (#728). Only on creation:
        # every later append writes into an already-durable name, so syncing the directory per
        # append would be pure cost.
        fsync_dir(p.parent)
    return rec


def read_all(path: Path | None = None) -> list[dict]:
    """Every well-formed event, oldest first.

    Torn-tail safe: a trailing partial line (crash mid-append) is discarded, as is any line
    that isn't a JSON object. A malformed line is *skipped*, never fatal — a damaged ledger
    must degrade to a shorter history, never take down the Pulse page.
    """
    return _read_all_at(_path(path))


def _parse_records(raw: str) -> list[dict]:
    """Records out of already-read ledger text. Split from :func:`_read_all_at` so a caller that
    must distinguish "unreadable" from "empty" can check the read itself and still share this
    parse — rather than reading the file a second time and giving it a second chance to change
    underneath the answer."""
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn tail or hand-edit — skip, never raise
        if isinstance(rec, dict) and isinstance(rec.get("id"), str):
            out.append(rec)
    return out


def _read_all_at(p: Path) -> list[dict]:
    if not p.exists():
        return []
    try:
        raw = p.read_text(errors="replace")
    except OSError:
        return []
    return _parse_records(raw)


def _read_all_strict(p: Path) -> list[dict]:
    """Every record, or `LedgerUnreadable` when the file exists and cannot be read. ABSENT and
    EMPTY are both ``[]``; a failed read (permission, I/O) is never mistaken for them — anything
    that REWRITES the ledger reads this way, so it can never replace history it could not read
    with nothing (#1882, Hermes on #1884)."""
    if not p.exists():
        return []
    try:
        raw = p.read_text(errors="replace")
    except OSError as e:
        raise LedgerUnreadable(f"the decision ledger could not be read ({type(e).__name__})") from e
    return _parse_records(raw)


def lookup(action_id: str, path: Path | None = None) -> tuple[str, dict | None]:
    """Tri-state read of one action: ``("found", rec)`` / ``("absent", None)`` /
    ``("unreadable", None)``.

    :func:`read_all` maps *every* ``OSError`` to an empty history — the right call for a feed,
    which must degrade rather than disappear, and the wrong one for anybody asking "does this
    action still exist?". Those callers get "no rows" for a transiently unreadable file and
    conclude the row was compacted away, which is a permanent answer to a temporary problem.

    **Reads exactly once.** A probe read followed by a second parsing read reintroduces the very
    hole it was meant to close: the probe succeeds, the second read fails, and the suppressed
    ``OSError`` becomes "absent" again. The bytes are read here and parsed here.

    An absent FILE is a genuine absence (a ledger nothing has written yet has no rows). A file
    that exists and will not read is unreadable, and the caller must not draw a conclusion from it.
    """
    p = _path(path)
    if not p.exists():
        return "absent", None
    try:
        raw = p.read_text(errors="replace")
    except OSError:
        return "unreadable", None
    merged: dict | None = None
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # torn tail or hand-edit — skip, never raise
        if isinstance(rec, dict) and rec.get("id") == action_id:
            # Merge forward, exactly as `_latest_by_id_locked` does: a transition event needn't
            # restate the whole proposal.
            merged = rec if merged is None else {**merged, **rec}
    return ("found", merged) if merged is not None else ("absent", None)


def latest_by_id_checked(path: Path | None = None) -> tuple[str, dict[str, dict]]:
    """Tri-state bulk read: ``("ok", mapping)`` / ``("unreadable", {})``.

    :func:`latest_by_id` cannot express "I could not read it": ``_read_all_at`` turns an
    ``OSError`` into ``[]``, which is indistinguishable from a ledger that genuinely holds
    nothing. A caller that reconciles rows against the ledger then reads a transient permission
    or I/O error as *every action is absent* — and "absent" is a real answer here, meaning
    ``historical``: no controls, no outcome asserted, out of the badge. A live escalation
    silently loses its buttons because a file briefly would not open.

    Same distinction :func:`lookup` already draws for one action, at the shape its bulk caller
    needs. A file that does not exist is a genuine absence (nothing has written a ledger yet);
    a file that exists and will not read is unreadable, and the caller must draw no conclusion.
    """
    p = _path(path)
    if not p.exists():
        return "ok", {}
    try:
        raw = p.read_text(errors="replace")
    except OSError:
        return "unreadable", {}
    return "ok", _latest_of(_parse_records(raw))


def latest_by_id(path: Path | None = None) -> dict[str, dict]:
    """Current state per action id — the newest event wins. Insertion order follows first
    appearance, so a caller iterating gets stable, roughly chronological output."""
    return _latest_by_id_locked(_path(path))


def latest_by_id_serialized(path: Path | None = None) -> dict[str, dict]:
    """:func:`latest_by_id`, but **taking the writer lock**.

    `latest_by_id` reads without it, which is right for a feed — a reader must not queue behind a
    write. It is wrong for anyone deciding *whether a write happened*: `append_batch_for_free_
    sessions` holds this lock across gate-then-append, so an unlocked reader can observe the
    ledger in the middle of that hold, see nothing, and conclude nothing was written while the
    append is moments away. `/message` recovery did exactly that and settled a turn terminal just
    before its own action landed.

    Serializing against the writer is what makes "readable and absent" mean "not written **yet**"
    is impossible rather than merely unlikely.
    """
    p = _path(path)
    with _locked(p):
        return _latest_by_id_locked(p)


def latest_by_id_serialized_checked(path: Path | None = None) -> tuple[str, dict[str, dict]]:
    """Both guarantees at once: **serialized** against the writer AND **tri-state**.

    The two existing helpers each give one half, and recovery needs both. Reading with
    `latest_by_id_serialized` cannot say "unreadable" — `_read_all_at` maps an `OSError` to `[]`
    — and a caller deciding *whether a write happened* then reads a transient I/O error as
    "nothing was written" and settles a turn terminal on no evidence. Reading with
    `latest_by_id_checked` says it, but without the writer lock, so it can observe the ledger
    mid-append and reach the same wrong conclusion for the other reason.

    Absence here is only meaningful because BOTH hold: the file read cleanly, and no write was in
    flight while it did.
    """
    p = _path(path)
    with _locked(p):
        if not p.exists():
            return "ok", {}
        try:
            raw = p.read_text(errors="replace")
        except OSError:
            return "unreadable", {}
        return "ok", _latest_of(_parse_records(raw))


def _latest_by_id_locked(p: Path) -> dict[str, dict]:
    return _latest_of(_read_all_at(p))


def _latest_of(records: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for rec in records:
        prev = out.get(rec["id"])
        if prev is None:
            out[rec["id"]] = rec
        else:
            # Merge forward so a transition event needn't restate the whole proposal.
            merged = {**prev, **rec}
            out[rec["id"]] = merged
    return out


def append_batch_for_free_sessions(
    records: list[dict],
    path: Path | None = None,
    *,
    gate: Callable[[], bool] | None = None,
    barred: Callable[[], set[str]] | None = None,
    record_guard: Callable[[dict], bool] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Append only those ``records`` whose session has no live action. Returns ``(kept, dropped)``.

    The dedupe rule — "at most one live action per session" — was being enforced by checking
    eligibility and then appending, which is a read and a write across two lock holds. The
    orchestrator's scheduled pass and the chat run under DIFFERENT single-flights, so both can
    observe a session as free, both mint an `approved` action, and both append. The session
    then carries two live actions, both can reach the actuator, and if the first write has not
    yet changed the screen the second precondition check passes too — duplicate input into a
    real session, which is the exact failure the ledger exists to prevent.

    Combining the check and the append under ONE exclusive hold is the only thing that closes
    it, because the losing writer must see the winner's record before deciding.

    ``gate`` extends that hold to a caller-supplied precondition, evaluated **inside** the lock
    and immediately before the write. `/message` uses it to make its turn reservation part of the
    same critical section as the append (#852): a fence checked before the lock is taken is
    check-then-write, and the writer can be reclaimed in between and append anyway — landing a
    second instruction no later fence can withdraw. A False gate drops the whole batch.

    ``barred`` is the same idea PER RECORD rather than per batch: a callable returning the set of
    session keys that may not be written at all, evaluated **inside** the lock and dropping only
    the records it names. `gate` cannot express this — it is all-or-nothing — and a caller
    filtering its own records before the call would be doing the check-then-write this function
    exists to prevent: the mission could be abandoned in the window between the filter and the
    lock, and the append would land anyway.

    **Lock order is ledger → missions, deliberately.** That is the order :func:`compact` already
    establishes (it holds this lock while projecting into the missions store), so a gate that
    touches missions here is consistent with it rather than the inversion that would deadlock.
    The same applies to ``barred``, which is a missions query.
    """
    # OWED TERMINALIZATIONS FIRST (#903 review 6, finding 1). A claim this process abandoned but
    # could not release reads as LIVE below, so the session looks busy and every later autonomous
    # action for it is dropped — indefinitely, because the owner is still running and startup
    # recovery is right to refuse the row.
    #
    # Here rather than inside the lock, because `discharge_owed` takes it. That makes this
    # check-then-act, and harmlessly so in the only direction that matters: discharging can only
    # REMOVE a live claim, never add one, so a discharge landing after the snapshot below means
    # the next pass admits the session rather than this one wrongly admitting it now.
    if _owed_terminal:
        discharge_owed(path)

    p = _path(path)
    kept: list[dict] = []
    dropped: list[dict] = []
    with _locked(p):
        if gate is not None and not gate():
            # Refused before anything is written. Every record is reported as dropped, so the
            # caller cannot claim to have queued what the ledger declined — the same rule the
            # per-session check below follows.
            return [], list(records)
        # Inside the lock, exactly like `gate` — and once, not per record, so the whole batch is
        # judged against one consistent snapshot of mission state.
        off_limits = barred() if barred is not None else set()
        latest = _latest_by_id_locked(p)
        busy = {
            r.get("session_id")
            for r in latest.values()
            if r.get("state") in LIVE_STATES and r.get("session_id")
        }
        for rec in records:
            if record_guard is not None and not record_guard(rec):
                dropped.append(rec)
                continue
            sid = rec.get("session_id")
            if sid and sid in busy:
                dropped.append(rec)
                continue
            if sid and sid in off_limits:
                # The session belongs to a mission that has been abandoned or is being archived.
                # Dropped rather than written-and-swept: an action that never exists cannot be
                # delivered by a pass that runs before the sweep reaches it.
                dropped.append(rec)
                continue
            _append_locked(p, rec, json.dumps(rec, sort_keys=True) + "\n")
            kept.append(rec)
            if sid:
                # A batch can itself name one session twice; the first append makes it busy.
                busy.add(sid)
    return kept, dropped


def live_actions(path: Path | None = None) -> list[dict]:
    """Actions still awaiting something, newest first."""
    rows = [r for r in latest_by_id(path).values() if r.get("state") in LIVE_STATES]
    rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
    return rows


def feed_by_session(
    limit: int = 100,
    path: Path | None = None,
    *,
    exclude: set[str] | None = None,
) -> list[dict]:
    """The activity feed as ONE row per session, newest first, bounded to ``limit`` SESSIONS.

    The orchestrator creates a fresh action for a session on every pass, so an idle session
    accumulates an action per pass forever — measured on the live ledger, the 100 rows the feed
    rendered carried only 26 distinct sessions, one of them 11 times (#774).

    Collapsing has to happen across the **complete** action set, not a slice of it: bound the
    input first and one busy session's recent actions push older sessions out entirely and
    under-report the count. That costs nothing, because ``latest_by_id`` already reads the whole
    ledger before anything is sliced — a pre-cap only truncates correctness.

    ``exclude`` drops action ids the caller renders elsewhere (the pending set), applied before
    the collapse so a hidden action can never become somebody's visible "latest".

    Each row is the session's newest action plus ``repeats`` — how many it stands for, so a
    collapsed row reads as a summary rather than as the only thing that happened.
    """
    rows = list(latest_by_id(path).values())
    rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
    skip = exclude or set()
    collapsed: dict[str, dict] = {}
    for r in rows:
        if r.get("id") in skip:
            continue
        # No session id means no identity to collapse ON — keying those to "" would merge
        # unrelated actions into a single row, so they fall back to their own unique id.
        sid = str(r.get("session_id") or "") or f"\x00{r.get('id')}"
        prior = collapsed.get(sid)
        if prior is None:
            collapsed[sid] = {**r, "repeats": 1}  # newest-first, so the first seen IS the latest
        else:
            prior["repeats"] += 1
    return list(collapsed.values())[: max(0, limit)]


def feed(limit: int = 100, path: Path | None = None) -> list[dict]:
    """The activity feed: every action's current state, newest first, bounded."""
    rows = list(latest_by_id(path).values())
    rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
    return rows[: max(0, limit)]


def get(action_id: str, path: Path | None = None) -> dict | None:
    return latest_by_id(path).get(action_id)


#: Report-store mutations running off the event loop (#1882): referenced until done, so none is
#: collected mid-flight, and a failure is logged rather than lost.
_report_endings: set = set()


def _report_ending_done(fut) -> None:
    _report_endings.discard(fut)
    if fut.cancelled():
        return
    exc = fut.exception()
    if exc is not None:
        log.warning("ending an answered HQ report failed", exc_info=exc)


def _end_answered_report(record: dict, proposed_at: float | None) -> None:
    """End the HQ report a delivered answer was FOR (#1882), and no other.

    A report's own answer ends only its ``report_id``. Any other delivered answer or relay (HQ's
    text, the orchestrator's) ends the current report only if it was filed BEFORE that action was
    proposed: text queued before the agent asked can't be its answer to the new question. With no
    proposal time known, nothing ends (the Commander can still dismiss it).

    The end waits on `session_input.mutation_fence` (up to its budget), so on a running event
    loop — `actuator.deliver` settles there — it runs in the loop's executor, never on the loop.
    """
    from . import agent_reports

    phys = str((record.get("authority") or {}).get("physical_key") or "")
    if not phys:
        return
    if record.get("source") == "report":
        report_id = str(record.get("report_id") or "")
        if not report_id:
            return
        kw: dict = {"report_id": report_id}
    else:
        pre = record.get("precondition")
        seen = pre.get("observed_at") if isinstance(pre, dict) else None
        cands = [
            float(t)
            for t in (proposed_at, seen)
            if isinstance(t, int | float) and not isinstance(t, bool)
        ]
        if not cands:
            return
        # The EARLIEST proposal-side time: a compaction can fold a later event's `ts` into the
        # first row, and erring early leaves a report current rather than swallowing it.
        kw = {"before": min(cands)}

    _off_loop(lambda: agent_reports.end_quietly(phys, "answered", **kw))


def _release_report_answer(record: dict, state: str) -> None:
    """A report answer that ended BEFORE byte one gives its report's one answer back (#1882).

    `agent_reports.mark_answered` reserved the report for this action when it was minted. Never
    claimed and now terminal (expired, rejected, refused before the claim), or refused after the
    claim (``stale``: every post-claim `stale` is pre-byte), it typed nothing, so a fresh tap may
    answer. Every other outcome keeps the reservation. Off the loop, like the end."""
    from . import agent_reports

    if record.get("source") != "report":
        return
    if state != "stale" and (record.get("claim_owner") or state == "claimed"):
        return
    phys = str((record.get("authority") or {}).get("physical_key") or "")
    report_id = str(record.get("report_id") or "")
    action_id = str(record.get("id") or "")
    if phys and report_id and action_id:
        _off_loop(lambda: agent_reports.clear_answer_quietly(phys, report_id, action_id))


def _off_loop(fn: Callable[[], object]) -> None:
    """Run a report-store mutation (it waits on `session_input.mutation_fence`) in the running
    loop's executor when there is one — `actuator.deliver` settles on the loop — else here."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        fn()
        return
    fut = loop.run_in_executor(None, fn)
    _report_endings.add(fut)
    fut.add_done_callback(_report_ending_done)


def _settled(
    action_id: str, state: str, record: dict | None = None, proposed_at: float | None = None
) -> None:
    """The single settlement boundary: an action just reached a terminal state, so the bell row
    that was raised for it is no longer something the operator can act on.

    Hooked here rather than at the call sites because there are five of them and they keep
    growing — approve, reject, the expiry sweep, actuator outcomes, startup recovery — and every
    one that forgot this left an alert pointing at an action nobody can resolve. Both mutation
    entry points funnel through :func:`transition` / :func:`compare_and_set`, so hooking the
    two of them covers every path, present and future.

    Two ordering rules, both load-bearing:

    * **After the durable append, never inside the lock.** The ledger write has already
      succeeded and is the record of truth. Holding the ledger lock across a notifications
      write would also take the two stores' locks in the opposite order from
      ``notifications.listing``, which reads this ledger — a deadlock waiting for load.
    * **Best-effort.** A notifications failure must not fail, or undo, a settled transition.
      ``notifications.listing`` reconciles anything missed on the next read, which is what makes
      swallowing the error safe rather than lossy.
    """
    if state not in TERMINAL_STATES:
        return
    # Suppressed on purpose: the ledger write stands, and `listing` heals on the next read.
    with contextlib.suppress(Exception):
        from . import notifications

        # Hand the DECISION time over while it is still in hand. Retirement can also happen as
        # a self-heal on a later read, and stamping the repair time there orders the operator's
        # decision history by when we noticed rather than when they decided — so an action
        # decided first can be projected newer than one decided after it.
        when = record.get("ts") if isinstance(record, dict) else None
        notifications.retire_for_actions(
            [action_id],
            decided_at={action_id: float(when)} if isinstance(when, int | float) else None,
        )
    # A DELIVERED answer ends that session's HQ report (#1882): the agent has its answer. Only a
    # delivery — a stale one typed nothing, and an indeterminate or failed one may not have given
    # the agent its answer, so the Commander must look (`report_answer_spent` keeps a second tap
    # from typing it again).
    if state == "delivered" and isinstance(record, dict) and record.get("verb") in REPORT_ENDING:
        with contextlib.suppress(Exception):
            _end_answered_report(record, proposed_at)
    if isinstance(record, dict):
        with contextlib.suppress(Exception):
            _release_report_answer(record, state)
    # A settled decision may end a needs-you episode (#1086 Phase 4): re-sync now, not at the
    # interval, so its notification is retracted while the operator is still looking.
    with contextlib.suppress(Exception):
        from . import needs_you_notify

        needs_you_notify.poke()
    # Freeze the mission timeline's settlement projection (#846, #840 §2). Compaction bounds
    # this ledger to a GLOBAL tail (`HISTORY_MAX`), which is right for a feed and wrong for a
    # mission that outlives it: without a projection, a six-week-old mission would keep its
    # `approval` event while the row carrying the verb, rationale and outcome had already been
    # compacted away, and the timeline would render a decision with no content. Written once,
    # here, for the same two reasons the retire above is: this is the ONE boundary every
    # settlement path reaches, and both mutation entry points funnel through it.
    #
    # Best-effort and outside the lock, on the same rules — a missions failure must never fail,
    # or undo, a settled ledger transition, and taking the missions write lock while holding the
    # ledger's would order two stores' locks against a reader that takes them the other way.
    with contextlib.suppress(Exception):
        from . import missions

        missions.record_settlement(action_id, record or {"id": action_id, "state": state})


def transition(action_id: str, state: str, path: Path | None = None, **extra) -> dict | None:
    """Record a state change for an existing action. Returns the merged record, or ``None``
    when the id is unknown (a transition for an action that never existed is dropped rather
    than inventing one)."""
    p = _path(path)
    with _locked(p):
        records = _read_all_at(p)
        cur = _latest_of(records).get(action_id)
        if cur is None:
            return None
        rec = {"id": action_id, "state": state, **extra}
        rec.setdefault("ts", time.time())
        _append_locked(p, rec, json.dumps(rec, sort_keys=True) + "\n")
        merged = {**cur, **rec}
    _settled(action_id, state, merged, _first_ts(records, action_id))
    return merged


def _first_ts(records: list[dict], action_id: str) -> float | None:
    """When the action's FIRST surviving event was written — its proposal, unless a compaction
    folded it (then a later event's time, never an earlier one)."""
    for r in records:
        if r.get("id") == action_id:
            t = r.get("proposed_ts", r.get("ts"))
            return float(t) if isinstance(t, int | float) and not isinstance(t, bool) else None
    return None


def compare_and_set(
    action_id: str,
    from_states: frozenset[str],
    to_state: str,
    path: Path | None = None,
    *,
    expect_ts: float | None = None,
    **fields: object,
) -> dict | None:
    """Atomically move an action to ``to_state`` iff it is currently in ``from_states``.

    The general form of :func:`claim`. Any caller that decides "this action is in state X, so
    I may move it to Y" needs the read and the write under ONE lock hold — otherwise two
    callers both observe X and both write, and the ledger's whole purpose (a single agreed
    history per action) is gone. Reject needs exactly this: without it a stale tap can
    overwrite ``delivered`` with ``rejected``, and a reject racing a claimed delivery produces
    ``claimed → rejected → delivered`` — the operator is told nothing was sent while the bytes
    are on their way.

    Returns the updated record, or ``None`` when the action is absent or not in ``from_states``.
    """
    p = _path(path)
    with _locked(p):
        records = _read_all_at(p)
        cur = _latest_of(records).get(action_id)
        if cur is None or cur.get("state") not in from_states:
            return None
        # REVISION check (#1086 review 5184): the caller acted on the record as it read it; any
        # event since (another claim, an edit, a state change) means that view is stale.
        if expect_ts is not None and cur.get("ts") != expect_ts:
            return None
        rec = {"id": action_id, "state": to_state, "ts": time.time()}
        # Carry the same optional fields `transition` records (detail, outcome), so a CAS
        # settlement keeps the WHY that the operator sees in the feed.
        rec.update({k: v for k, v in fields.items() if v is not None})
        _append_locked(p, rec, json.dumps(rec, sort_keys=True, default=str) + "\n")
        merged = {**cur, **rec}
    _settled(action_id, to_state, merged, _first_ts(records, action_id))
    return merged


#: Who this process is, for the purposes of "may I recover that claim?" (#903 review 3,
#: finding 4).
#:
#: The pid is what makes the question answerable — a claim whose owner is not running cannot be
#: in flight — and the start time is what makes the pid trustworthy, because pids are reused. Both
#: are read from ``/proc`` here and from ``/proc`` again at recovery time, so the comparison is
#: between two readings of the same fact rather than between a fact and a memory of one.
_OWNER_PID = os.getpid()


def _proc_started(pid: int) -> str | None:
    """The kernel's own start-time stamp for ``pid``, or None if it cannot be read.

    Field 22 of ``/proc/<pid>/stat``, in clock ticks since boot. Parsed from the LAST ``)`` rather
    than by splitting, because field 2 is the executable name and may itself contain spaces and
    parentheses — a split-on-space parser reads the wrong column for anything launched from a path
    with a bracket in it.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    cut = raw.rfind(")")
    if cut < 0:
        return None
    fields = raw[cut + 2 :].split()
    # `state` is field 3, so field 22 is index 19 of what follows the name.
    return fields[19] if len(fields) > 19 else None


def owner_token() -> str:
    """This process's claim owner. Stable for the life of the process."""
    return f"{_OWNER_PID}:{_proc_started(_OWNER_PID) or 'unknown'}"


def owner_is_live(token: object) -> bool | None:
    """Is the process that wrote this claim still running? ``None`` means we cannot tell.

    Three answers, and the third is the point (`unknown` is not `absent`): a token we cannot parse,
    or a pid whose ``/proc`` entry will not read, is not evidence that the owner is gone — and
    recovering a LIVE claim is the harmful direction, because it steals the claim from a delivery
    that is mid-write and makes its own settling CAS fail after the bytes have landed.
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
        return None  # we never knew which incarnation; we cannot tell now either
    return now == started


def claim(
    action_id: str,
    from_states: frozenset[str],
    path: Path | None = None,
    *,
    expect_ts: float | None = None,
    **fields: object,
) -> dict | None:
    """Atomically move an action to ``claimed`` iff it is currently in ``from_states``.

    ``get()`` then ``transition()`` is a read and a write across TWO lock holds, so two callers
    can both observe ``proposed`` and both append ``claimed`` — and then both write to the PTY.
    That silently breaks the at-most-once guarantee the whole ledger exists to provide, and it
    breaks it in the one direction that matters: a duplicate `choose` answers a prompt twice.

    Compare-and-swap under a single exclusive hold. Returns the claimed record, or ``None``
    when another caller got there first (or the action is not claimable).

    **The claim records WHO holds it** (#903 review 3, finding 4). This store is shared between
    sibling instances by design, so "every claimed action is orphaned" is false the moment two of
    them are running: instance B starting while A is mid-delivery would recover A's claim, and A's
    own ``claimed -> delivered`` CAS then fails after the bytes have already landed. Recovery can
    only act on a claim it can PROVE is orphaned, and this is what makes that provable.

    **Revision and payload are part of the claim** (#1086 review 5184). ``expect_ts`` refuses a
    record that changed since the caller read and rendered it, and ``fields`` (an operator's edit:
    the `relay` verb and its text) are written by the SAME atomic append that claims — so the bytes
    rendered from that view are exactly what the claimed record says was sent, and two approvals
    can never interleave one's text with the other's claim.
    """
    return compare_and_set(
        action_id,
        from_states,
        "claimed",
        path,
        expect_ts=expect_ts,
        claim_owner=owner_token(),
        **fields,
    )


def expire_due(now: float | None = None, path: Path | None = None) -> list[str]:
    """Move every live action past its ``expires_at`` to ``expired``. Returns the ids moved.

    An expired proposal is one whose screen the operator never acted on in time; delivering it
    later would be delivering against a screen nobody has looked at recently, which is exactly
    what the precondition check exists to prevent.

    The snapshot below is read outside the lock, so an action can be CLAIMED between being
    listed and being expired. Skipping `claimed` in the loop is therefore not enough — the
    check and the write must be one atomic step, or expiry lands on top of a live delivery and
    the ledger records `approved -> claimed -> expired -> delivered`: an action that was
    expired and then delivered anyway, which is both a lie about what happened and an ordering
    no reader can make sense of.
    """
    now = time.time() if now is None else now
    moved: list[str] = []
    for rec in live_actions(path):
        if rec.get("state") == "claimed":
            continue  # mid-delivery; recover_claimed owns this one
        exp = rec.get("expires_at")
        if isinstance(exp, int | float) and not isinstance(exp, bool) and now >= exp:
            # CAS from the states expiry may legitimately act on. A claim that landed since the
            # snapshot wins, and this quietly does nothing.
            if compare_and_set(rec["id"], EXPIRABLE_STATES, "expired", path) is not None:
                moved.append(rec["id"])
    return moved


#: How long a claim whose owner cannot be identified may sit before it counts as orphaned.
#:
#: Only ever applied to rows written by a build that did not record an owner — every claim this
#: build writes is decided by :func:`owner_is_live` instead, with no clock involved. The bound is
#: derived rather than picked: a delivery holds the write fence for at most
#: ``MUTATION_FENCE_BUDGET_S`` and writes with a ``WRITE_TIMEOUT_S`` deadline, so a claim still
#: open two orders of magnitude past their sum is not a delivery in progress under any path that
#: exists. Legacy rows only; delete this when no store can still contain one.
LEGACY_CLAIM_STALE_S = 900.0


#: Claims THIS process abandoned but could not terminalize (#903 review 5, finding 1).
#:
#: The compensating `claimed -> indeterminate` CAS is written by the one process that knows the
#: delivery is over — and it can fail for the same reason the delivery did, because it is the same
#: store. Suppressed, that left the ledger `claimed` under a live owner, which `recover_claimed`
#: correctly refuses to touch: the session reads busy and later actions are refused until the
#: process restarts, while the timeline says the delivery is already terminal.
#:
#: So the obligation outlives the attempt. In memory rather than on disk deliberately: the store
#: is the thing that just failed, and a durable record of "the store would not take a write" has
#: nowhere to live. If the process dies with entries here, its owner token dies with it and
#: startup recovery takes them — the two paths cover each other exactly.
_owed_terminal: dict[str, str] = {}
_owed_lock = threading.Lock()


def owe_terminalize(action_id: str, note: str) -> None:
    """Remember that this process still has to release a claim it abandoned."""
    with _owed_lock:
        _owed_terminal[action_id] = note


def discharge_owed(path: Path | None = None) -> list[str]:
    """Retry every owed terminalization. Returns the ids that moved (or were already settled).

    Called from the read-time relay reconcile, which is the cadence that already exists for
    exactly this class of unfinished business — no new loop, and it runs whenever anybody looks
    at a mission. Never raises: a store that is still down simply keeps the obligation.
    """
    with _owed_lock:
        owed = dict(_owed_terminal)
    done: list[str] = []
    for action_id, note in owed.items():
        try:
            moved = compare_and_set(
                action_id, frozenset({"claimed"}), "indeterminate", path, note=note
            )
            if moved is None:
                # Not `claimed` any more — somebody settled it, so the obligation is discharged
                # by the outcome rather than by us. An unreadable ledger raises instead.
                status, _rec = lookup(action_id, path)
                if status == "unreadable":
                    continue
            done.append(action_id)
        except Exception:  # noqa: BLE001
            log.debug("could not discharge the owed terminalization of %s", action_id)
    if done:
        with _owed_lock:
            for action_id in done:
                _owed_terminal.pop(action_id, None)
    return done


def recover_claimed(path: Path | None = None, now: float | None = None) -> list[str]:
    """Startup recovery: every action left ``claimed`` **by a process that is provably gone**
    becomes ``indeterminate``.

    A ``claimed`` record means "we were about to write, or had just written" — and no on-disk
    state can distinguish those two, because the process died in exactly the gap between them.
    Retrying could double-deliver a ``choose``; assuming success could silently drop one. So
    neither is assumed: the action is parked for the operator and the next pass re-reads the live
    screen.

    **"Left by a process that is provably gone" is the whole of the change** (#903 review 3,
    finding 4). Recovering every global ``claimed`` row was correct for a single instance and
    actively harmful for the siblings this store supports: instance B starting while A is
    mid-delivery moved A's action to ``indeterminate``, A's own settling CAS then failed, and the
    ledger recorded an ambiguous outcome for a delivery whose bytes had landed. So a claim is
    recovered only when its owner's pid is gone, and the move is a CAS from ``claimed`` — if the
    owner settles it between the reading and the write, the owner wins and this does nothing.

    Returns the ids moved.
    """
    ts = time.time() if now is None else now
    moved: list[str] = []
    for rec in latest_by_id(path).values():
        if rec.get("state") != "claimed":
            continue
        token = rec.get("claim_owner")
        if token is None:
            # LEGACY ROW, from a build that recorded no owner. Nothing can prove it orphaned, so
            # the only honest handle is its age against a bound the delivery path cannot exceed.
            if ts - float(rec.get("ts") or 0) < LEGACY_CLAIM_STALE_S:
                continue
            note = "claimed with no recorded owner and long past any write deadline"
        else:
            live = owner_is_live(token)
            if live is not False:
                # ALIVE, or we could not tell. Both mean "leave it": stealing a live claim breaks
                # the delivery that holds it, and an unreadable `/proc` is not evidence of death.
                continue
            note = "the process that claimed it is gone; cannot prove whether input landed"
        if compare_and_set(rec["id"], frozenset({"claimed"}), "indeterminate", path, note=note):
            moved.append(rec["id"])
    return moved


def compact(path: Path | None = None, history_max: int = HISTORY_MAX) -> int:
    """Rewrite the ledger to the current state of every live action plus a bounded tail of
    terminal ones. Returns the number of records kept — **0 if the pass was declined**.

    Atomic (temp + ``os.replace``): a crash during compaction leaves the previous ledger
    intact, never a half-written one.

    **A row is never destroyed before the projection that outlives it is durable (#846).** This is
    the only place a ledger row is ever removed, so it is the only place that can make that
    promise. The projection therefore happens **inside the same lock hold, over the same snapshot
    that is about to be rewritten**, and a failure to project **declines the compaction** rather
    than proceeding. Two earlier shapes were both wrong: projecting outside the lock let a
    concurrent terminal append change which rows were dropped, so a newly-doomed referenced action
    went unprojected; and swallowing the projection error let compaction destroy the row anyway.

    Declining is safe: ``COMPACT_AT_LINES`` is a trigger, not a bound, so the file simply compacts
    on a later pass once the missions store is available again.
    """
    p = _path(path)
    if not p.exists():
        return 0
    with _locked(p):
        # STRICT: a ledger that exists and cannot be read is never rewritten (it would become an
        # empty one). Declined, logged; the next pass retries.
        try:
            records = _read_all_strict(p)
        except LedgerUnreadable as e:
            log.warning("ledger: compaction declined — %s", e)
            return 0
        rows = list(_latest_of(records).values())
        first = _first_ts_map(records)
        # …and while the HQ reports store cannot be read, which rows can still spend a CURRENT
        # report is unknown (#1882): declined entirely rather than guessing a subset to keep. The
        # ledger only grows until the next pass that can read it.
        pins = _report_pins([r for r in rows if r.get("state") not in LIVE_STATES], first)
        if pins is None:
            log.warning("ledger: compaction declined — the HQ reports store could not be read")
            return 0
        doomed = _doomed(rows, history_max, first_ts=first, report_pins=pins)
        # Before anything is projected or deleted: a delivered supervisor nudge whose thread record
        # is missing keeps its only copy of the typed text HERE (#983 review 4871).
        doomed = _preserve_deliveries(doomed)
        if not _project(doomed):
            log.warning(
                "ledger: compaction declined — could not durably project %d settled action(s) "
                "a mission timeline still references",
                len(doomed),
            )
            return 0
        # Ask's follows (#1517): a delivered Ask nudge earns its conversation a follow. The row is
        # its only record, so it is ADMITTED (follow or a visible non-follow, one durable write)
        # before it can go; a failure declines, exactly like the mission projection. Lock order:
        # this ledger lock, then the conversation's follows lock — the follower never takes them
        # the other way round.
        from . import ask_follow

        if not ask_follow.project_for_compaction(doomed):
            log.warning("ledger: compaction declined — an Ask nudge could not be admitted yet")
            return 0
        return _compact_locked(p, history_max, rows=rows, doomed=doomed, first_ts=first)


def _first_ts_map(records: list[dict]) -> dict[str, float]:
    """``action id -> when it was proposed``: a kept ``proposed_ts``, else its first event."""
    out: dict[str, float] = {}
    for r in records:
        aid = r["id"]
        t = r.get("proposed_ts", r.get("ts")) if aid not in out else None
        if aid not in out and isinstance(t, int | float) and not isinstance(t, bool):
            out[aid] = float(t)
    return out


def _report_pins(done: list[dict], first_ts: dict[str, float]) -> set[str] | None:
    """Terminal action ids compaction must KEEP because they can spend a CURRENT HQ report
    (#1882, Hermes on #1884): the one-answer rule and `answered_after` read the ledger, so evicting
    them would make an answered report answerable again. Exactly those rows while each report is
    current — a report ends at the latest when its launch ends, is archived or replaced — so the
    pinned set is bounded by the sessions that hold a report. The reports store is read STRICTLY:
    ``None`` when it cannot be read, and compaction then DECLINES (`compact`) — no partial guess at
    which rows to keep."""
    from . import agent_reports

    try:
        current = agent_reports.current_checked()
    except agent_reports.StoreUnreadable:
        return None
    pins: set[str] = set()
    for phys, rep in current.items():
        for r in done:
            if _spends_report(r, first_ts.get(r["id"]), phys, rep):
                pins.add(str(r["id"]))
    return pins


def _pinned_turn_keys() -> set[tuple[str, str]] | None:
    """Turn ids with an unresolved `/message` claim. ``None`` if the missions store is unreadable.

    A turn recovering from a crash asks the ledger "is there already an action for me?", and
    reads a readable absence as "nothing was ever appended" — the only state from which it may
    call the model a second time. That reading is only sound if compaction cannot have removed
    the action underneath it, because *compacted away* and *never written* are otherwise the same
    observation with opposite safe responses.

    So an action belonging to an unresolved turn is not compactable. ``None`` (unreadable) is
    propagated rather than treated as "nothing pinned": guessing empty here would let compaction
    delete the very evidence the guess depends on.

    Keyed on ``(mission_id, turn_id)``, both halves. `turn_id` is client-generated and may
    legitimately repeat across missions, so pinning on it alone let one long-running turn retain
    another mission's unrelated terminal history — defeating this ledger's global bound.
    """
    try:
        from . import missions

        return missions.unresolved_turn_keys()
    except Exception:  # noqa: BLE001 — unreadable is not "nothing pinned"
        log.debug("ledger: could not read unresolved turns", exc_info=True)
        return None


def _doomed(
    rows: list[dict],
    history_max: int,
    *,
    first_ts: dict[str, float] | None = None,
    report_pins: set[str] | None = None,
) -> list[dict]:
    """The terminal rows this compaction will drop. The SAME partition ``_compact_locked`` uses —
    computed once, from one snapshot, and handed to both, so the two cannot disagree.

    Rows pinned by an unresolved turn are excluded, so they are neither dropped nor counted
    against the tail — see :func:`_pinned_turn_ids` — and so are rows that can spend a current HQ
    report (:func:`_report_pins`).
    """
    pinned = _pinned_turn_keys()
    done = [r for r in rows if r.get("state") not in LIVE_STATES]
    if report_pins is None:
        report_pins = _report_pins(done, first_ts or {})
        if report_pins is None:
            return []  # which rows spend a current report is unknown: nothing may be dropped
    if report_pins:
        done = [r for r in done if str(r.get("id") or "") not in report_pins]
    if pinned is None:
        # The pin set is unknown, so nothing may be dropped: this pass cannot prove any given
        # row is safe to remove. Compaction is a housekeeping optimisation and skipping one is
        # free; deleting an action a recovering turn was about to find is not.
        return []
    if pinned:
        done = [
            r
            for r in done
            if (str(r.get("mission_id") or ""), str(r.get("turn_id") or "")) not in pinned
        ]
    done.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
    return done[max(0, history_max) :]


def _preserve_deliveries(doomed: list[dict]) -> list[dict]:
    """Make sure a doomed DELIVERED supervisor nudge's thread record exists, or keep the row.

    A delivery settles this ledger first and writes the mission thread's delivered record after it,
    best-effort; `actuator.reconcile_delivered_nudges` repairs a lost write from the row's
    `delivered_text` on the next sweep. Compaction can come first, and the settlement projection
    below keeps the state and outcome but not the text — so deleting the row would lose what was
    typed for good (#983 review 4871).

    So the SAME record-level reconciliation runs here, inside the compaction lock and over the
    same snapshot, before the projection and the rewrite. Lock order is the one `_project` already
    takes: this ledger's lock, then the missions store.

    **Fails closed, narrowly.** A row whose record could not be written is removed from `doomed`,
    i.e. pinned for this pass: it stays in the ledger and the next sweep or compaction retries. If
    the reconciliation cannot run at all, every doomed delivered nudge with text is pinned. Nothing
    else is retained longer.
    """
    from . import missions

    # The store's own predicate, so a delivered AI draft (#983 P3) is kept exactly as a nudge is.
    candidates = [r for r in doomed if missions.is_delivered_supervisor_nudge(r)]
    # …AND THE BELL'S OBLIGATION (#983 P4 review). An autonomous AI-written direction announces
    # itself after the settlement, best-effort, and the repair reads THIS row to do it — so
    # dropping the row while the announcement is still owed discards it for good: the action is
    # gone and no notification was ever made. Pinned until the receipt is durable, which is the
    # same promise the thread record above gets — and the pin RELEASES in the ordinary case,
    # because a receipt whose first write failed is re-attempted on every later announcement of
    # the same action (`notifications._converge`). That receipt is also what the announce path
    # consults, so a pinned row can never outlive the record that stops it being re-announced.
    owing: set[str] = set()
    auto = [
        r
        for r in doomed
        if r.get("state") == "delivered"
        and r.get("verb") == "draft_direction"
        and str(r.get("sent_by") or "") == "auto"
    ]
    if auto:
        try:
            owing = missions.unannounced_auto_ids([str(r.get("id") or "") for r in auto])
        except Exception as e:  # noqa: BLE001 — unknown is not "announced"
            log.warning("ledger: could not read announcement receipts before compaction (%s)", e)
            owing = {str(r.get("id") or "") for r in auto}
    if not candidates:
        return [r for r in doomed if str(r.get("id") or "") not in owing] if owing else doomed
    try:
        keep = set(missions.reconcile_delivered_records(candidates)["unrecorded"])
    except Exception as e:  # noqa: BLE001 — unknown is not "recorded"
        log.warning("ledger: could not reconcile delivered nudges before compaction (%s)", e)
        keep = {str(r.get("id") or "") for r in candidates}
    keep |= owing
    if not keep:
        return doomed
    log.warning(
        "ledger: keeping %d delivered nudge(s) whose thread record could not be written",
        len(keep),
    )
    return [r for r in doomed if str(r.get("id") or "") not in keep]


def _project(doomed: list[dict]) -> bool:
    """Freeze a mission-side projection of every doomed action a mission timeline references.

    Returns True when there is nothing to do or everything referenced was written. Returns
    **False** when the missions store could not be consulted or could not be written, which
    declines the compaction — the caller must not destroy evidence it failed to preserve.

    Scoped to *referenced* ids on purpose: the ledger carries far more actions than any mission
    points at, and copying the rest into the missions store would put bounded model text there
    with no mission to own it and no retention window over it.
    """
    if not doomed:
        return True
    try:
        from . import missions

        ids = [r["id"] for r in doomed if isinstance(r.get("id"), str)]
        referenced = missions.referenced_action_ids(ids)
        if not referenced:
            return True
        wanted = [r for r in doomed if r.get("id") in referenced]
        missions.record_settlements(wanted)
        return True
    except Exception as e:  # noqa: BLE001 — a decline, not a crash: compaction is housekeeping
        log.warning("ledger: could not project settlements (%s)", type(e).__name__)
        return False


def _compact_locked(
    p: Path,
    history_max: int,
    *,
    rows: list[dict] | None = None,
    doomed: list[dict] | None = None,
    first_ts: dict[str, float] | None = None,
) -> int:
    # `rows` is passed in by `compact` so the partition that was PROJECTED is byte-identical to
    # the one that is rewritten. Re-reading here would reopen the window the projection closed.
    # A re-read here is STRICT for the same reason as in `compact`: never rewrite what was unread.
    rows = list(_latest_of(_read_all_strict(p)).values()) if rows is None else rows
    # …and `doomed` is passed in for the same reason, one level up. This used to RE-DERIVE the
    # partition, which was fine only while both derivations happened to agree: the moment
    # `_doomed` learned to exclude rows pinned by an unresolved turn, a recomputation here would
    # have projected one set and deleted a different, larger one. One computation, handed to
    # both, is what the `_doomed` docstring always claimed.
    if first_ts is None:
        first_ts = _first_ts_map(_read_all_strict(p))
    if doomed is None:
        doomed = _doomed(rows, history_max, first_ts=first_ts)
    drop = {id(r) for r in doomed}
    # A kept row is rewritten as ONE merged event, whose `ts` is its LATEST: it keeps when it was
    # PROPOSED as `proposed_ts`, which the report end and `answered_after` bind on (#1882).
    keep = [
        {**r, "proposed_ts": first_ts[r["id"]]}
        if r["id"] in first_ts and "proposed_ts" not in r
        else r
        for r in rows
        if id(r) not in drop
    ]
    keep.sort(key=lambda r: float(r.get("ts") or 0))
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        for rec in keep:
            # Same short-write rule as _append_locked. Compaction REPLACES the ledger, so a
            # torn line here loses history rather than just one record.
            buf = (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8")
            off = 0
            while off < len(buf):
                n = os.write(fd, buf[off:])
                if n <= 0:
                    raise OSError("short write while compacting the ledger")
                off += n
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, p)
    # The rename is a NAMESPACE change and needs its own sync (#728): the compacted bytes are
    # durable above, but after power loss the directory can still name the pre-compaction inode
    # — the docstring's "a crash during compaction leaves the previous ledger intact, never a
    # half-written one" was true for process death and not for power loss.
    fsync_dir(p.parent)
    return len(keep)


def generation(path: Path | None = None) -> str:
    """A cheap marker that changes on ANY ledger mutation — append, transition, compaction.

    Change detection cannot reason about settlement by watching for it in one place. Actions
    expire from the scheduled sweep, from `GET /api/pulse/orchestrator`'s housekeeping, from an
    operator approving or rejecting, and from startup recovery. Every one of those RESTORES a
    session to eligibility, and a fingerprint computed only over the eligible world cannot see
    it: the world afterwards is byte-identical to the world before the proposal existed.

    Folding this into the fingerprint makes invalidation a property of the ledger rather than
    something each call site has to remember. It converges rather than looping: recording
    actions makes those sessions ineligible, so a pass that proposes for everything leaves an
    empty set, and a pass that proposes for nothing leaves the generation unchanged.

    Two stat fields, not a hash — this runs every sweep and must stay free.
    """
    p = _path(path)
    try:
        st = p.stat()
    except OSError:
        return ""
    return f"{st.st_size}:{st.st_mtime_ns}"


def compact_if_needed(path: Path | None = None) -> int:
    """Compact once the raw line count crosses ``COMPACT_AT_LINES``. Cheap no-op otherwise —
    this is what keeps an append-only file bounded without a scheduler."""
    p = _path(path)
    if not p.exists():
        return 0
    try:
        with p.open("rb") as fh:
            lines = sum(1 for _ in fh)
    except OSError:
        return 0
    return compact(path) if lines >= COMPACT_AT_LINES else 0
