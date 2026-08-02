"""Agent-side reverse proxy: bridge each mux stream to the box's local app (#579 P2).

Decision 3a: the browser runs the real BattleLab SPA (served from our origin) and
routes only its private dynamic traffic — `/api` requests and the terminal `/ws` —
over the blind relay. On the agent side, each such request/socket is one mux stream
(P1); this module proxies it to the box's **own** local app.

Security — the target is FIXED (`127.0.0.1:<app-port>` from the agent's config),
never taken from the browser's request. The per-stream open-info carries only a
method/path/headers, and the path is validated to be a plain absolute path, so this
can never become a generic encrypted tunnel to arbitrary loopback services.

Auth — `AUTH_MODE=none` still mints a session cookie and enforces CSRF + Origin on
state-changing routes, so we pass the browser's `Cookie` + `X-CSRF-Token` through
unchanged and rewrite `Origin`/`Referer` to the app's own origin for the loopback hop.

Per-stream sub-protocol (over one mux stream):
  HTTP  open-info ``{"k":"http","method","path","headers"}``; the browser's DATA is
        the request body, its END closes it. The agent replies with
        ``u32 meta_len | meta_json | body…`` then END, where
        ``meta_json = {"status": int, "headers": [[k, v], …]}``.
  WS    open-info ``{"k":"ws","path","headers"}`` (headers carry Cookie/CSRF for the
        upgrade); each message is length-framed as
        ``type(u8) · len(u32) · payload`` (type 0 = text, 1 = binary), in both
        directions. The mux stream is a *byte* stream — DATA frames coalesce and
        fragment — so messages MUST be length-delimited, not read-boundary-delimited.
        On teardown the agent may send one **close frame** (type 2, agent→browser only:
        ``u16 code + utf8 reason``) carrying the app WebSocket's deliberate close code so
        the browser adapter can surface ``/ws/term`` rejects (4401/4403/4404/4500) instead
        of a generic close; a browser-initiated close just gets EOF (clean 1000).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import struct

import httpx
import websockets

from .mux import Stream, StreamReset

# Hop-by-hop headers must not be forwarded across a proxy (RFC 7230 §6.1).
_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "transfer-encoding",
        "upgrade",
        "proxy-authorization",
        "te",
        "trailer",
        "host",
    }
)


# WS message-frame types (the `type` byte of `type·len·payload`). 0/1 are bidirectional
# data; 2 is an agent→browser CLOSE carrying the app WS close code (u16 BE + utf8 reason).
_WS_CLOSE = 2


def _safe_path(p: object) -> bool:
    """A plain absolute request path — not a full URL, not a scheme, not a host."""
    return isinstance(p, str) and p.startswith("/") and not p.startswith("//") and "://" not in p


async def _read_exact(stream: Stream, n: int) -> bytes | None:
    """Read exactly ``n`` bytes from the mux byte stream, or None at EOF before ``n``."""
    buf = bytearray()
    while len(buf) < n:
        part = await stream.read(n - len(buf))
        if not part:
            return None
        buf += part
    return bytes(buf)


class AppProxyTarget:
    """Reverse-proxies mux streams to one fixed local app."""

    def __init__(
        self,
        *,
        app_host: str = "127.0.0.1",
        app_port: int,
        app_origin: str | None = None,
        client: httpx.AsyncClient | None = None,
        ws_connect=websockets.connect,
    ) -> None:
        self.http_base = f"http://{app_host}:{app_port}"
        self.ws_base = f"ws://{app_host}:{app_port}"
        # the origin the app expects on state-changing requests (its own origin)
        self.origin = app_origin or self.http_base
        self._client = client
        self._ws_connect = ws_connect

    async def serve(self, stream: Stream) -> None:
        """Handle one mux stream: parse its open-info and proxy HTTP or WS."""
        try:
            info = json.loads(bytes(stream.open_info) or b"{}")
            path = info.get("path", "")
            if not _safe_path(path):
                raise ValueError("unsafe path")
            kind = info.get("k")
            if kind == "http":
                await self._proxy_http(stream, info, path)
            elif kind == "ws":
                await self._proxy_ws(stream, info, path)
            else:
                raise ValueError("unknown stream kind")
        except (StreamReset, asyncio.CancelledError):
            raise
        except Exception:
            # Any proxy error (bad open-info, app down, disallowed path) aborts just
            # this stream — never the whole tunnel, never a crash.
            with contextlib.suppress(Exception):
                stream.reset()

    def _rewrite_headers(self, headers: object) -> dict[str, str]:
        pairs = headers.items() if isinstance(headers, dict) else (headers or [])
        out: dict[str, str] = {}
        for k, v in pairs:
            if k.lower() in _HOP:
                continue
            out[k] = v
        # Normalize Origin/Referer to the app's own origin for the loopback hop;
        # Cookie + X-CSRF-Token pass through untouched so auth still works.
        out["Origin"] = self.origin
        for key in list(out):
            if key.lower() == "referer":
                out[key] = self.origin
        return out

    async def _proxy_http(self, stream: Stream, info: dict, path: str) -> None:
        body = bytearray()
        while True:
            chunk = await stream.read()
            if not chunk:
                break
            body += chunk
        headers = self._rewrite_headers(info.get("headers", {}))
        method = info.get("method", "GET")

        async def do(client: httpx.AsyncClient) -> None:
            resp = await client.request(
                method, self.http_base + path, headers=headers, content=bytes(body)
            )
            meta = json.dumps(
                {
                    "status": resp.status_code,
                    "headers": [[k, v] for k, v in resp.headers.items() if k.lower() not in _HOP],
                }
            ).encode()
            await stream.write(struct.pack(">I", len(meta)) + meta)
            await stream.write(resp.content)
            await stream.end()

        if self._client is not None:
            await do(self._client)
        else:
            async with httpx.AsyncClient() as client:
                await do(client)

    async def _proxy_ws(self, stream: Stream, info: dict, path: str) -> None:
        # Forward the browser's auth headers (Cookie / X-CSRF-Token) on the WS upgrade —
        # the local /ws/term route still needs the minted session cookie even under
        # AUTH_MODE=none. Origin is rewritten via the `origin=` param (below).
        headers = self._rewrite_headers(info.get("headers", {}))
        origin = headers.pop("Origin", self.origin)
        headers.pop("Referer", None)  # not needed on a WS upgrade
        async with self._ws_connect(
            self.ws_base + path, origin=origin, additional_headers=headers
        ) as appws:

            async def upstream() -> None:  # browser → app
                # Each message is length-framed: type(u8) + len(u32) + payload. The mux
                # stream is a byte stream, so we read exact frame sizes — never rely on
                # read() boundaries (coalesced/fragmented DATA would corrupt messages).
                while True:
                    header = await _read_exact(stream, 5)
                    if header is None:
                        break
                    mtype = header[0]
                    (length,) = struct.unpack(">I", header[1:5])
                    payload = await _read_exact(stream, length) if length else b""
                    if payload is None:
                        break
                    await appws.send(payload if mtype == 1 else payload.decode())

            async def downstream() -> None:  # app → browser
                async for m in appws:
                    if isinstance(m, str):
                        data, mtype = m.encode(), 0
                    else:
                        data, mtype = bytes(m), 1
                    await stream.write(bytes([mtype]) + struct.pack(">I", len(data)) + data)
                await stream.end()

            up = asyncio.ensure_future(upstream())
            down = asyncio.ensure_future(downstream())
            try:
                # Whichever side ends first (browser closes → upstream EOF, or app closes
                # → downstream ends) tears down the other and closes the app socket — a
                # clean close on one leg must propagate, not leak the task/connection.
                await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                up.cancel()
                down.cancel()
                await asyncio.gather(up, down, return_exceptions=True)
                # If the APP closed with a deliberate code (/ws/term rejects with
                # 4401/4403/4404/4500), serialize it as a CLOSE frame so the browser adapter
                # surfaces the reject to TermSocket (its NO_RETRY set) instead of a generic
                # 1000/1006 that would reconnect forever. A browser-initiated close leaves
                # appws still open here (close_code is None) → no frame, just the EOF below.
                with contextlib.suppress(Exception):
                    code = appws.close_code
                    if code is not None and code != 1000:
                        reason = (appws.close_reason or "")[:123].encode("utf-8", "replace")
                        payload = struct.pack(">H", code & 0xFFFF) + reason
                        frame = bytes([_WS_CLOSE]) + struct.pack(">I", len(payload)) + payload
                        await stream.write(frame)
                # Always half-close the mux stream back to the browser so its read side
                # gets EOF — a cancelled downstream() never reaches its own end(), which
                # would leave the browser adapter's socket half-open. (On errors, serve()
                # RESETs, which supersedes.) Exiting the `async with` closes appws too.
                with contextlib.suppress(Exception):
                    await stream.end()
