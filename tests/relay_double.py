"""A small stand-in for the Home Free relay (battlelab-cloud), for the federation relay tests.

It speaks the relay's wire for the roles the tests need: ``agent`` (register with a signed
nonce), ``peer`` (the domain-separated signature of battlelab-cloud#94, a live registered caller),
``agent-session`` (attach by sid and token) and the blind binary bridge. It verifies signatures
like the real relay, and it RECORDS every byte it forwards so a test can assert the relay never
saw plaintext. ``peer_role=False`` models a relay that predates the role.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
from urllib.parse import parse_qs, urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from websockets.asyncio.server import serve

from agent_sessions.homefree.peerpipe import peer_message


def _verify(pub: bytes, message: bytes, sig: bytes) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(pub).verify(sig, message)
        return True
    except (InvalidSignature, ValueError):
        return False


class RelayDouble:
    def __init__(self, *, peer_role: bool = True) -> None:
        self.peer_role = peer_role
        self.agents: dict[str, tuple[bytes, object]] = {}  # name -> (idpub, control ws)
        self.pending: dict[str, tuple[str, asyncio.Future]] = {}
        self.forwarded: list[bytes] = []
        self.peer_calls: list[tuple[str, str]] = []
        self.flip_next = False  # corrupt one forwarded frame
        self.caller_legs: list = []  # paired callers, so a test can send relay control text
        self.url = ""
        self._server = None

    async def __aenter__(self) -> RelayDouble:
        self._server = await serve(self._handle, "127.0.0.1", 0, max_size=1 << 20)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}/relay/ws"
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, ws) -> None:
        role = parse_qs(urlsplit(ws.request.path).query).get("role", [""])[0]
        if role == "agent":
            await self._agent(ws)
        elif role == "agent-session":
            await self._session(ws)
        elif role == "peer" and self.peer_role:
            await self._peer(ws)
        else:
            await ws.send(json.dumps({"t": "error", "code": "bad_request"}))

    async def _agent(self, ws) -> None:
        nonce = secrets.token_bytes(32)
        await ws.send(json.dumps({"t": "challenge", "nonce": base64.b64encode(nonce).decode()}))
        msg = json.loads(await ws.recv())
        pub, sig = base64.b64decode(msg["idpub"]), base64.b64decode(msg["sig"])
        if not _verify(pub, nonce, sig):
            return await ws.send(json.dumps({"t": "error", "code": "bad_signature"}))
        name = msg["name"].lower()
        self.agents[name] = (pub, ws)
        await ws.send(json.dumps({"t": "registered", "name": name}))
        try:
            async for raw in ws:
                if isinstance(raw, str) and json.loads(raw).get("t") == "ping":
                    await ws.send(json.dumps({"t": "pong"}))
        finally:
            if self.agents.get(name, (None, None))[1] is ws:
                del self.agents[name]

    async def _peer(self, ws) -> None:
        nonce = secrets.token_bytes(32)
        await ws.send(json.dumps({"t": "challenge", "nonce": base64.b64encode(nonce).decode()}))
        msg = json.loads(await ws.recv())
        name, target = msg["name"], msg["target"]
        pub, sig = base64.b64decode(msg["idpub"]), base64.b64decode(msg["sig"])

        async def refuse(code: str) -> None:
            await ws.send(json.dumps({"t": "error", "code": code}))

        if not _verify(pub, peer_message(nonce, name, target), sig):
            return await refuse("bad_signature")
        registered = self.agents.get(name.lower())
        if registered is None or registered[0] != pub:
            return await refuse("not_registered")
        callee = self.agents.get(target.lower())
        if callee is None or target.lower() == name.lower():
            return await refuse("offline")
        self.peer_calls.append((name.lower(), target.lower()))
        sid, stoken = secrets.token_hex(16), secrets.token_hex(16)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[sid] = (stoken, fut)
        await callee[1].send(
            json.dumps(
                {"t": "peer_start", "sid": sid, "stoken": stoken, "deadline": 0, "from": name}
            )
        )
        try:
            leg, done = await asyncio.wait_for(fut, 2.0)
        except TimeoutError:
            self.pending.pop(sid, None)
            return await refuse("agent_unreachable")
        await ws.send(json.dumps({"t": "paired", "sid": sid, "deadline": 0, "ttl": 3600}))
        self.caller_legs.append(ws)
        try:
            await self._bridge(ws, leg)
        finally:
            done.set()

    async def _session(self, ws) -> None:
        attach = json.loads(await ws.recv())
        entry = self.pending.pop(attach.get("sid"), None)
        if entry is None or entry[0] != attach.get("stoken"):
            return
        done = asyncio.Event()
        entry[1].set_result((ws, done))
        await done.wait()

    async def _bridge(self, a, b) -> None:
        async def pump(src, dst) -> None:
            async for message in src:
                if isinstance(message, bytes):
                    self.forwarded.append(message)
                    if self.flip_next:
                        self.flip_next = False
                        message = bytes([message[0] ^ 1]) + message[1:]
                    await dst.send(message)

        tasks = [asyncio.create_task(pump(a, b)), asyncio.create_task(pump(b, a))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await a.close()
            await b.close()
