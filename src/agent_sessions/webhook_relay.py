"""Webhooks through the BattleLab relay for a Home Free box (#2221 P1b, battlelab-cloud#101).

A Home Free box has no public edge. Its webhooks get a public address on the relay
(``https://<public_id>.hook.battlelabos.com/``); the relay forwards each delivery live over the
Home Free agent's identity-proven connection, and the agent POSTs it to this app's
``/hooks/w/{id}``, where the signature is checked as for any delivery. The relay holds no webhook
secret, but it does see delivery bodies in transit (TLS ends there) — the webhook page says so.

The app owns the list, so it tells the agent which webhooks to relay (``hook_sync``: every
pickable webhook, on startup, after a create, and every :data:`SYNC_S`) and removes one at the
relay BEFORE deleting it (``hook_unregister``; a relay that can't confirm keeps the webhook). The
agent re-registers its synced set on every reconnect, so relay ids stay stable.

**Ordering.** Every request goes out under one lock (:func:`_guard`), with the snapshot taken
inside it, and carries a generation that only rises; the agent refuses an older one, so a slow
request abandoned by its caller can't undo a newer one. A delete holds the lock from the relay
removal to the store delete; when the store refuses (an automation picked the webhook
meanwhile), it registers the webhook again before answering.

**Catching up.** A sync is *owed* until one completes with every webhook confirmed. An owed sync
is retried with a backoff from :data:`RETRY_MIN_S` to :data:`RETRY_MAX_S`. Every :data:`POLL_S`
the app also asks the agent's ``hook_status``, and an agent restart or reconnect (its
``boot``/``epoch`` differ from the last complete sync) makes one owed at once. The agent keeps the
synced set only in memory, so this is what restores it after an agent restart, and what records
an address confirmed once the relay came back. An unregister owes one BEFORE it is sent, so a
reply lost after the agent acted still gets the webhook registered again.

**Revisions.** Every change to the relayed set (a create, a delete) raises :data:`_want` through
:func:`mark_dirty`. A sync reads the revision before its snapshot and settles the debt only if no
change came after it, so a sync that was already running when a webhook was created can never
mark the new one as synced.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time

from . import automations_store as store
from . import webhooks

log = logging.getLogger("agent_sessions.webhook_relay")

SYNC_S = 24 * 3600
POLL_S = 60.0
RETRY_MIN_S = 60.0
RETRY_MAX_S = 3600.0
STARTUP_DELAY_S = 5.0
ANSWER_MAX = 32 * 1024


class RelayError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _socket_path():
    from .federation.service import relay_socket_path

    return relay_socket_path()


def available() -> bool:
    """Whether a Home Free agent is set up on this box to relay webhooks. One stat."""
    try:
        return _socket_path().exists()
    except Exception:  # noqa: BLE001 — no federation path: no relay
        return False


async def _ask(request: dict, timeout: float = 60.0) -> dict:
    from .homefree import peerpipe

    path = _socket_path()
    if not path.exists():
        raise RelayError("no_homefree")
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(path)), timeout=5.0
        )
    except (OSError, TimeoutError):
        raise RelayError("no_relay") from None
    try:
        async with asyncio.timeout(timeout):
            try:
                peerpipe.write_frame(writer, json.dumps(request, separators=(",", ":")).encode())
                await writer.drain()
                raw = await peerpipe.read_frame(reader, ANSWER_MAX)
            finally:
                writer.close()
    except (OSError, TimeoutError, ValueError):
        raise RelayError("no_relay") from None
    try:
        answer = json.loads(raw) if raw else None
    except ValueError:
        answer = None
    if not isinstance(answer, dict):
        raise RelayError("no_relay")
    if answer.get("ok") is not True:
        code = answer.get("code")
        raise RelayError(code if isinstance(code, str) else "refused")
    return answer


_lock: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None
_gen = 0
_seen: tuple | None = None  # (boot, epoch) of the agent at the last complete sync
_dirty = True  # a sync is owed: not done yet, incomplete, or a request failed since
_last_sync = 0.0
_retry_at = 0.0  # an owed sync is not retried before this
_retry_s = RETRY_MIN_S
_want = 0  # the relayed set's revision: raised by every create and delete


def _now() -> float:
    return time.monotonic()


def _owe(*, now: bool = False) -> None:
    """A sync is owed: at once (``now``), or after the current backoff, which then grows."""
    global _dirty, _retry_at, _retry_s
    _dirty = True
    if now:
        _retry_at = 0.0
    else:
        _retry_at = _now() + _retry_s
        _retry_s = min(_retry_s * 2, RETRY_MAX_S)


def _guard() -> asyncio.Lock:
    """The one lock every request to the agent is made under (one per event loop)."""
    global _lock
    loop = asyncio.get_running_loop()
    if _lock is None or _lock[0] is not loop:
        _lock = (loop, asyncio.Lock())
    return _lock[1]


def _next_gen(floor: int = 0) -> int:
    global _gen
    _gen = max(_gen + 1, time.time_ns(), floor + 1)
    return _gen


async def _ask_ordered(request: dict) -> dict:
    """``request`` with a fresh generation. Called under :func:`_guard`, so it is the newest: an
    agent that has applied a higher one (this app's clock went back) gets one past it, once."""
    try:
        return await _ask({**request, "gen": _next_gen()})
    except RelayError as e:
        if e.code != "stale":
            raise
    st = await _ask({"op": "hook_status"})
    floor = st.get("gen") if isinstance(st.get("gen"), int) else 0
    return await _ask({**request, "gen": _next_gen(floor)})


def _relayed_ids() -> list[str]:
    return [w["id"] for w in webhooks.list_all() if w["pickable"]][: webhooks.WEBHOOKS_MAX]


async def sync_all() -> dict:
    """Tell the agent the full set of webhooks to relay and record each relay address. A webhook
    the relay didn't confirm keeps no relay address (it is shown with its direct one)."""
    async with _guard():
        return await _sync_locked()


async def _sync_locked() -> dict:
    global _dirty, _seen, _last_sync, _retry_at, _retry_s
    _dirty = True  # until this one completes (a cancel leaves a sync owed)
    rev = _want  # read BEFORE the snapshot: a change after this is not covered by it
    ids = await asyncio.to_thread(_relayed_ids)
    try:
        answer = await _ask_ordered({"op": "hook_sync", "refs": ids})
    except RelayError:
        _owe()
        raise
    hooks = answer.get("hooks") if isinstance(answer.get("hooks"), dict) else {}
    refused = answer.get("refused") if isinstance(answer.get("refused"), list) else []
    for wid in ids:
        got = hooks.get(wid) if isinstance(hooks.get(wid), dict) else None
        public_id = got.get("public_id") if got else None
        url = got.get("url") if got else None
        if isinstance(public_id, str) and isinstance(url, str):
            await asyncio.to_thread(webhooks.set_relay, wid, public_id, url)
        elif wid in refused:
            await asyncio.to_thread(webhooks.set_relay, wid, None, None)
        # Not reached this time (the relay stopped answering): its address, if any, stays shown;
        # the relay keeps a public id across re-registrations, and the retry confirms it.
    _seen = (answer.get("boot"), answer.get("epoch"))
    _last_sync = _now()
    if answer.get("complete") is True and rev == _want:
        _dirty, _retry_at, _retry_s = False, 0.0, RETRY_MIN_S
    elif answer.get("complete") is True:
        _owe(now=True)  # the set changed since this snapshot: the next poll syncs the newer one
    else:
        _owe()  # some webhook is not registered: try again later, on the same connection too
    return hooks


async def delete(wid: str, delete_fn) -> None:
    """Remove ``wid`` at the relay, then run ``delete_fn`` (the store delete), under the lock. A
    relay that can't confirm raises :class:`RelayError` and nothing is deleted. A store that
    refuses (an automation picked the webhook meanwhile) re-raises after the webhook is
    registered again. A sync is owed BEFORE the unregister goes out, so every outcome that isn't
    known to be final (a lost reply after the agent acted, a cancel, a failed re-registration)
    is repaired by the next poll."""
    async with _guard():
        mark_dirty()
        try:
            await _ask_ordered({"op": "hook_unregister", "ref": wid})
        except RelayError:
            # Refused, or the reply was lost after the agent may have acted: register it again
            # now (the webhook stays), else the owed sync does.
            with contextlib.suppress(RelayError, store.StoreError):
                await _sync_locked()
            raise
        try:
            await asyncio.to_thread(delete_fn)
        except Exception:
            with contextlib.suppress(RelayError, store.StoreError):
                await _sync_locked()
            raise


def mark_dirty() -> None:
    """The relayed set changed (or may have): a new revision, and the next poll syncs."""
    global _want
    _want += 1
    _owe(now=True)


async def _due() -> bool:
    if _dirty:
        return _now() >= _retry_at
    if _now() - _last_sync >= SYNC_S:
        return True
    st = await _ask({"op": "hook_status"})
    return st.get("attached") is True and (st.get("boot"), st.get("epoch")) != _seen


async def run_loop() -> None:
    """Sync on startup, then every :data:`POLL_S` whenever one is owed (:func:`_due`). Quiet when
    Home Free isn't set up."""
    await asyncio.sleep(STARTUP_DELAY_S)
    while True:
        if available():
            with contextlib.suppress(RelayError, store.StoreError):
                if await _due():
                    await sync_all()
        await asyncio.sleep(POLL_S)
