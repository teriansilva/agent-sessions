"""A blind relay, for tests (#579, #806, #807).

The production relay is a separate service, so an app-mode end-to-end test needs a stand-in. This
is a faithful one **because the real relay is blind**: its entire job is to authenticate an agent
by signature, pair a viewer with it, and pipe ciphertext between the two legs. It never sees
plaintext, never inspects a frame, and has no application knowledge — so a test relay that pipes
bytes is not a simplification of the interesting part, it *is* the part.

What it deliberately keeps faithful, because the code under test depends on each:

* the **control leg** (``?role=agent``) challenge/register/registered exchange, including the
  Ed25519 signature over the relay's nonce — a broken signature must still fail here;
* the **session leg** (``?role=agent-session``) ``attach`` with a session token, so a viewer can
  only ever be joined to the agent the relay paired it with;
* the **viewer leg** (``?role=viewer``) ``hello``/``paired`` exchange the browser's own
  ``runAppSession`` drives;
* the ALTCHA challenge the connect page solves before it will open a socket at all.

What it does not do: rate limiting, name registries, TLS, or the public deployment's session
accounting. None of those are on the path this test exercises.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import secrets
import sys
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from websockets.asyncio.server import Response, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed

#: How long a viewer waits for the agent's session leg to attach before giving up.
ATTACH_TIMEOUT_S = 10.0
#: Announced session lifetime. The connect page renders a countdown from it.
SESSION_TTL_S = 4 * 60 * 60


class Relay:
    def __init__(self) -> None:
        #: name -> the agent's live control socket
        self.agents: dict[str, object] = {}
        #: sid -> (stoken, future that the agent-session leg resolves with its socket)
        self.pending: dict[str, tuple[str, asyncio.Future]] = {}

    # -- HTTP ------------------------------------------------------------

    def altcha(self) -> Response:
        """A real (trivially small) ALTCHA challenge.

        The connect page will not open a socket without solving one, so the endpoint has to exist
        and the answer has to actually verify. The difficulty is 1 because proof-of-work is a
        rate-limiting device for the public relay, not a property this test is about.
        """
        salt = secrets.token_hex(8)
        number = 1
        challenge = hashlib.sha256(f"{salt}{number}".encode()).hexdigest()
        body = json.dumps(
            {
                "algorithm": "SHA-256",
                "challenge": challenge,
                "salt": salt,
                "signature": "test",
                "maxnumber": 10,
            }
        ).encode()
        return Response(
            200,
            "OK",
            Headers(
                {
                    "Content-Type": "application/json",
                    # The connect page is served from the preview origin, so this fetch is
                    # cross-origin exactly as it is in production.
                    "Access-Control-Allow-Origin": "*",
                    "Content-Length": str(len(body)),
                }
            ),
            body,
        )

    def process_request(self, connection, request):  # noqa: ARG002 - websockets hook signature
        path = request.path.split("?", 1)[0]
        if path == "/altcha/challenge":
            return self.altcha()
        if path != "/relay/ws":
            return Response(404, "Not Found", Headers({"Content-Length": "0"}), b"")
        return None  # let the WebSocket handshake proceed

    # -- WebSocket -------------------------------------------------------

    async def handler(self, ws) -> None:
        query = ws.request.path.partition("?")[2]
        params = dict((kv.split("=", 1) + [""])[:2] for kv in query.split("&") if kv)
        role = params.get("role")
        if role == "agent":
            await self.serve_agent(ws)
        elif role == "agent-session":
            await self.serve_agent_session(ws)
        elif role == "viewer":
            await self.serve_viewer(ws, params.get("name", ""))
        else:
            await ws.close(code=4000, reason="unknown role")

    async def serve_agent(self, ws) -> None:
        nonce = secrets.token_bytes(32)
        await ws.send(json.dumps({"t": "challenge", "nonce": base64.b64encode(nonce).decode()}))
        reg = json.loads(await ws.recv())
        if reg.get("t") != "register":
            await ws.close(code=4001, reason="expected register")
            return
        # The signature is verified for real: this is the one security property the relay itself
        # is responsible for, and a test relay that skipped it would let a broken agent pass.
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(reg["idpub"])).verify(
                base64.b64decode(reg["sig"]), nonce
            )
        except (InvalidSignature, KeyError, ValueError):
            await ws.close(code=4003, reason="bad signature")
            return
        name = reg.get("name", "")
        self.agents[name] = ws
        await ws.send(json.dumps({"t": "registered"}))
        try:
            async for _ in ws:  # held open; session_start is pushed from serve_viewer
                pass
        finally:
            if self.agents.get(name) is ws:
                del self.agents[name]

    async def serve_agent_session(self, ws) -> None:
        msg = json.loads(await ws.recv())
        sid = msg.get("sid")
        entry = self.pending.get(sid)
        if msg.get("t") != "attach" or entry is None or entry[0] != msg.get("stoken"):
            await ws.close(code=4004, reason="bad attach")
            return
        entry[1].set_result(ws)
        # Held open by `pump` on the viewer side; returning here would close the leg.
        await ws.wait_closed()

    async def serve_viewer(self, ws, name: str) -> None:
        hello = json.loads(await ws.recv())
        if hello.get("t") != "hello" or not hello.get("captcha"):
            await ws.send(json.dumps({"t": "error", "code": "captcha"}))
            return
        agent = self.agents.get(name)
        if agent is None:
            await ws.send(json.dumps({"t": "error", "code": "offline"}))
            return
        sid = secrets.token_hex(8)
        stoken = secrets.token_hex(16)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[sid] = (stoken, fut)
        await agent.send(json.dumps({"t": "session_start", "sid": sid, "stoken": stoken}))
        try:
            leg = await asyncio.wait_for(fut, ATTACH_TIMEOUT_S)
        except TimeoutError:
            await ws.send(json.dumps({"t": "error", "code": "attach_timeout"}))
            return
        finally:
            self.pending.pop(sid, None)
        # `paired` is what releases the browser's Initiator handshake, so it must arrive only
        # once the agent's leg is actually attached — otherwise msg1 races into a void.
        #
        # `deadline` is an ABSOLUTE epoch-seconds value, not a duration: the connect page computes
        # `deadline - now` for its countdown and `deadline * 1000` for the credential window. A
        # relative value here reads as 1970, so the page tore the session down the moment it
        # mounted — which is exactly how this was found.
        deadline = int(time.time()) + SESSION_TTL_S
        await ws.send(json.dumps({"t": "paired", "deadline": deadline, "ttl": SESSION_TTL_S}))
        await self.pipe(ws, leg)

    @staticmethod
    async def pipe(viewer, agent) -> None:
        """Ciphertext both ways, until either side goes. The relay reads nothing it forwards."""

        async def pump(src, dst):
            try:
                async for frame in src:
                    await dst.send(frame)
            except ConnectionClosed:
                pass
            finally:
                with contextlib.suppress(Exception):
                    await dst.close()

        await asyncio.gather(pump(viewer, agent), pump(agent, viewer))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()
    relay = Relay()
    async with serve(
        relay.handler, "127.0.0.1", args.port, process_request=relay.process_request
    ) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        # The only line on stdout, so the harness can read the chosen port and know we are up.
        print(f"RELAY_PORT={port}", flush=True)
        await asyncio.get_running_loop().create_future()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
