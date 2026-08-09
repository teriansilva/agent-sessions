"""The one server-owned seam for writing input into a live session (#726 Phase 2).

Until now, input reached a PTY exactly two ways: a browser keystroke over ``/ws/term/{sid}``,
or the one-shot handoff seed injected at launch. The orchestrator needs a third — and getting
that wrong garbles somebody's terminal, so the whole design is about *not* getting it wrong.

**One seam, not a handle.** Callers never receive the pty fd or the write lock. They call
:func:`send_input` and get an :class:`Outcome`. Handing out ``(fd, lock)`` would make every
call site responsible for the single-writer invariant and leave the race surface untestable as
a unit; this way there is exactly one place that writes, and one place to test.

**Ownership flips under you.** ``session_stream.SessionRegistry`` moves byte ownership between
the headless ``SessionStream`` and the attached WS bridge on every attach/detach. So writers
*register* here, and :func:`send_input` resolves the current one at write time — a caller can
never route a write to a stale owner, because it never names one.

**Serialisation is shared, not parallel.** A registered writer brings its own lock — the same
``write_lock`` ``pump_in`` and ``_deliver_seed`` already contend for. We take that lock rather
than inventing a second one, or the "only one writer at a time" guarantee would be two
guarantees that don't know about each other.

**Never on the event loop.** :func:`send_input` blocks; callers run it under
``asyncio.to_thread``. A blocking write on the loop freezes every session this process serves
— the #678 failure mode, still commented in ``webterm``.

**Partial write ⇒ abort, never replay.** An unterminated bracketed paste has already reached
the TUI. Replaying it would corrupt the prompt; pretending it worked would be a lie. It is
consumed explicitly as ``aborted`` and the ledger records that.
"""

from __future__ import annotations

import contextlib
import errno
import os
import select
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count

from . import tty_health

# A pty write with confirmed room accepts at least this much without blocking; a blocking
# write never returns short, so a larger chunk could still block past the deadline.
WRITE_CHUNK = 256
# Default wall-clock ceiling for one delivery. Generous enough for a TUI that is mid-render,
# short enough that a wedged target settles the action instead of pinning a worker thread.
WRITE_TIMEOUT_S = 5.0
# How long the target must be quiet before we type into it. Readiness is NOT DECSET 2004: an
# engine can arm bracketed-paste mode in its preamble and still be eating stdin (the handoff
# path paid for that lesson). Quiet-after-paint is the signal that actually correlates.
QUIET_WINDOW_S = 0.35
QUIET_WAIT_MAX_S = 3.0


@dataclass(frozen=True)
class Outcome:
    """What happened. ``state`` maps onto the ledger's terminal states."""

    state: str  # delivered | not_live | refused | stale | aborted | failed | timeout
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.state == "delivered"


@dataclass
class _Writer:
    fd: int
    lock: threading.Lock
    kind: str  # "attached" | "headless"
    token: int


_writers: dict[str, _Writer] = {}
_lock = threading.Lock()
_tokens = count(1)

# --- the authorization fence (#726) -------------------------------------------------------
#
# A safe write needs THREE things simultaneously stable, and each lives in its own
# synchronization domain: policy in `prefs` (a flock'd file), viewer state in
# `session_stream` (an asyncio lock), ownership here (a threading lock). Six rounds of
# review showed that no arrangement of *checks* closes this — each fix narrowed one window
# and left another, because the thing being checked can change between the check and the
# write no matter how late the check is moved.
#
# So: one epoch per session, bumped by ANY event that invalidates an in-flight
# authorization — a writer registered or released, a viewer attaching or detaching. A sender
# captures the epoch before its gates run and re-verifies it, together with the policy
# fingerprint, while holding `_lock` across the WHOLE payload. Anything that would
# invalidate the authorization must take `_lock` to bump the epoch, so it cannot interleave.
#
# The cost is real and was chosen deliberately: attach/detach serialises behind an in-flight
# send. Sends are small (a bracketed-paste nudge) and bounded by the write deadline, so the
# stall is short — and a brief delay attaching is a far better failure than typing into the
# wrong terminal.
_epochs: dict[str, int] = {}

# Policy is the third domain and it lives in a FILE, so it cannot be read inside the fence:
# `prefs.set_orchestrator` holds the prefs flock and would need `_lock` to announce itself,
# while a fence that read prefs would hold `_lock` and want the prefs flock. That is a
# lock-order inversion and it deadlocks. Policy therefore announces itself as a plain
# in-memory counter — the fence compares an integer with no I/O at all.
_policy_epoch: int = 0

