"""A session started from a specialist (#1693 S4b, D7): the launch side of the capacity ledger.

* **A new session** names ``{id, version}``. The version is re-validated where the launch happens:
  it must exist, run on THIS agent and the model this launch resolved (a client that changed the
  agent or model after choosing a specialist launches a plain session, or is refused here). Then a
  slot is reserved (`specialists_store.reserve`, one transaction against the specialist's and the
  pool's limits), the version's skills are copied into a bundle named by the reservation
  (`skill_bundles.build`, refused if a skill changed since the version was saved), and only then
  is the agent launched, with that bundle and the specialist's notebook for this project.
* **A relaunch** (a terminal reopened, a dispatch resuming it) of a session that was started from
  a specialist re-reserves the SAME version (`for_relaunch`): capacity is enforced at every seam
  where a specialist's agent is born, and the agent is never relaunched without its skills.
* **Release on every exit.** A refusal after the reservation releases it at once (`abandon`).
  After a launch, `settle` asks the session's liveness the way reconcile does: dead → released,
  live → ``live``; unknown leaves the slot ``starting`` for reconcile, never released on a guess.
  Only the attempt that created a slot (its owner token) settles it; a re-take (``created`` False)
  holds no token, so reconcile settles it on proof.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass

from . import model_grades, skill_bundles, specialist_capacity
from . import specialists_store as store

log = logging.getLogger("agent_sessions.specialist_launch")

#: How long a launch's watch waits for its master before leaving the slot to reconcile.
LIVE_WAIT_S = 60.0
_LIVE_POLL_S = 0.25


class Refused(Exception):
    """The launch can't start from this specialist; nothing runs. ``detail`` is for the operator."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True)
class Claim:
    """One launch's hold on a slot."""

    reservation_id: str
    session_key: str
    specialist_id: str
    version: int
    #: The token `reserve` gave the attempt that created the slot; None on a re-take.
    owner: str | None
    #: The skill bundle's absolute path, or None for a specialist without skills.
    bundle: str | None


def parse(raw_id: object, raw_version: object) -> tuple[str, int] | None:
    """``(id, version)`` from a request, None when neither is given, or `Refused`."""
    if raw_id in (None, "") and raw_version in (None, ""):
        return None
    if not isinstance(raw_id, str) or not store.ID_RE.fullmatch(raw_id):
        raise Refused("unknown specialist")
    if isinstance(raw_version, str) and raw_version.isascii() and raw_version.isdigit():
        raw_version = int(raw_version) if len(raw_version) <= 6 else None
    if isinstance(raw_version, bool) or not isinstance(raw_version, int) or raw_version < 1:
        raise Refused("a specialist launch names the version it starts from")
    return raw_id, raw_version


def _version(sid: str, n: int) -> dict:
    try:
        return store.version(sid, n)
    except store.NotFound:
        raise Refused("this specialist version no longer exists") from None
    except store.StoreError as exc:
        raise Refused(str(exc)) from None


def for_new(engine: str, session_key: str, sid: str, n: int, *, model: str | None) -> Claim:
    """Reserve and bundle for a NEW session of ``engine`` on ``model`` (None: the agent's
    default). Off the event loop: it takes the store's write transaction and copies files."""
    v = _version(sid, n)
    if v["engine"] != engine:
        raise Refused(f"this specialist runs on {v['engine']}, not {engine}")
    if v["model"] != (model or model_grades.DEFAULT_MODEL):
        raise Refused(f"this specialist runs on the model {v['model']}; choose it to start one")
    return _take(sid, n, session_key, v)


def relaunch_model(prov, session_keys: list[str], requested: object) -> object:
    """The model a relaunch asks for. A session started from a specialist resumes on THAT version's
    model, never another: an explicit request that resolves elsewhere is `Refused`, and none asks
    for the pinned one (so the resume re-applies it, or keeps what the agent started with). Any
    other session's request passes through untouched."""
    from . import model_choice

    last = store.last_reservation(session_keys)
    if last is None:
        return requested
    v = _version(last["specialist_id"], last["version"])
    pinned = v["model"]
    if requested is not None:
        try:
            asked = model_choice.select(prov, requested).model or model_grades.DEFAULT_MODEL
        except model_choice.ModelRefused:
            asked = None
        if asked != pinned:
            raise Refused(f"this session runs as {v['title']}, on the model {pinned}")
    return pinned


