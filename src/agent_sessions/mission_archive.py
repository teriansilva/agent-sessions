"""Archiving a mission takes its sessions with it — the durable bridge (#846, #840 §11).

A done mission is clutter with a runtime cost: its sessions still hold a ``dtach`` master, an
agent process group, scrollback and the single-writer lock. So archiving a mission archives its
sessions, reusing the reviewed path exactly — :func:`runtime_cleanup.cleanup_runtime` then
``prov.archive`` per session (#523) — rather than growing a second teardown.

**Why this is a three-step operation and not one transaction.** #840 §11 says the
abandon-then-archive pair happens "inside one transaction". It cannot: ``cleanup_runtime`` and
``prov.archive`` are external process and filesystem effects, they can partially succeed across
sessions, and a SQLite transaction must never be held across an ``await``. So the durable state
lives *between* the steps instead:

1. :func:`missions.begin_archive` — one transaction. Terminal-state-only (a live mission without
   an explicit ``abandon`` is a 409, never a prompt); stamps ``archiving_at`` and marks every
   active session ``archive_state='pending'``.
2. :func:`_teardown_session` per session, **outside any transaction**, each settling in its own
   tiny transaction.
3. :func:`missions.finish_archive` — one transaction: ``archived_at``, release what is still
   held, and name whatever did not archive.

**Idempotent, because the provider is not.** ``ClaudeProvider.archive`` is two effects, not one:
``archive.archive()`` ``shutil.move``s the live JSONL into the archive tree and *then*
``metadata.patch(archived=True)`` stamps the sidecar. A crash between them leaves the file moved
and the sidecar unset, and the retry raises ``ArchiveError("… not found to archive")`` — a **false
failure** against a session that is in fact already archived. :func:`_archive_idempotent` closes
that: it checks the effective archived state before calling the provider, and on ``ArchiveError``
re-checks it, completing the torn sidecar stamp and settling ``already_archived`` rather than
recording a failure that never happened.

**Honest about partial failure.** A session that cannot be archived does not abort the mission's
archive; the mission archives and the timeline names the session that survived.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from . import archive as archive_mod
from . import engines, metadata, missions, runtime_cleanup, transcript_owner

log = logging.getLogger("agent_sessions.mission_archive")


def _effective_archived(prov, native: str) -> bool | None:
    """Is ``engine:native`` already archived? ``None`` when it cannot be determined.

    The sidecar override wins where it is set — that is the same precedence ``pulse.build_cards``
    and the sidebar use. Otherwise the engine's own scan is asked, which is what sees a JSONL that
    has already been moved into the archive tree by a torn provider archive.
    """
    key = f"{prov.engine_id}:{native}"
    with contextlib.suppress(Exception):
        m = metadata.load().get(key)
        if m is not None and m.archived is not None:
            return bool(m.archived)
    try:
        for s in prov.scan():
            if getattr(s, "uuid", None) == native:
                return bool(getattr(s, "archived", False))
    except Exception:  # noqa: BLE001 — an unreadable engine store must not decide anything
        return None
    return None


def _tree_archived(prov, native: str) -> bool | None:
    """What the ENGINE'S OWN STORE says, with the sidecar override deliberately ignored.

    The two directions need **opposite** precedence, which is the whole reason a symmetric-looking
    wrapper failed twice:

    * *Is it archived?* — the sidecar wins. In the archive direction the torn state leaves the
      sidecar **unset**, so falling through to the scan answers correctly.
    * *Has it come back?* — the **tree** wins. In the restore direction the torn state leaves the
      sidecar **set and wrong** (it still says archived from the archive that preceded it), so
      asking the sidecar first reports "archived" about a file that is already live, the retry
      raises, and a landed move is recorded as a failure.

    ``None`` when the engine's store cannot be read, which decides nothing either way.
    """
    try:
        for s in prov.scan():
            if getattr(s, "uuid", None) == native:
                return bool(getattr(s, "archived", False))
    except Exception:  # noqa: BLE001 — an unreadable engine store must not decide anything
        return None
    return None


def _archive_idempotent(prov, native: str) -> tuple[str, str]:
    """Archive one session; return ``(outcome, reason)`` with ``outcome`` in the archive states.

    The three branches are the whole point:

    * **already archived** ⇒ ``already_archived`` without calling the provider at all;
    * **``ArchiveError`` but archived on re-read** ⇒ the move landed and only the sidecar was
      missing, so the torn write is completed and it settles ``already_archived``;
    * **anything else** ⇒ an honest ``failed`` with the reason.
    """
    if _effective_archived(prov, native) is True:
        # Complete a possibly-torn stamp so the effective state stops depending on the tree.
        with contextlib.suppress(Exception):
            metadata.patch(f"{prov.engine_id}:{native}", archived=True)
        return "already_archived", ""
    try:
        prov.archive(native)
        return "done", ""
    except archive_mod.ArchiveError as e:
        if _effective_archived(prov, native) is True:
            with contextlib.suppress(Exception):
                metadata.patch(f"{prov.engine_id}:{native}", archived=True)
            return "already_archived", ""
        return "failed", str(e) or "archive failed"
    except NotImplementedError:
        return "failed", f"archive not supported for engine {prov.engine_id}"


def _unarchive_idempotent(prov, native: str) -> tuple[str, str]:
    """Restore one session, idempotently across the provider's own two-step write.

    `ClaudeProvider.unarchive` moves the JSONL back and *then* clears the sidecar. A crash between
    them leaves the file live with the sidecar still saying archived — and a retry raises
    "not found to unarchive", recording a **false failure** over a transcript that is already back.

    The check is against :func:`_tree_archived`, not the sidecar-first
    :func:`_effective_archived`: in this direction the sidecar is exactly the thing that is wrong.
    A tree that says live while the sidecar says archived **is** the torn state, and the repair is
    to finish the stamp the crash interrupted.

    Engines whose archive is a pure sidecar toggle (opencode, shell, …) never reach the torn
    branch — their unarchive is a single write — and pass through unchanged.
    """
    key = f"{prov.engine_id}:{native}"
    if _tree_archived(prov, native) is False:
        return _repair_sidecar(key)
    try:
        prov.unarchive(native)
        return "restored", ""
    except archive_mod.ArchiveError as e:
        if _tree_archived(prov, native) is False:
            return _repair_sidecar(key)
        return "restore_failed", str(e) or "unarchive failed"
    except NotImplementedError:
        return "restore_failed", f"unarchive not supported for engine {prov.engine_id}"


def _repair_sidecar(key: str) -> tuple[str, str]:
    """Finish the stamp the crash interrupted. **A failed repair is a failed restore.**

    Suppressing the error and reporting ``restored`` anyway looked harmless and is not: the
    sidecar still says archived, and `_effective_archived` gives it precedence — so the app goes
    on treating a live transcript as archived, which is the exact split-brain this repair exists
    to end. The caller would then clear the session's archive state and the mission's fence over
    the top of it.

    Note the asymmetry with :func:`_archive_idempotent`, which *does* suppress its equivalent: in
    the archive direction the tree and the sidecar AGREE after a torn write (both say archived,
    one merely implicitly), so the stamp is an optimisation. Here they disagree, and the stamp is
    the only thing that resolves it.
    """
    try:
        metadata.patch(key, archived=False)
    except Exception as e:  # noqa: BLE001 — the reason is the operator's, whatever its type
        return (
            "restore_failed",
            f"the transcript is back but the archived flag could not be cleared "
            f"({type(e).__name__}); the session would still read as archived",
        )
    return "restored", ""


async def _teardown_session(mission_id: str, session_key: str) -> str:
    """Free one session's runtime, archive it, and settle its row. Returns the outcome.

    **The lease is taken first, and it decides whether anything happens at all.** Two things are
    only knowable at this moment, not at plan time:

    * whether another worker is already performing this session's teardown — ``begin_archive``
      hands the *same* pending rows to a retry, and ``prov.archive`` is not idempotent in
      general, so an unclaimed second pass runs the destructive effect twice;
    * whether another open mission has adopted this session since the roster was snapshotted —
      reaching a terminal state released the key, so that is a legal thing to have happened, and
      tearing it down would kill a live agent belonging to a different mission.

    Terminate-first after that, exactly as ``POST /api/sessions/{sid}/archive`` does, so a
    still-running agent cannot recreate its transcript between the move and the kill.
    """
    verdict, token = await missions.run_admitted(
        lambda: missions.claim_session_teardown(mission_id, session_key)
    )
    if verdict != "claimed":
        # "skipped" already settled itself with the holder's name; "taken"/"gone" are somebody
        # else's business. Either way this worker touches nothing external.
        return verdict
    try:
        return await _teardown_claimed(mission_id, session_key, token)
    except BaseException as e:
        # The lease is ours and the worker is dying — settle it here rather than leaving it for
        # recovery. Recovery only reopens leases owned by a process that is GONE, so an exception
        # inside a live process would otherwise strand this row for the life of the app.
        why = f"teardown raised {type(e).__name__}"
        with contextlib.suppress(Exception):
            await missions.run_admitted(
                lambda: missions.settle_session_archive(
                    mission_id, session_key, "failed", reason=why, token=token
                )
            )
        raise


async def _teardown_claimed(mission_id: str, session_key: str, token: str | None) -> str:
    """The teardown itself, with the lease already held. ``token`` fences every settlement.

    Wrapped in :func:`missions.holding` because everything below spans calls this process does not
    control — a process group that may take its time dying, a file move on a loaded filesystem. The
    claim is renewed for exactly as long as the work actually runs, so its expiry means "the holder
    stopped", never "the holder is slow".
    """
    async with missions.holding(session_key, token):
        return await _teardown_effect(mission_id, session_key, token)


async def _teardown_effect(mission_id: str, session_key: str, token: str | None) -> str:
    try:
        prov, native = engines.parse_key(session_key)
    except engines.EngineError:
        await missions.run_admitted(
            lambda: missions.settle_session_archive(
                mission_id, session_key, "failed", reason="unknown session id", token=token
            )
        )
        return "failed"

    with contextlib.suppress(Exception):
        await runtime_cleanup.cleanup_runtime(prov.engine_id, native)

    # Background-agent guard (#631): with our own master gone, a live process STILL holding this
    # transcript is a Claude background agent we don't manage. Moving its open JSONL would make
    # the file diverge between the live and archive trees — refuse rather than corrupt.
    if prov.engine_id == "claude" and transcript_owner.transcript_is_owned(native):
        await missions.run_admitted(
            lambda: missions.settle_session_archive(
                mission_id,
                session_key,
                "failed",
                reason="running background agent — not archivable",
                token=token,
            )
        )
        return "failed"

    outcome, reason = _archive_idempotent(prov, native)
    await missions.run_admitted(
        lambda: missions.settle_session_archive(
            mission_id, session_key, outcome, reason=reason, token=token
        )
    )
    with contextlib.suppress(Exception):
        engines.invalidate_scan_cache()  # a moved JSONL means the next list must re-walk (#561)
    return outcome


async def archive_mission(mission_id: str, *, abandon: bool = False) -> dict:
    """Archive a mission and every session in its roster. The full three-step operation.

    Raises :class:`missions.MissionError` from step 1 — a live mission without ``abandon`` is a
    409 there, before anything is torn down.
    """
    begun = await missions.run_admitted(lambda: missions.begin_archive(mission_id, abandon=abandon))
    for row in begun["sessions"]:
        await _teardown_session(mission_id, row["session_key"])
    return await missions.run_admitted(lambda: missions.finish_archive(mission_id))


async def unarchive_mission(mission_id: str, *, sessions: bool = True) -> dict:
    """Reverse it — **claim first, then move files**. History was never destroyed.

    The claim is what races, not the provider calls: a second concurrent request is refused
    *before* it moves anything, the requested mode travels with the claim so recovery finishes
    this operation rather than a differently-shaped one, and each restore is leased so the same
    session cannot be unarchived twice by two callers.

    Unarchiving the sessions is offered rather than forced (``sessions=False`` leaves them
    archived), and it never reaps: an unarchived session relaunches from its transcript exactly as
    it does today.

    **Calling it again retries what did not come back.** A session that will not restore does not
    fence its mission — that would park the record on one bad session for the life of the process —
    but it does stay *reserved*, so nobody can adopt a session that is still archived, and a second
    call claims the mission again to retry exactly those rows.
    """
    claim = await missions.run_admitted(
        lambda: missions.begin_unarchive(mission_id, sessions=sessions)
    )
    restored = await _restore_sessions(mission_id) if claim["sessions"] else []
    out = await missions.run_admitted(lambda: missions.finish_unarchive(mission_id))
    out["sessions"] = restored
    return out


async def _restore_sessions(mission_id: str) -> list[dict]:
    """Restore each archived session under its own lease. Failures are recorded, not fatal."""
    restored: list[dict] = []
    for row in await missions.run_admitted(lambda: missions.archive_sessions_for(mission_id)):
        key = row["session_key"]
        if row.get("archive_state") not in ("done", "already_archived", "restore_failed"):
            continue
        verdict, token = await missions.run_admitted(
            lambda k=key: missions.claim_session_restore(mission_id, k)
        )
        if verdict != "claimed":
            continue  # somebody else owns this restore, or the row is gone
        # Renewed for as long as the restore actually runs — see `_teardown_claimed`.
        async with missions.holding(key, token):
            try:
                prov, native = engines.parse_key(key)
                outcome, reason = _unarchive_idempotent(prov, native)
            except engines.EngineError as e:
                outcome, reason = "restore_failed", str(e) or type(e).__name__
            except BaseException as e:
                # Settle our own lease before dying, for the reason above.
                why = f"restore raised {type(e).__name__}"
                with contextlib.suppress(Exception):
                    await missions.run_admitted(
                        lambda k=key, w=why, t=token: missions.settle_session_restore(
                            mission_id, k, "restore_failed", reason=w, token=t
                        )
                    )
                raise
            await missions.run_admitted(
                lambda k=key, o=outcome, r=reason, t=token: missions.settle_session_restore(
                    mission_id, k, o, reason=r, token=t
                )
            )
        restored.append(
            {"session_key": key, "result": "unarchived" if outcome == "restored" else "failed"}
            | ({"reason": reason} if reason else {})
        )
    with contextlib.suppress(Exception):
        engines.invalidate_scan_cache()
    return restored


class _WorklistUnavailable(RuntimeError):
    """The recovery worklist itself could not be read — retryable, not "nothing to do"."""


async def resume_pending_operations() -> dict:
    """Re-drive every archive or unarchive that began and never finished. Called at boot.

    A crash can land between any two steps of either operation, so recovery re-lists the work and
    runs it again — safe because :func:`_archive_idempotent` makes a repeated teardown a no-op
    rather than a false failure, and because a stale lease at boot can only be a crashed worker's.

    **Failures are reported, not swallowed.** Nothing logged here carries mission content: ids and
    failure kinds only.
    """
    out: dict[str, list[str]] = {"archived": [], "unarchived": [], "failed": []}
    try:
        archives = await missions.run_admitted(missions.pending_archives)
        unarchives = await missions.run_admitted(missions.pending_unarchives)
    except Exception as e:  # noqa: BLE001
        # NOT an empty result. Returning `failed=[]` here told the retry wrapper the pass had
        # succeeded with nothing to do, so a transient store error consumed the whole retry
        # budget in one attempt and left the missions fenced.
        log.warning("mission recovery could not read its worklist: %s", type(e).__name__)
        raise _WorklistUnavailable from e

    for mission_id in archives:
        try:
            # Reopen both kinds of stale lease FIRST — one still open at boot can only be a
            # crashed worker's, and leaving it would make `finish_*` refuse forever.
            await missions.run_admitted(lambda mid=mission_id: missions.reopen_stale_leases(mid))
            # `resume=True` is the ONLY way to take over a claim somebody else began. A request
            # is refused instead, because handing it the worklist is how a second caller came to
            # finalise an archive while the first worker was still inside `cleanup_runtime`.
            begun = await missions.run_admitted(
                lambda mid=mission_id: missions.begin_archive(mid, resume=True)
            )
            token = begun["op_token"]
            for row in begun["sessions"]:
                await _teardown_session(mission_id, row["session_key"])
            await missions.run_admitted(
                lambda mid=mission_id, t=token: missions.finish_archive(mid, op_token=t)
            )
            out["archived"].append(mission_id)
        except Exception as e:  # noqa: BLE001 — one stuck mission must not block the others
            log.warning(
                "mission %s: archive recovery failed (%s: %s)", mission_id, type(e).__name__, e
            )
            out["failed"].append(mission_id)

    for mission_id in unarchives:
        try:
            # `resume=True` is the ONLY way to take over a live claim, and the mode comes back
            # from the claim rather than from a default — recovery finishes the operation that
            # was actually started.
            await missions.run_admitted(lambda mid=mission_id: missions.reopen_stale_leases(mid))
            claim = await missions.run_admitted(
                lambda mid=mission_id: missions.begin_unarchive(mid, resume=True)
            )
            if claim["sessions"]:
                await _restore_sessions(mission_id)
            await missions.run_admitted(lambda mid=mission_id: missions.finish_unarchive(mid))
            out["unarchived"].append(mission_id)
        except Exception as e:  # noqa: BLE001
            log.warning("mission %s: unarchive recovery failed (%s)", mission_id, type(e).__name__)
            out["failed"].append(mission_id)
    return out


async def recover_with_retry(attempts: int = 3, delay: float = 30.0) -> dict:
    """:func:`resume_pending_operations` with a bounded retry, so a transient error at boot does
    not fence a mission for the life of the process. Returns the last result.

    A worklist read failure is **retryable** and consumes an attempt without ending the loop —
    treating it as success was how a transient store error used to burn the whole budget on the
    first pass.

    The delay between attempts **stretches to cover an outstanding lease**. Reclamation waits for a
    proven expiry now (:func:`missions.reopen_stale_leases`), so after a crash the sessions this
    pass most needs are precisely the ones it may not touch yet; retrying on a fixed 30s would
    spend every attempt before the first of them became reclaimable and then declare the mission
    unrecoverable. Bounded by the expiry itself, so this can never wait longer than it takes for
    the answer to change.
    """
    result: dict = {"archived": [], "unarchived": [], "failed": []}
    for attempt in range(max(1, attempts)):
        try:
            result = await resume_pending_operations()
            if not result["failed"]:
                return result
            unresolved = len(result["failed"])
        except _WorklistUnavailable:
            result = {"archived": [], "unarchived": [], "failed": ["<worklist>"]}
            unresolved = 1
        if attempt + 1 < attempts:
            wait = delay
            with contextlib.suppress(Exception):
                if (until := missions.next_lease_expiry()) is not None:
                    wait = max(delay, min(until - time.time() + 1.0, missions.LEASE_MAX_AGE_S + 1))
            log.warning("mission recovery: %d unresolved, retrying in %.0fs", unresolved, wait)
            await asyncio.sleep(wait)
    if result["failed"]:
        log.error(
            "mission recovery gave up with %d unresolved: %s",
            len(result["failed"]),
            ", ".join(result["failed"]),
        )
    return result


__all__ = [
    "archive_mission",
    "recover_with_retry",
    "resume_pending_operations",
    "unarchive_mission",
]