# The FOURTH domain: the screen itself. `check_precondition` compares a screen fingerprint, but
# that read happens in the final guard — which must run OUTSIDE the fence, because it does I/O.
# Ordinary agent output between that read and the first byte changes the prompt without
# touching ownership, the session epoch or policy, so a stale `choose`/`answer` lands in a
# prompt nobody proposed against. Output ingestion bumps this; the fence compares it.
#
# Deliberately NOT guarded by `_lock`: ingestion is the hot path (every chunk of pty output),
# and making it contend with a write that holds `_lock` across a whole payload would stall the
# reader. A monotonic counter under its own tiny lock is enough — the fence only needs to
# observe increments that COMPLETED before it looked.
_screen_epochs: dict[str, int] = {}
_screen_lock = threading.Lock()


@contextlib.contextmanager
def screen_change(key: str):
    """Publish a screen mutation as a SEQLOCK interval: odd while in progress, even when done.

    A single counter cannot express this. Bumping AFTER the ring mutation leaves a window where
    the new bytes are observable but the counter is stale (the sender authorises against a
    screen that already moved). Bumping BEFORE leaves the mirror image: the sender captures the
    already-incremented value, the guard reads the OLD screen, the append then lands with no
    further change, and the in-fence comparison still matches — so the write lands in the new
    screen anyway. Both orderings were shipped and both were caught.

    So the interval is published, not the instant: increment on entry (odd = a change is in
    flight), mutate, increment on exit (even = stable). A reader is safe only if it saw an EVEN
    value before reading the screen and the SAME value afterwards.

    Deliberately not the registry lock: ingestion is the hot path and must never stall behind a
    payload write.
    """
    with _screen_lock:
        _screen_epochs[key] = _screen_epochs.get(key, 0) + 1
    try:
        yield
    finally:
        with _screen_lock:
            _screen_epochs[key] = _screen_epochs.get(key, 0) + 1


def screen_is_stable(key: str) -> bool:
    """False while a screen mutation is in flight (odd sequence)."""
    return current_screen_epoch(key) % 2 == 0


def bump_screen_epoch(key: str) -> None:
    """A complete screen change with no interval to wrap. Kept for callers that mutate the
    screen atomically elsewhere; equivalent to entering and leaving `screen_change`."""
    with _screen_lock:
        _screen_epochs[key] = _screen_epochs.get(key, 0) + 2


def current_screen_epoch(key: str) -> int:
    with _screen_lock:
        return _screen_epochs.get(key, 0)


@contextlib.contextmanager
def policy_transaction():
    """Hold the write fence across a policy change, bumping the epoch on the way out.

    A bump AFTER persisting is not enough: between the two, the stored policy has already
    changed while the epoch still reads old, so a send in that gap passes the compare and
    writes under policy the operator has withdrawn. Holding the lock across the persist means
    a send either finishes before the change starts, or waits and then sees the new epoch.

    Lock order is one-way by construction: this takes the registry lock then the prefs flock,
    while the fence takes only the registry lock and compares an integer — it never wants the
    prefs lock, so the two cannot deadlock.
    """
    global _policy_epoch
    with _lock:
        try:
            yield
        finally:
            _policy_epoch += 1


def current_policy_epoch() -> int:
    with _lock:
        return _policy_epoch


def _bump_epoch_locked(key: str) -> None:
    _epochs[key] = _epochs.get(key, 0) + 1


@contextlib.contextmanager
def session_transaction(key: str):
    """Hold the write fence across a change to THIS session's authorization, bumping its epoch
    on the way out.

    The per-session opt-out (`orchestrator_excluded`) is read by `check_precondition` inside
    the final guard — which runs BEFORE `_write_all` takes the lock. So an opt-out committing
    between the guard and byte one changed nothing the fence could see: the writer generation
    was untouched and the policy epoch is global, not per-session.

    Same shape and same reasoning as `policy_transaction`, scoped to one session so opting one
    out never cancels an in-flight send to another. Announce-after-writing is not sufficient
    for the same reason it wasn't for policy: between the write and the bump, the stored state
    has already changed while the epoch still reads old.
    """
    with _lock:
        try:
            yield
        finally:
            _bump_epoch_locked(key)


def bump_epoch(key: str) -> None:
    """Invalidate any in-flight authorization for ``key``.

    Called by the viewer-attach path, which lives in a different lock domain and therefore
    cannot be observed by a check — only fenced by this.
    """
    with _lock:
        _bump_epoch_locked(key)