def for_relaunch(session_keys: list[str], *, launch_key: str) -> Claim | None:
    """For a relaunch of an EXISTING session: None if it was never started from a specialist,
    else a slot for the same version under ``launch_key`` (the key its master runs under)."""
    last = store.last_reservation(session_keys)
    if last is None:
        return None
    v = _version(last["specialist_id"], last["version"])
    return _take(last["specialist_id"], last["version"], launch_key, v)


def _take(sid: str, n: int, session_key: str, v: dict) -> Claim:
    try:
        r = store.reserve(sid, n, session_key, max_instances=specialist_capacity.max_instances())
    except store.CapacityRefused as exc:
        raise Refused(str(exc)) from None
    except store.StoreError as exc:
        raise Refused(str(exc)) from None
    owner = r.get("owner") if r["created"] else None
    claim = Claim(r["reservation_id"], r["session_key"], sid, n, owner, None)
    try:
        bundle = _bundle(claim.reservation_id, v["skills"])
    except BaseException:
        abandon(claim)
        raise
    return Claim(claim.reservation_id, claim.session_key, sid, n, owner, bundle)


def _bundle(rid: str, skills: list[dict]) -> str | None:
    if not skills:
        return None
    try:
        return str(skill_bundles.build(rid, skills))
    except skill_bundles.BundleRefused as exc:
        # A re-take finds the bundle its slot already has (the same version, the same pins; a
        # build never replaces one): it reuses that one — but only while the library still holds
        # every pinned revision, so it is refused exactly when a fresh bundle would be.
        try:
            path = skill_bundles.verify(str(skill_bundles.path_of(rid)))
        except skill_bundles.BundleRefused:
            raise Refused(str(exc)) from None
        try:
            skill_bundles.prove_pins(skills)
        except skill_bundles.BundleRefused as changed:
            raise Refused(str(changed)) from None
        return path


def abandon(claim: Claim | None) -> None:
    """Nothing was launched for ``claim``: release the slot this attempt created, with its bundle.
    A re-take holds no token and frees nothing (reconcile settles it on proof)."""
    if claim is None or claim.owner is None:
        return
    try:
        specialist_capacity.release(claim.reservation_id, owner=claim.owner)
    except Exception:  # noqa: BLE001 — reconcile releases it once its session is proven dead
        log.warning("specialist slot %s not released", claim.reservation_id, exc_info=True)


async def settle(claim: Claim | None) -> None:
    """After a launch's connection ends: dead → released, live → ``live``, unknown → left for
    reconcile. Asked exactly the way reconcile asks (`specialist_capacity.liveness`)."""
    if claim is None or claim.owner is None:
        return
    try:
        verdict, _why = await specialist_capacity.liveness(claim.session_key)
        if verdict == specialist_capacity.DEAD:
            await asyncio.to_thread(abandon, claim)
        elif verdict == specialist_capacity.LIVE:
            await asyncio.to_thread(store.activate, claim.reservation_id, owner=claim.owner)
    except Exception:  # noqa: BLE001 — reconcile settles it
        log.warning("specialist slot %s not settled", claim.reservation_id, exc_info=True)


async def activate_once_live(claim: Claim, is_live) -> None:
    """``starting`` → ``live`` once ``is_live()`` (a blocking probe) says the launched agent runs;
    past `LIVE_WAIT_S` the slot is left to `settle` / reconcile, never released here."""
    if claim.owner is None:
        return
    deadline = time.monotonic() + LIVE_WAIT_S
    while time.monotonic() < deadline:
        with contextlib.suppress(Exception):
            if await asyncio.to_thread(is_live):
                await asyncio.to_thread(store.activate, claim.reservation_id, owner=claim.owner)
                return
        await asyncio.sleep(_LIVE_POLL_S)
