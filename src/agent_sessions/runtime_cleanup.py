"""Reclaim a session's live runtime footprint — shared teardown for archive (#523).

Archiving a session (the operator's explicit "I'm done with this") should free the
resources a *launched* session holds — the ``dtach`` master + agent process group, the
scrollback ring + VT mirror, the takeover-mode owner lease, and the now-stale socket —
while leaving the on-disk transcript untouched so the session stays fully resumable. The
inherited single-writer lock is released for free: the kernel drops it when the master
closes its fd on death, so a later relaunch is seen as ``LAUNCH``, never a phantom ``BUSY``.

The teardown sequence here is reconstructed (by reference, not import) from the manual
session-restart route removed in #503 — final form at commit ``f6db25d`` (the #503 parent),
**not** the initial ``a788fc8``, which predates the split-brain socket guard. The lock-guarded
unlink keeps that guard (the 2026-06-12 prod wedge): only a socket whose single-writer lock
is *acquirable* is a stale leftover safe to remove; if the lock is *held*, a NEW master
generation owns the path — leave it alone, or we orphan a fresh master and 4409-loop forever.

Every step is best-effort: a missing master, an already-dead socket, or a scrollback hiccup
must never block the caller (the archive flag/file move still has to land). Resources are
addressed by the PHYSICAL session key, so a reconciled opencode/codex placeholder alias
resolves to its real id — the same mapping ``terminal.py`` keys live resources by.

No shell anywhere: this only orchestrates the existing argv-list / syscall helpers.
"""

from __future__ import annotations

import asyncio
import contextlib

from . import engines, owner, ptybridge, reaper, scrollback, sessionlock


class UnresolvableRuntime(RuntimeError):
    """Where a session's runtime lives could not be established (#994 review 2, finding 1).

    Raised before anything is signalled. A caller must not record the session as archived on the
    strength of a teardown that could not even name its target: the agent is very possibly still
    running under a key nobody looked at.
    """


async def resolve_runtime_key(engine: str, native: str) -> str:
    """The PHYSICAL key ``engine:native``'s runtime lives under. Raises `UnresolvableRuntime`.

    The alias answers first, as it always has (#127). **A late-id engine's real key can have no
    alias and still be mapped** (#989): a mission that adopted a late-bound session records the
    placeholder in its store in the adopting commit and publishes the alias afterwards, so a failed
    publication — or a restart before the repair pass — leaves the real key resolving to itself
    while the master runs under the placeholder. Archive then terminated a runtime that does not
    exist and reported the session archived, with the agent still running. The store's mapping is
    read in that case, and a store that cannot be read refuses rather than guessing.

    Pinned-id engines never have a mapping and never read the store; nor does a placeholder, which
    IS the physical key.
    """
    logical = f"{engine}:{native}"
    phys = engines.physical_key(logical)
    if phys != logical or engines.is_new_session_placeholder(logical):
        return phys
    if not getattr(engines.get(engine), "new_session_reconciles", False):
        return phys
    from . import missions  # lazy: this module is the teardown, not a mission-store dependency

    try:
        stored = await asyncio.to_thread(missions.physical_key_of, logical)
    except Exception as e:  # noqa: BLE001 — an unreadable mapping is not permission to guess
        raise UnresolvableRuntime(
            f"could not tell where {logical}'s runtime lives ({type(e).__name__}); "
            "nothing was stopped or archived"
        ) from e
    return stored or phys


async def cleanup_runtime(
    engine: str, native: str, *, spare_if=None, physical_key: str | None = None
) -> str:
    """Free the live runtime footprint of ``engine:native``; return the master outcome.

    Resolves the PHYSICAL key first (alias → real id), terminates the ``dtach`` master via
    the shared reaper path (SIGTERM → grace → SIGKILL, frees the VT mirror), then clears the
    scrollback ring + VT mirror, the owner lease, and any socket the master left behind on a
    hard kill — the socket unlink guarded by the single-writer lock (split-brain guard).

    ``spare_if`` (optional) is forwarded to :func:`reaper.terminate_master` and re-checked
    before each signal; if it ever returns ``False`` the master is SPARED and *no* cleanup
    runs (outcome ``"spared"``), so a session a viewer just (re)claimed is left intact.
    Archive passes no guard — it is an explicit operator action ⇒ force-style cleanup.

    Returns the ``terminate_master`` outcome:
    ``"gone" | "spared" | "term" | "kill" | "leaked"``. ``"leaked"`` means something in the
    session's process group survived SIGKILL — the local state below is still cleaned, but a
    caller that reports "stopped" on it is reporting a process that is still running (#898).
    **Best-effort is the TEARDOWN, not the resolution** (#994 review 3). Every step past the
    terminate is exception-suppressed, and a caller may wrap the whole call so a teardown hiccup
    never blocks its own work — but `UnresolvableRuntime` is a different answer: nothing was
    signalled because nothing could be named, and an archive that swallows it records a session
    archived while its agent keeps running. Archive callers therefore resolve first (see
    `resolve_runtime_key`) and refuse on that, suppressing only what comes after.
    """
    # The caller's resolution when it made one — archive resolves first so that a runtime it cannot
    # locate is refused rather than archived past — otherwise resolved here (#994 review 2).
    phys_key = physical_key or await resolve_runtime_key(engine, native)
    _eng, _, phys_native = phys_key.partition(":")

    # Kill the master (and its agent process group). The single-writer lock the master
    # inherited is released by the kernel when it dies — no explicit unlock needed.
    outcome = await reaper.terminate_master(engine, phys_native, key=phys_key, spare_if=spare_if)
    if outcome == "spared":
        return outcome

    # Local terminal state: persisted scrollback + in-memory ring + VT mirror, then the
    # now-meaningless owner lease.
    with contextlib.suppress(Exception):
        scrollback.clear_scrollback([phys_key])
    with contextlib.suppress(Exception):
        owner.clear_owner(engine, phys_native)

    # Stale-socket unlink under the single-writer lock (split-brain guard, 2026-06-12 prod
    # wedge): acquirable ⇒ no live master/launcher generation ⇒ the sock is a stale leftover,
    # safe to remove; held ⇒ a NEW generation owns the path — leave its socket alone.
    with contextlib.suppress(Exception):
        lk = sessionlock.acquire(phys_key)
        if lk is not None:
            try:
                with contextlib.suppress(OSError):
                    ptybridge.socket_path(engine, phys_native).unlink()
            finally:
                lk.release()

    return outcome