def current_epoch(key: str) -> int:
    with _lock:
        return _epochs.get(key, 0)


def register_writer(key: str, fd: int, lock: threading.Lock, kind: str) -> int:
    """Declare that ``fd`` is the current byte-owner for ``key``. Returns a token the caller
    passes back to :func:`unregister_writer`.

    The token is what makes teardown safe against interleaving: an attach that lands while a
    detach is unwinding must not have its brand-new registration deleted by the old owner's
    cleanup. A stale token simply no-ops.
    """
    with _lock:
        token = next(_tokens)
        _writers[key] = _Writer(fd=fd, lock=lock, kind=kind, token=token)
        _bump_epoch_locked(key)
        return token


def unregister_writer(key: str, token: int) -> None:
    """Drop the registration IFF it is still the one this token minted."""
    with _lock:
        cur = _writers.get(key)
        if cur is not None and cur.token == token:
            del _writers[key]
            _bump_epoch_locked(key)


def current_writer(key: str) -> _Writer | None:
    with _lock:
        return _writers.get(key)


def borrow_writer(key: str) -> tuple[int, threading.Lock, int] | None:
    """Pin the current writer's file DESCRIPTION for the duration of a write.

    Returns ``(dup_fd, lock, token)``; the caller MUST ``os.close(dup_fd)`` when done. Returns
    ``None`` when nothing owns the session, and raises ``OSError`` when a registration exists
    but its fd is already dead.

    A plain ``current_writer()`` hands back an fd *number*, and a number is not a durable
    reference: teardown can close it and the kernel can hand the same integer to the next
    ``open`` — a pipe, a log file, anything. A write that lands there succeeds, so the seam
    reports ``delivered`` while the payload went somewhere else entirely and the real session
    got nothing. That is the worst possible failure for a component whose entire job is "write
    to the right terminal or refuse".

    ``os.dup`` under ``_lock`` fixes it on both sides:

    * The dup happens while the registration still exists, and teardown must take ``_lock`` in
      ``unregister_writer`` *before* it closes the master — so the fd cannot already be closed
      when we dup it.
    * The dup refers to the same open file description, not the number. If teardown closes the
      original a moment later, our copy still points at the ORIGINAL pty master, never at
      whatever reused the integer. Writing to it then fails with EIO/EBADF — which is
      fail-closed, and exactly what the caller should see.
    """
    with _lock:
        w = _writers.get(key)
        if w is None:
            return None
        # A dup failure here means the registration exists but its fd is already dead. That is
        # a BROKEN OWNER, not an absent one, and the caller reports them differently — so let
        # the OSError propagate rather than flattening it into "nobody owns this session".
        return os.dup(w.fd), w.lock, w.token


def is_live(key: str) -> bool:
    """True when some writer owns this session's bytes right now."""
    return current_writer(key) is not None


def reset() -> None:
    """Drop all registrations AND every epoch. Test hook — the registry is process-global.

    The epochs matter as much as the registrations: a stale `_screen_epochs` entry from a
    previous test makes the next one refuse a write for a screen change that never happened
    in it, which reads as a flaky fence rather than as leaked state.
    """
    global _policy_epoch
    with _lock:
        _writers.clear()
        _epochs.clear()
        _policy_epoch = 0
    with _screen_lock:
        _screen_epochs.clear()


def bracketed_paste(text: str) -> bytes:
    """Render text as one bracketed paste + CR — the same framing the handoff seed uses, so a
    TUI treats it as pasted content rather than a burst of keystrokes."""
    return b"\x1b[200~" + text.encode("utf-8", "replace") + b"\x1b[201~\r"


def _wait_quiet(key: str, deadline: float) -> bool:
    """Wait until the session has produced no output for ``QUIET_WINDOW_S``. Returns False if
    the deadline passes first — the caller then declines to write rather than typing into a
    mid-render TUI that will swallow it."""
    from . import scrollback

    while time.monotonic() < deadline:
        last = scrollback.get_last_output_at(key)
        if last is None or (time.time() - last) >= QUIET_WINDOW_S:
            return True
        time.sleep(0.05)
    return False


