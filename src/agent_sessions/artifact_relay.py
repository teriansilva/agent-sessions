"""A shared artifact hosted on the BattleLab relay, for a Home Free box (#2190, battlelab-cloud#90).

A Home Free box runs in ``AUTH_MODE=none``: its own public artifact routes are off, so a public
link cannot point at the box. When the operator shares an artifact there, the box instead uploads
that revision to the relay, which serves it at ``https://<id>.artifacts.battlelabos.com/``.

The relay connection belongs to the Home Free agent process, so this module asks the agent over
its local peer socket (``homefree/peerpipe.py``: a 0700 directory, a same-uid check):

- :func:`share` uploads the latest revision and records the relay id and URL on the artifact;
- :func:`republish` replaces a shared artifact's relay copy with its new revision (same link);
- :func:`revoke` deletes the relay copy, and clears the record only once the relay confirmed it.

**Only a shared artifact ever goes up**: these are called from the operator's share and revoke,
the agent's ``artifact_share_public``, and a republish of an artifact that already has a relay
copy. Nothing else in the app reaches the agent socket with an artifact.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging

from . import artifact_store

# `federation.service` and `homefree.peerpipe` are imported where they are used: this module is
# imported by `routes.system` (the config's `artifact_relay` flag), which loads during the
# runtime's own import, and federation imports the runtime back (an import cycle otherwise).

log = logging.getLogger("agent_sessions.artifact_relay")

#: The agent uploads every blob before it answers; a 25 MB site over a home uplink takes a while.
PUT_TIMEOUT_S = 300.0
DELETE_TIMEOUT_S = 45.0

#: Why a relay copy could not be made or removed, in the operator's words.
REASONS = {
    # No Home Free agent on this box at all: the plain `none`-mode answer.
    "no_homefree": artifact_store.PUBLIC_OFF_REASON,
    "no_relay": "This box is not connected to the BattleLab relay right now.",
    "artifacts_unavailable": "The BattleLab relay does not host public artifacts yet.",
    "too_large": "This artifact is larger than the relay hosts (25 MB, 500 files).",
    "quota_exceeded": "This box has used its relay hosting space (100 MB).",
    "rate_limited": "Too many uploads to the relay just now. Try again in a minute.",
}
GENERIC = "The BattleLab relay did not accept this artifact."


def _socket_path():
    from .federation.service import relay_socket_path

    return relay_socket_path()


def available() -> bool:
    """Whether a public link here would be a relay copy (#2190): no login on this box, and a
    Home Free agent set up to ask. One stat; the share itself still reports what the relay says."""
    return not artifact_store.PUBLIC_AVAILABLE and _socket_path().exists()


class RelayRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code

    @property
    def reason(self) -> str:
        return REASONS.get(self.code, GENERIC)


async def _ask(request: dict, timeout: float) -> dict:
    """One request to the local Home Free agent; its JSON answer. RelayRefused on any failure."""
    from .homefree import peerpipe

    path = _socket_path()
    if not path.exists():
        raise RelayRefused("no_homefree")  # Home Free is not set up on this box
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)), timeout=5.0
        )
    except (FileNotFoundError, NotADirectoryError):
        raise RelayRefused("no_homefree") from None  # Home Free is not set up on this box
    except (OSError, TimeoutError):
        raise RelayRefused("no_relay") from None
    try:
        async with asyncio.timeout(timeout):
            try:
                peerpipe.write_frame(writer, json.dumps(request, separators=(",", ":")).encode())
                await writer.drain()
                raw = await peerpipe.read_frame(reader, peerpipe.REQUEST_MAX)
            finally:
                writer.close()
    except (OSError, TimeoutError, ValueError):
        raise RelayRefused("no_relay") from None
    try:
        answer = json.loads(raw) if raw else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        raise RelayRefused("no_relay")
    if answer.get("ok") is not True:
        code = answer.get("code")
        raise RelayRefused(code if isinstance(code, str) else "refused")
    return answer


#: One relay operation per artifact at a time, in this process (the app and the agent tool's broker
#: both live here): share, refresh, revoke and delete never interleave. Across a crash or a second
#: process the relay's idempotent creation per (owner, ref) is what keeps one copy per artifact.
_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(aid: str) -> asyncio.Lock:
    return _LOCKS.setdefault(aid, asyncio.Lock())


def ref_of(aid: str) -> str:
    """The stable reference the relay keys a copy's creation on: a hash of the box's artifact id
    (the id itself never leaves the box)."""
    return hashlib.sha256(f"battlelab-artifact:{aid}".encode()).hexdigest()[:32]


#: Refusals that prove the relay made no copy: the request was refused or never sent. Anything
#: else (a lost reply above all) leaves a first upload's outcome UNKNOWN, and the intent stays.
DEFINITE = frozenset(
    {
        "no_homefree",
        "artifacts_unavailable",
        "too_large",
        "quota_exceeded",
        "rate_limited",
        "banned",
        "bad_manifest",
        "bad_request",
        "blob_mismatch",
        "not_found",
    }
)


async def _upload(aid: str, parts: dict, public_id: str | None) -> dict:
    """Upload the latest revision under ``public_id`` (or the copy the relay holds for this
    artifact's ref) and record it.

    A FIRST upload writes its intent (`relay_pending`) before anything leaves the box, and that
    intent is cleared only by a definite outcome: recorded success, or a refusal that proves no
    copy was made. A lost reply leaves it set, so revoke and delete still reach the copy (by ref)
    and the artifact is never shown as private while a public copy may exist."""
    from .homefree import peerpipe

    request = {
        "op": "artifact_put",
        "public_id": public_id,
        "ref": ref_of(aid),
        "kind": parts["kind"],
        "manifest": parts["manifest"],
        "blobs": parts["blobs"],
    }
    if len(json.dumps(request)) > peerpipe.ARTIFACT_REQUEST_MAX:
        raise RelayRefused("too_large")
    first = public_id is None
    # An intent an EARLIER attempt left (its reply was lost) is not this attempt's to clear: a
    # refusal now proves only that THIS attempt made nothing, not that the earlier copy is gone.
    inherited = bool(parts.get("relay_pending"))
    if first:
        if not _socket_path().exists():
            raise RelayRefused("no_homefree")  # nothing can leave the box: no intent to record
        await asyncio.to_thread(artifact_store.mark_relay_pending, aid, True)
    try:
        answer = await _ask(request, PUT_TIMEOUT_S)
    except RelayRefused as e:
        if first and e.code in DEFINITE and not inherited:
            await asyncio.to_thread(artifact_store.mark_relay_pending, aid, False)
        raise
    try:
        return await asyncio.to_thread(
            artifact_store.set_relay, aid, answer["public_id"], answer["url"], parts["latest"]
        )
    except (artifact_store.ArtifactRefused, artifact_store.ArtifactsUnavailable):
        if first:
            # A first copy the box could not record: take it down again. If that fails too, the
            # intent written above stays, and the next revoke or delete removes it by ref.
            try:
                await _ask(
                    {"op": "artifact_delete", "public_id": answer["public_id"], "ref": None},
                    DELETE_TIMEOUT_S,
                )
            except RelayRefused as e:
                if e.code != "not_found":
                    raise
            with contextlib.suppress(
                artifact_store.ArtifactRefused, artifact_store.ArtifactsUnavailable
            ):
                await asyncio.to_thread(artifact_store.mark_relay_pending, aid, False)
        # A replacement keeps its recorded id and link; `relay_rev` stays behind for a retry.
        raise


async def share(aid: str) -> dict:
    """Host the artifact's latest revision on the relay; the artifact as the API sees it. A copy
    that already holds the latest revision is the same answer twice; one left behind by a refresh
    that failed is brought up to date under the same link (the retry), and an uncertain first
    upload is settled (the relay's creation is idempotent per ref)."""
    async with _lock(aid):
        parts = await asyncio.to_thread(artifact_store.relay_parts, aid)
        if parts["relay_public_id"] and parts["relay_rev"] == parts["latest"]:
            return await asyncio.to_thread(artifact_store.get, aid) or {}
        return await _upload(aid, parts, parts["relay_public_id"])


async def republish(aid: str) -> None:
    """A new revision of a relay-shared artifact: replace the relay copy under the same link.
    Best effort: on failure the previous revision stays served, `relay_rev` stays behind, and the
    operator's next share brings the copy up to date."""
    try:
        async with _lock(aid):
            parts = await asyncio.to_thread(artifact_store.relay_parts, aid)
            if parts["relay_public_id"] and parts["relay_rev"] != parts["latest"]:
                await _upload(aid, parts, parts["relay_public_id"])
    except (RelayRefused, artifact_store.ArtifactRefused, artifact_store.ArtifactsUnavailable) as e:
        log.warning("artifact relay copy not refreshed (%s)", getattr(e, "code", type(e).__name__))


async def _revoke_locked(aid: str) -> dict | None:
    parts = await asyncio.to_thread(artifact_store.relay_parts, aid)
    pid = parts["relay_public_id"]
    if not pid and not parts["relay_pending"]:
        return None
    # By id when the box knows it, else by ref: an unacknowledged first upload has no id here.
    request = {"op": "artifact_delete", "public_id": pid, "ref": None if pid else ref_of(aid)}
    try:
        await _ask(request, DELETE_TIMEOUT_S)
    except RelayRefused as e:
        if e.code != "not_found":
            raise  # not confirmed absent: the record (and the intent) stay
    return await asyncio.to_thread(artifact_store.set_relay, aid, None, None)


async def revoke(aid: str) -> dict | None:
    """Delete the relay copy; None when the artifact has none. The record is cleared only once the
    relay confirmed it (an id it no longer knows counts: the copy is gone either way)."""
    async with _lock(aid):
        return await _revoke_locked(aid)


async def delete_artifact(aid: str) -> bool:
    """Delete the artifact: its relay copy first (kept, with the reason, when the relay can't
    confirm), then the artifact itself, under the same lock a share holds."""
    async with _lock(aid):
        try:
            await _revoke_locked(aid)
        except artifact_store.ArtifactRefused:
            pass  # unknown: the delete below answers it
        return await asyncio.to_thread(artifact_store.delete, aid)
