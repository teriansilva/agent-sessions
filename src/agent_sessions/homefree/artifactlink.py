"""The Home Free agent's part in hosting a shared artifact on the relay (#2190, battlelab-cloud#90).

The relay key and the identity-proven control connection belong to this process, not to the app.
So when the operator shares an artifact on a Home Free box, the app asks this agent over its local
peer socket (``peerpipe``: a 0700 directory, a same-uid check). The agent then:

1. sends ``artifact_put`` (the revision's manifest) on its live control connection;
2. ``PUT``s each blob the relay asks for to ``https://<relay>/artifacts/upload/<sha256>``, with the
   relay's single-use token in ``X-Artifact-Upload-Token`` (never in the URL, so no access log
   holds it), reading the blob from the path the app named and checking its size and sha256
   first (the relay checks them again);
3. sends ``artifact_commit`` and answers the app ``{ok, public_id, url}``.

``artifact_delete`` revokes. Relay replies are matched to requests by ``rid``. With no live
control connection the answer is ``no_relay``; the relay's own refusals pass through by code.
Nothing here decides WHAT is shared: the app sends only an artifact the operator shared.
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import re
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

log = logging.getLogger("battlelab.homefree.artifacts")

REPLY_TIMEOUT_S = 30.0
UPLOAD_TIMEOUT_S = 120.0
SHA_RE = re.compile(r"\A[0-9a-f]{64}\Z")
#: The relay's public id for a hosted copy: 26 lowercase base32 characters.
RELAY_ID_RE = re.compile(r"\A[a-z2-7]{26}\Z")
#: The box's stable reference for a copy (the relay keys idempotent creation on it).
REF_RE = re.compile(r"\A[0-9a-f]{16,64}\Z")
_REPLIES = ("artifact_upload", "artifact_ok", "artifact_error")


class LinkError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def upload_base(relay_url: str) -> str:
    """``wss://host/relay/ws`` → ``https://host`` (``ws://`` → ``http://`` for local relays)."""
    parts = urlsplit(relay_url)
    scheme = {"wss": "https", "ws": "http"}.get(parts.scheme)
    if scheme is None:
        raise LinkError("no_relay")
    return urlunsplit((scheme, parts.netloc, "", "", ""))


class ArtifactLink:
    def __init__(self, relay_url: str, *, transport: httpx.AsyncBaseTransport | None = None):
        self._relay_url = relay_url
        self._ws = None
        self._pending: dict[int, asyncio.Future] = {}
        self._rids = itertools.count(1)
        self._transport = transport  # tests inject one; production uses the network

    # ── the control connection ────────────────────────────────────────
    def attach(self, ws) -> None:
        """The control connection is registered: requests may go out on it."""
        self._ws = ws

    def detach(self) -> None:
        """The control connection is gone: every request waiting on it fails now."""
        self._ws = None
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(LinkError("no_relay"))
        self._pending.clear()

    def on_message(self, data: dict) -> bool:
        """A control message; True when it was a reply to one of our requests."""
        if data.get("t") not in _REPLIES:
            return False
        fut = self._pending.pop(data.get("rid"), None)
        if fut is not None and not fut.done():
            fut.set_result(data)
        return True

    async def _ask(self, msg: dict) -> dict:
        if self._ws is None:
            raise LinkError("no_relay")
        rid = next(self._rids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._ws.send(json.dumps({**msg, "rid": rid}))
            reply = await asyncio.wait_for(fut, REPLY_TIMEOUT_S)
        except (TimeoutError, OSError) as exc:
            raise LinkError("no_relay") from exc
        finally:
            self._pending.pop(rid, None)
        if reply.get("t") == "artifact_error":
            code = reply.get("code")
            raise LinkError(code if isinstance(code, str) and code.isidentifier() else "refused")
        return reply

    # ── the two operations ────────────────────────────────────────────
    async def put(
        self, public_id: object, kind: object, manifest: object, blobs: object, ref: object = None
    ) -> dict:
        if public_id is not None and not (
            isinstance(public_id, str) and RELAY_ID_RE.fullmatch(public_id)
        ):
            raise LinkError("bad_request")
        if not isinstance(manifest, dict) or not isinstance(blobs, dict):
            raise LinkError("bad_request")
        if ref is not None and not (isinstance(ref, str) and REF_RE.fullmatch(ref)):
            raise LinkError("bad_request")
        sizes = {}
        for entry in manifest.values():
            sha = entry.get("sha256") if isinstance(entry, dict) else None
            if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
                raise LinkError("bad_request")
            sizes[sha] = entry.get("size")
        if set(blobs) != set(sizes) or not all(isinstance(p, str) for p in blobs.values()):
            raise LinkError("bad_request")
        up = await self._ask(
            {
                "t": "artifact_put",
                "public_id": public_id,
                "ref": ref,
                "kind": kind,
                "manifest": manifest,
            }
        )
        token, need, put = up.get("token"), up.get("need"), up.get("put")
        if not (isinstance(token, str) and isinstance(need, list) and isinstance(put, str)):
            raise LinkError("refused")
        if put != "/artifacts/upload/":
            raise LinkError("refused")  # exactly the relay's own upload path: no token in a URL
        base = upload_base(self._relay_url)
        async with httpx.AsyncClient(
            timeout=UPLOAD_TIMEOUT_S, transport=self._transport, follow_redirects=False
        ) as client:
            for sha in need:
                if sha not in blobs:
                    raise LinkError("refused")
                data = await asyncio.to_thread(Path(blobs[sha]).read_bytes)
                if len(data) != sizes[sha] or hashlib.sha256(data).hexdigest() != sha:
                    raise LinkError("blob_mismatch")  # the box's own store is not what it says
                try:
                    r = await client.put(
                        f"{base}{put}{sha}",
                        content=data,
                        headers={"X-Artifact-Upload-Token": token},
                    )
                except httpx.HTTPError as exc:
                    raise LinkError("no_relay") from exc
                if r.status_code != 204:
                    raise LinkError("upload_refused")
        ok = await self._ask({"t": "artifact_commit", "token": token})
        pid, url = ok.get("public_id"), ok.get("url")
        if not (isinstance(pid, str) and RELAY_ID_RE.fullmatch(pid) and isinstance(url, str)):
            raise LinkError("refused")
        if not url.startswith("https://") and not url.startswith("http://"):
            raise LinkError("refused")
        return {"ok": True, "public_id": pid, "url": url}

    async def delete(self, public_id: object, ref: object = None) -> dict:
        """Revoke by the relay's id, or by the box's ref when the id was never learned."""
        by_id = isinstance(public_id, str) and RELAY_ID_RE.fullmatch(public_id)
        by_ref = isinstance(ref, str) and REF_RE.fullmatch(ref)
        if public_id is not None and not by_id or ref is not None and not by_ref:
            raise LinkError("bad_request")
        if bool(by_id) == bool(by_ref):
            raise LinkError("bad_request")  # exactly one of the two
        msg = (
            {"t": "artifact_delete", "public_id": public_id}
            if by_id
            else {"t": "artifact_delete", "ref": ref}
        )
        await self._ask(msg)
        return {"ok": True}
