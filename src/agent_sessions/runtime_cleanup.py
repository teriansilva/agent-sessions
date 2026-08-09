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

import contextlib

from . import engines, owner, ptybridge, reaper, scrollback, sessionlock


async def cleanup_runtime(engine: str, native: str, *, spare_if=None) -> str:
    """Free the live runtime footprint of ``engine:native``; return the master outcome.

    Resolves the PHYSICAL key first (alias → real id), terminates the ``dtach`` master via
    the shared reaper path (SIGTERM → grace → SIGKILL, frees the VT mirror), then clears the
    scrollback ring + VT mirror, the owner lease, and any socket the master left behind on a
    hard kill — the socket unlink guarded by the single-writer lock (split-brain guard).

    ``spare_if`` (optional) is forwarded to :func:`reaper.terminate_master` and re-checked
    before each signal; if it ever returns ``False`` the master is SPARED and *no* cleanup
    runs (outcome ``"spared"``), so a session a viewer just (re)claimed is left intact.
    Archive passes no guard — it is an explicit operator action ⇒ force-style cleanup.

    Returns the ``terminate_master`` outcome: ``"gone" | "spared" | "term" | "kill"``.
    Best-effort: every teardown step past the terminate is exception-suppressed; the caller
    should still wrap the whole call so even a resolution error can't block its own work.
    """
    phys_key = engines.physical_key(f"{engine}:{native}")
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