def send_input(
    key: str,
    payload: bytes,
    *,
    precondition: Callable[[], tuple[bool, str]] | None = None,
    final_guard: Callable[[], tuple[bool, str]] | None = None,
    policy_fingerprint: Callable[[], object] | None = None,
    timeout_s: float = WRITE_TIMEOUT_S,
    require_quiet: bool = True,
) -> Outcome:
    """Write ``payload`` into the live session ``key``. BLOCKING — run under
    ``asyncio.to_thread``.

    ``precondition`` is re-checked immediately before the first byte, under the same call that
    performs the write. That placement is the point: an engine-capability check, an opt-out, or
    a screen fingerprint verified minutes ago at proposal time proves nothing about *now*. It
    returns ``(ok, reason)``; a False verdict yields ``stale`` and writes nothing.
    """
    writer = current_writer(key)
    if writer is None:
        return Outcome("not_live", "no writer owns this session")
    # A PTY stuck out of raw mode accepts our bytes and gives them to the line discipline, not to
    # the agent (#804) — so the write "succeeds", the ledger records `delivered`, and nothing
    # happens. Repair before the readiness waits below, since a cooked terminal is exactly the
    # state in which "quiet" means the opposite of ready. Best-effort: an unresolvable or
    # ambiguous PTY is left alone and the delivery proceeds as it always did. This is the EARLY
    # check; it is re-made at the write boundary below, because the waits in between are long
    # enough for the terminal to revert underneath it (#805 r2).
    early_tty = tty_health.ensure_raw(key)
    # The generation every check below is made AGAINST. Bind it now: the precondition's verdict
    # ("no viewer, screen still looks right") describes THIS writer, so if the registry is
    # swapped before we write, that verdict says nothing about the new owner. Without this the
    # payload follows ownership onto a freshly attached PTY — a probe caught exactly that: the
    # headless pty received nothing and a newly attached one received the input.
    generation = writer.token
    epoch = current_epoch(key)
    # Policy is the third domain, and it lives in a file this module must not import. The
    # caller supplies a cheap fingerprint of whatever authority the write rests on; the fence
    # re-reads it before byte one and refuses on any change.
    policy_fp = policy_fingerprint() if policy_fingerprint is not None else None
    policy_epoch = current_policy_epoch()

    deadline = time.monotonic() + timeout_s
    if require_quiet and not _wait_quiet(key, min(deadline, time.monotonic() + QUIET_WAIT_MAX_S)):
        return Outcome("refused", "session never went quiet; not typing into a mid-render TUI")

    if precondition is not None:
        ok, reason = precondition()
        if not ok:
            return Outcome("stale", reason)

    # Raw mode is re-established HERE, at the write boundary — not merely before the waits
    # above (#805 r2). `_wait_quiet` alone can burn QUIET_WAIT_MAX_S, and a PTY that reverts
    # inside that window takes our bytes into a canonical line buffer while the ledger records
    # `delivered` — a delivery that provably did not happen. Same lesson as the confirm-window
    # race in `tty_health`: an authorization computed before a wait says nothing after it.
    #
    # What this refuses is deliberately narrow. Proven-cooked-and-unrepairable is a refusal:
    # we know the bytes cannot land. *Unresolvable* is not, because it is no worse than what
    # every delivery before this change knew — refusing there would trade a rare false
    # `delivered` for a common false failure. The one exception is a PTY that WAS resolvable at
    # the early check and is not now: that is not ignorance, it is a change of state under us.
    final_tty = tty_health.ensure_raw(key)
    if final_tty.status == tty_health.STUCK and not final_tty.repaired:
        return Outcome("refused", f"the session's PTY is consuming input ({final_tty.detail})")
    if early_tty.device is not None and final_tty.device != early_tty.device:
        return Outcome("refused", "the session's PTY changed while waiting for it to go quiet")
    # Re-asked once more under the write lock — see `_write_all`. Bound to the exact device
    # this verdict describes, so a recycled pts number cannot answer for it.
    tty_probe = None
    if final_tty.device is not None and final_tty.rdev is not None:
        _dev, _rdev = final_tty.device, final_tty.rdev
        tty_probe = lambda: tty_health.still_raw(_dev, _rdev)  # noqa: E731

    # Re-resolve AFTER the waits: an attach/detach may have flipped ownership while we waited,
    # and writing to the fd we looked up before the wait would be writing to a dead owner.
    # BORROW rather than read: this pins the file description for the whole write, so a
    # teardown racing us can never redirect our bytes into a reused fd number.
    try:
        borrowed = borrow_writer(key)
    except OSError as e:
        # Registered, but the fd is gone — the owner broke rather than departed.
        return Outcome("failed", f"the session's fd is no longer usable ({e.strerror})")
    if borrowed is None:
        return Outcome("not_live", "ownership was released while waiting")
    fd, lock, token = borrowed
    if token != generation:
        # Somebody else owns the bytes now. Every gate above was evaluated against the old
        # owner, so re-using them here would be authorising a write nobody checked.
        with contextlib.suppress(OSError):
            os.close(fd)
        return Outcome("stale", "ownership changed after the screen was checked")

    try:
        return _write_all(
            fd,
            lock,
            payload,
            deadline,
            key=key,
            token=token,
            epoch=epoch,
            policy_fp=policy_fp,
            policy_epoch=policy_epoch,
            policy_fingerprint=policy_fingerprint,
            final_guard=final_guard,
            tty_probe=tty_probe,
        )
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def _write_all(
    fd: int,
    lock: threading.Lock,
    payload: bytes,
    deadline: float,
    *,
    key: str = "",
    token: int = 0,
    epoch: int = 0,
    policy_fp: object = None,
    policy_epoch: int = 0,
    policy_fingerprint: Callable[[], object] | None = None,
    final_guard: Callable[[], tuple[bool, str]] | None = None,
    tty_probe: Callable[[], bool | None] | None = None,
) -> Outcome:
    """The chunked write itself, against an fd the caller has already pinned.

    ``final_guard`` is evaluated UNDER the write lock, immediately before the first byte, and
    nothing is written if it refuses. That placement is the whole point and it took several
    goes to get right: every earlier check — policy, viewer, screen fingerprint — happens
    before the quiet wait, the precondition callback, the fd borrow and the lock acquisition,
    which together can span seconds. An operator switching orchestration off in that window,
    or a browser attaching and starting to type, would otherwise be overruled by a verdict
    formed before they acted. A check that is not fenced against the actual write is a check
    that describes the past.

    Ownership is re-verified here too: if the registration changed while we queued for the
    lock, our pinned description is still valid but no longer the session's current writer.
    """
    view = memoryview(payload)
    written = 0
    failure = ""
    while written < len(payload):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            failure = "timeout"
            break
        try:
            # Cheap writability wait OUTSIDE the lock, so an idle wait never holds up the
            # browser's own input path…
            select.select([], [fd], [], min(0.5, remaining))
            if not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
                failure = "timeout"
                break
            try:
                # …then the authoritative re-check UNDER it. With the only other writer
                # excluded, confirmed room cannot vanish before our write, so a bounded chunk
                # is guaranteed not to block.
                if written == 0:
                    # The authoritative gate, under the lock, before ANY byte. Only on the
                    # first chunk: aborting mid-payload would leave a half-typed line in a
                    # real session, which is worse than either outcome (see the abort rule).
                    # SEQLOCK read, captured BEFORE the guard reads the screen.
                    #
                    # An ODD value means ingestion is mid-flight: the ring is being mutated
                    # right now, so whatever the guard is about to read is not a settled
                    # screen. Refuse rather than approve against it.
                    screen_epoch = current_screen_epoch(key) if key else 0
                    if key and screen_epoch % 2 == 1:
                        return Outcome("stale", "the screen was changing as this was checked")
                    if final_guard is not None:
                        ok, why = final_guard()
                        if not ok:
                            return Outcome("stale", why)

                    # Ownership is re-verified AFTER the guard and, critically, while HOLDING
                    # the registry lock across the first write. `register_writer` and
                    # `unregister_writer` both take that lock, so this is what actually makes
                    # the sequence atomic — checking the token and then writing leaves a gap
                    # in which a browser can attach (payload lands on the old pty) or the
                    # owner can be released (payload lands on a pinned fd nobody owns). The
                    # guard callback itself does I/O, so it runs BEFORE this, not inside it;
                    # what runs inside is only the check plus one bounded write.
                    # THE FENCE. Held across the WHOLE payload, not just this chunk: an
                    # owner swap after chunk one used to send the remainder to the old
                    # terminal while the ledger reported success. Everything that could
                    # invalidate this authorization — register/unregister, viewer attach or
                    # detach — must take `_lock` to bump the epoch, so none of it can
                    # interleave with the send.
                    with _lock:
                        # The PTY proof lives INSIDE the fence (#805 r4), not before it.
                        #
                        # It was one line above until the review disproved the reason it was
                        # there: I claimed acquiring `_lock` was an I/O-free instant. It is
                        # not. `policy_transaction()` and `session_transaction()` hold `_lock`
                        # across the caller's whole body, and those bodies do persisted
                        # writes — `prefs._mutate(...)` and `metadata.patch(...)`. So queueing
                        # for `_lock` is a real, file-I/O-bound wait, and a probe taken before
                        # it describes the past exactly like every earlier version did.
                        #
                        # No deadlock is reintroduced: the hazard the module warns about is
                        # reading PREFS under `_lock` (that inverts against the prefs flock).
                        # `still_raw` touches one pty device and nothing else.
                        #
                        # `is not True` — not `is False`. A closure exists ONLY because this
                        # delivery already resolved and bound a concrete (device, rdev), so
                        # `None` here means that bound PTY stopped being resolvable, which is
                        # a state change under us, not the ignorance the never-resolvable
                        # policy tolerates. That distinction is the whole reason the closure
                        # is bound rather than re-resolved.
                        if tty_probe is not None and tty_probe() is not True:
                            return Outcome(
                                "refused",
                                "could not prove the session's PTY still accepts input at the "
                                "write boundary",
                            )
                        cur = _writers.get(key) if key else None
                        # Fail CLOSED on an absent writer: the previous form only compared
                        # tokens when `cur is not None`, so a released session read as
                        # success and the payload went to a pinned fd nobody owned.
                        if cur is None or cur.token != token:
                            failure = "ownership changed while waiting for the write lock"
                            break
                        if _epochs.get(key, 0) != epoch:
                            failure = "a viewer attached or ownership moved during authorization"
                            break
                        # Integer compare, no I/O: reading prefs here would invert the lock
                        # order against `set_orchestrator` and deadlock both.
                        if _policy_epoch != policy_epoch:
                            return Outcome("stale", "policy changed before the write")
                        # The seqlock's second half — and it must RESERVE the screen, not
                        # merely observe it. Comparing and then writing leaves ingestion free
                        # to enter and complete an interval in between: the compare sees the
                        # matching even value, `PROMPT B` lands, and byte one goes into it.
                        #
                        # So the comparison and the FIRST byte happen while holding
                        # `_screen_lock`, which `screen_change()` needs to open an interval.
                        # Ingestion therefore either finishes before the compare or starts
                        # after the input is committed — never between.
                        #
                        # Held for the first chunk ONLY. That is what makes this affordable:
                        # the pty reader is blocked for one bounded write, not for the whole
                        # payload. Read `_screen_epochs` directly rather than through
                        # `current_screen_epoch`, which would re-enter this non-reentrant lock.
                        #
                        # Lock order is one-way: this path takes `_lock` then `_screen_lock`;
                        # ingestion takes `_screen_lock` alone and never wants `_lock`.
                        first = True
                        while written < len(payload):
                            remaining_now = deadline - time.monotonic()
                            if remaining_now <= 0:
                                failure = "timeout"
                                break
                            if first:
                                with _screen_lock:
                                    if key and _screen_epochs.get(key, 0) != screen_epoch:
                                        return Outcome(
                                            "stale", "the screen changed before the write"
                                        )
                                    _, writable, _ = select.select([], [fd], [], 0)
                                    if not writable:
                                        select.select([], [fd], [], min(0.5, remaining_now))
                                        continue
                                    written += os.write(fd, view[:WRITE_CHUNK])
                                first = False
                                continue
                            _, writable, _ = select.select([], [fd], [], min(0.5, remaining_now))
                            if not writable:
                                continue
                            written += os.write(fd, view[written : written + WRITE_CHUNK])
                    break
                _, writable, _ = select.select([], [fd], [], 0)
                if not writable:
                    continue
                written += os.write(fd, view[written : written + WRITE_CHUNK])
            finally:
                lock.release()
        except OSError as e:
            failure = "closed" if e.errno in (errno.EBADF, errno.EIO, errno.EPIPE) else "error"
            break

    if not failure:
        return Outcome("delivered")
    if written == 0:
        # Nothing reached the TUI — clean to retry later.
        return Outcome("failed", f"write {failure} before any byte was sent")
    # An unterminated bracketed paste IS on the target's stdin. Replaying would corrupt the
    # prompt; the honest move is to consume the action and say so.
    return Outcome("aborted", f"partial write ({failure}, {written}/{len(payload)} bytes)")


@contextlib.contextmanager
def writer_registered(key: str, fd: int, lock: threading.Lock, kind: str):
    """Scoped registration — the shape both owners use, so neither can forget to release."""
    token = register_writer(key, fd, lock, kind)
    try:
        yield
    finally:
        unregister_writer(key, token)
