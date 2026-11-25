"""The Home Free agent's part in receiving webhooks through the relay (#2221 P1b, cloud#101).

A Home Free box has no public edge, so a webhook's public address lives on the relay:
``https://<public_id>.hook.battlelabos.com/``. The relay forwards each delivery over this agent's
identity-proven control connection as one ``hook_request`` frame; this module POSTs it to the
box's own app at ``http://<app_host>:<app_port>/hooks/w/<ref>`` (loopback, no redirects) and
answers ``hook_response {hid, status}``. The app verifies the signature exactly as for a direct
delivery: the relay holds no webhook secret.

**Registration** is the app's decision (it owns the webhook list). The app asks over the local
peer socket (``peerpipe``: ``hook_sync`` with every webhook id, ``hook_unregister`` before a
delete). This module remembers the synced ids and re-registers them on every control connection
and every :data:`RENEW_S`, which is what keeps a relay id alive across reconnects and long
uptimes (the relay expires ids after 30 days without a renewal).

**Ordering:** every app request carries a generation ``gen`` the app raises for each request it
makes under its own lock; a request older than the last one applied is refused ``stale``, so a
slow, abandoned app snapshot can never undo a newer one. Sync, unregister and renewal run one at
a time under :attr:`HookLink._lock`, and an unregister drops a webhook here only once the relay
has confirmed it (a refused one keeps forwarding). ``hook_status`` reports a per-process ``boot``
id and a per-connection ``epoch``: the app compares them with its last sync and syncs again soon
after this agent restarts or reconnects (the synced set lives only in memory here).

**Bounds:** at most :data:`INFLIGHT_MAX` forwards at once (the relay limits too; a full agent
answers 429), a :data:`FORWARD_TIMEOUT_S` deadline per forward, the body capped at
:data:`BODY_MAX` decoded bytes, and only an allowlist of headers is passed on.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import itertools
import json
import logging
import re
import secrets

import httpx

log = logging.getLogger("battlelab.homefree.hooks")

REF_RE = re.compile(r"\A[a-z0-9]{1,64}\Z")
HID_RE = re.compile(r"\A[0-9a-f]{16,64}\Z")
PUBLIC_ID_RE = re.compile(r"\A[a-z2-7]{26}\Z")
#: The headers a forwarded delivery may carry (the relay sends only these; checked again here).
HEADERS = frozenset(
    {
        "content-type",
        "x-forgejo-signature",
        "x-gitea-signature",
        "x-hub-signature-256",
        "x-forgejo-event",
        "x-gitea-event",
        "x-github-event",
        "x-forgejo-delivery",
        "x-github-delivery",
    }
)
BODY_MAX = 256 * 1024
INFLIGHT_MAX = 8
FORWARD_TIMEOUT_S = 9.0
REPLY_TIMEOUT_S = 15.0
RENEW_S = 24 * 3600
#: The statuses passed back to the relay; anything else from the app is answered 404.
PASS_STATUSES = frozenset({202, 404, 413, 429})
REFS_MAX = 100
GEN_MAX = 2**63 - 1


class HookError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class HookLink:
    def __init__(
        self,
        *,
        app_host: str = "127.0.0.1",
        app_port: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._app = f"http://{app_host}:{app_port}" if app_port else None
        self._transport = transport
        self._ws = None
        self._rids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._refs: set[str] = set()
        self._known: dict[str, dict] = {}  # ref -> {public_id, url}
        self._tasks: set[asyncio.Task] = set()
        self._inflight = 0
        self._renewer: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._gen = 0  # the newest app request applied
        self._boot = secrets.token_hex(8)  # this process
        self._epoch = 0  # control connections attached so far

    # ---- the control connection -------------------------------------------------------------

    def attach(self, ws) -> None:
        """A (re)registered control connection: re-register every synced webhook on it, then
        keep renewing them while it lasts."""
        self._ws = ws
        self._epoch += 1
        self._renewer = asyncio.get_running_loop().create_task(self._renew_loop())

    def detach(self) -> None:
        self._ws = None
        if self._renewer is not None:
            self._renewer.cancel()
            self._renewer = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(HookError("no_relay"))
        self._pending.clear()

    async def _renew_loop(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            while True:
                async with self._lock:
                    for ref in sorted(self._refs):
                        try:
                            await self._register(ref)
                        except HookError as exc:
                            log.info("hook %s not registered: %s", ref, exc.code)
                            if exc.code == "no_relay":
                                break
                await asyncio.sleep(RENEW_S)

    def on_message(self, data: dict) -> bool:
        """Consume a relay message meant for webhooks. True when it was one."""
        t = data.get("t")
        if t in ("hook_ok", "hook_error"):
            fut = self._pending.pop(data.get("rid"), None)
            if fut is not None and not fut.done():
                fut.set_result(data)
            return True
        if t == "hook_request":
            task = asyncio.get_running_loop().create_task(self._forward(data))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            return True
        return False

    async def _ask(self, msg: dict) -> dict:
        ws = self._ws
        if ws is None:
            raise HookError("no_relay")
        rid = next(self._rids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await ws.send(json.dumps({**msg, "rid": rid}))
            reply = await asyncio.wait_for(fut, REPLY_TIMEOUT_S)
        except (TimeoutError, ConnectionError, OSError):
            raise HookError("no_relay") from None
        finally:
            self._pending.pop(rid, None)
        if reply.get("t") == "hook_error":
            code = reply.get("code")
            raise HookError(code if isinstance(code, str) else "refused")
        return reply

    async def _register(self, ref: str) -> dict:
        reply = await self._ask({"t": "hook_register", "ref": ref})
        public_id, url = reply.get("public_id"), reply.get("url")
        if reply.get("ref") != ref or not isinstance(public_id, str):
            raise HookError("bad_reply")
        if not PUBLIC_ID_RE.match(public_id) or not isinstance(url, str):
            raise HookError("bad_reply")
        if not url.startswith(f"https://{public_id}.") or len(url) > 200:
            raise HookError("bad_reply")
        self._known[ref] = {"public_id": public_id, "url": url}
        return self._known[ref]

    # ---- the app's requests (over the peer socket) --------------------------------------------

    def status(self) -> dict:
        return {
            "boot": self._boot,
            "epoch": self._epoch,
            "gen": self._gen,
            "attached": self._ws is not None,
        }

    def _check_gen(self, gen: object) -> int:
        if not isinstance(gen, int) or isinstance(gen, bool) or not 0 < gen <= GEN_MAX:
            raise HookError("bad_request")
        if gen <= self._gen:
            raise HookError("stale")  # an older app request than one already applied
        return gen

    async def sync(self, refs: object, gen: object) -> dict:
        """Make ``refs`` the box's relayed webhooks: register each (idempotent on the relay) and
        answer ``{"hooks": {ref: {public_id, url}}, "refused": [ref], "complete", "boot",
        "epoch"}``: ``hooks`` for those the relay confirmed, ``refused`` for those it answered
        with an error, ``complete`` only when every ref was confirmed (else the app retries). A ref
        no longer listed is forgotten here (the app unregisters it explicitly before a delete).
        ``epoch`` is the connection the registrations went out on: one that changed meanwhile
        shows the app it should sync again."""
        if not isinstance(refs, list) or len(refs) > REFS_MAX:
            raise HookError("bad_request")
        if not all(isinstance(r, str) and REF_RE.match(r) for r in refs):
            raise HookError("bad_request")
        async with self._lock:
            self._gen = self._check_gen(gen)
            self._refs = set(refs)
            for gone in set(self._known) - self._refs:
                self._known.pop(gone, None)
            if self._ws is None:
                raise HookError("no_relay")
            epoch = self._epoch
            out: dict[str, dict] = {}
            refused: list[str] = []
            for ref in sorted(self._refs):
                try:
                    out[ref] = await self._register(ref)
                except HookError as exc:
                    if exc.code == "no_relay":
                        break  # the relay doesn't answer (offline, or one without webhooks)
                    refused.append(ref)
            return {
                "hooks": out,
                "refused": refused,
                "complete": len(out) == len(self._refs),
                "boot": self._boot,
                "epoch": epoch,
            }

    async def unregister(self, ref: object, gen: object) -> None:
        """Remove ``ref`` at the relay, and only then here: a refused or unanswered removal keeps
        the webhook forwarding (the app keeps it too)."""
        if not isinstance(ref, str) or not REF_RE.match(ref):
            raise HookError("bad_request")
        async with self._lock:
            gen = self._check_gen(gen)
            await self._ask({"t": "hook_unregister", "ref": ref})
            self._gen = gen
            self._refs.discard(ref)
            self._known.pop(ref, None)

    # ---- a delivery forwarded by the relay ------------------------------------------------------

    async def _respond(self, hid: str, status: int) -> None:
        ws = self._ws
        if ws is not None:
            with contextlib.suppress(ConnectionError, OSError):
                await ws.send(json.dumps({"t": "hook_response", "hid": hid, "status": status}))

    async def _forward(self, data: dict) -> None:
        hid, ref = data.get("hid"), data.get("ref")
        if not isinstance(hid, str) or not HID_RE.match(hid):
            return  # nothing to answer to
        if not isinstance(ref, str) or ref not in self._refs or self._app is None:
            await self._respond(hid, 404)
            return
        if self._inflight >= INFLIGHT_MAX:
            await self._respond(hid, 429)
            return
        raw_headers = data.get("headers")
        body_b64 = data.get("body_b64")
        try:
            if not isinstance(raw_headers, dict) or not isinstance(body_b64, str):
                raise ValueError
            body = base64.b64decode(body_b64, validate=True)
        except (ValueError, binascii.Error):
            await self._respond(hid, 404)
            return
        if len(body) > BODY_MAX:
            await self._respond(hid, 413)
            return
        headers = {
            k: v
            for k, v in raw_headers.items()
            if isinstance(k, str) and k.lower() in HEADERS and isinstance(v, str) and len(v) < 512
        }
        self._inflight += 1
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=FORWARD_TIMEOUT_S,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                r = await client.post(f"{self._app}/hooks/w/{ref}", content=body, headers=headers)
            status = r.status_code if r.status_code in PASS_STATUSES else 404
        except httpx.HTTPError:
            status = 404
        finally:
            self._inflight -= 1
        await self._respond(hid, status)
