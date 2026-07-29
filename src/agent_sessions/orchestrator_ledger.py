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

    proposed ─┬─► claimed ─┬─► delivered        bytes reached the PTY
              │            ├─► failed           write refused / aborted mid-write
              │            └─► indeterminate    crashed after the write, before the record
              ├─► approved ─► claimed …         operator tapped approve
              ├─► rejected                      operator declined
              ├─► escalated                     below threshold; needs the operator
              ├─► stale                         precondition moved before delivery
              └─► expired                       TTL elapsed untouched

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

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path

# Terminal states — an action here will never transition again, so compaction may drop it
# once it falls out of the history tail.
TERMINAL_STATES: frozenset[str] = frozenset(
    # `observed` is terminal: it is a note in the feed, and nothing further ever happens to it.
    # It was previously in NEITHER set, so it fell outside compaction's live/done partition.
    {"delivered", "failed", "indeterminate", "rejected", "stale", "expired", "observed"}
)
# States an action can sit in while still awaiting something (an operator tap, a delivery).
LIVE_STATES: frozenset[str] = frozenset({"proposed", "approved", "claimed", "escalated"})
ALL_STATES: frozenset[str] = TERMINAL_STATES | LIVE_STATES

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
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return rec


def read_all(path: Path | None = None) -> list[dict]:
    """Every well-formed event, oldest first.

    Torn-tail safe: a trailing partial line (crash mid-append) is discarded, as is any line
    that isn't a JSON object. A malformed line is *skipped*, never fatal — a damaged ledger
    must degrade to a shorter history, never take down the Pulse page.
    """
    return _read_all_at(_path(path))


def _read_all_at(p: Path) -> list[dict]:
    if not p.exists():
        return []
    try:
        raw = p.read_text(errors="replace")
    except OSError:
        return []
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


def latest_by_id(path: Path | None = None) -> dict[str, dict]:
    """Current state per action id — the newest event wins. Insertion order follows first
    appearance, so a caller iterating gets stable, roughly chronological output."""
    return _latest_by_id_locked(_path(path))


def _latest_by_id_locked(p: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for rec in _read_all_at(p):
        prev = out.get(rec["id"])
        if prev is None:
            out[rec["id"]] = rec
        else:
            # Merge forward so a transition event needn't restate the whole proposal.
            merged = {**prev, **rec}
            out[rec["id"]] = merged
    return out


def live_actions(path: Path | None = None) -> list[dict]:
    """Actions still awaiting something, newest first."""
    rows = [r for r in latest_by_id(path).values() if r.get("state") in LIVE_STATES]
    rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
    return rows


def feed(limit: int = 100, path: Path | None = None) -> list[dict]:
    """The activity feed: every action's current state, newest first, bounded."""
    rows = list(latest_by_id(path).values())
    rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
    return rows[: max(0, limit)]


def get(action_id: str, path: Path | None = None) -> dict | None:
    return latest_by_id(path).get(action_id)


def transition(action_id: str, state: str, path: Path | None = None, **extra) -> dict | None:
    """Record a state change for an existing action. Returns the merged record, or ``None``
    when the id is unknown (a transition for an action that never existed is dropped rather
    than inventing one)."""
    p = _path(path)
    with _locked(p):
        cur = _latest_by_id_locked(p).get(action_id)
        if cur is None:
            return None
        rec = {"id": action_id, "state": state, **extra}
        rec.setdefault("ts", time.time())
        _append_locked(p, rec, json.dumps(rec, sort_keys=True) + "\n")
        return {**cur, **rec}


def expire_due(now: float | None = None, path: Path | None = None) -> list[str]:
    """Move every live action past its ``expires_at`` to ``expired``. Returns the ids moved.

    An expired proposal is one whose screen the operator never acted on in time; delivering it
    later would be delivering against a screen nobody has looked at recently, which is exactly
    what the precondition check exists to prevent.
    """
    now = time.time() if now is None else now
    moved: list[str] = []
    for rec in live_actions(path):
        if rec.get("state") == "claimed":
            continue  # mid-delivery; recover_claimed owns this one
        exp = rec.get("expires_at")
        if isinstance(exp, int | float) and not isinstance(exp, bool) and now >= exp:
            transition(rec["id"], "expired", path)
            moved.append(rec["id"])
    return moved


def recover_claimed(path: Path | None = None) -> list[str]:
    """Startup recovery: every action left ``claimed`` becomes ``indeterminate``.

    Called once at boot. A ``claimed`` record means "we were about to write, or had just
    written" — and no on-disk state can distinguish those two, because the process died in
    exactly the gap between them. Retrying could double-deliver a ``choose``; assuming success
    could silently drop one. So neither is assumed: the action is parked for the operator and
    the next pass re-reads the live screen. Returns the ids moved.
    """
    moved: list[str] = []
    for rec in latest_by_id(path).values():
        if rec.get("state") == "claimed":
            transition(
                rec["id"],
                "indeterminate",
                path,
                note="process restarted mid-delivery; cannot prove whether input landed",
            )
            moved.append(rec["id"])
    return moved


def compact(path: Path | None = None, history_max: int = HISTORY_MAX) -> int:
    """Rewrite the ledger to the current state of every live action plus a bounded tail of
    terminal ones. Returns the number of records kept.

    Atomic (temp + ``os.replace``): a crash during compaction leaves the previous ledger
    intact, never a half-written one.
    """
    p = _path(path)
    if not p.exists():
        return 0
    with _locked(p):
        return _compact_locked(p, history_max)


def _compact_locked(p: Path, history_max: int) -> int:
    rows = list(_latest_by_id_locked(p).values())
    live = [r for r in rows if r.get("state") in LIVE_STATES]
    done = [r for r in rows if r.get("state") not in LIVE_STATES]
    done.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
    keep = live + done[: max(0, history_max)]
    keep.sort(key=lambda r: float(r.get("ts") or 0))
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        for rec in keep:
            os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, p)
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
