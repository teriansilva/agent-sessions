"""Run one request through this app, in this process, as the operator who asked (#1559).

HQ's confirmed operations (``ask_operations``) execute the SAME route the UI's button calls, so
every guard is the route's own: login, CSRF and Origin, the forced password change, mission
reservations, runtime teardown, revisions. This is the one way they do it: an ASGI call to
``app`` carrying the tapping operator's ``Cookie``, ``X-CSRF-Token`` and ``Origin``, captured
from their decide request.

**Nothing leaves the process.** There is no HTTP client and no socket: the request is a scope
handed to the app's own ASGI callable, so the outbound-HTTP inventory
(``prompt-registry.md``) is unchanged.

**Only what the operator's request carried.** The forwarded headers are exactly the three the
route's guards read, taken from the operator's decide request, never built here. A missing one
is simply absent, so the route refuses as it would refuse the browser.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: The scope key every in-process request carries (#2390). An HTTP client cannot set it (the
#: server builds the scope), so a route can tell a call HQ made from the operator's own request:
#: the Authority route refuses these, so HQ can never change its own authority.
IN_PROCESS = "battlelab_in_process"
#: The scope key an HQ act's grant rides on into the route it calls (#2391): a callable returning
#: "" while the act may still run, else why not. A route whose effect lands after its own awaits
#: (the mission relay's delivery) asks it at that effect's final fence. Like `IN_PROCESS`, no HTTP
#: client can set it.
ACT_ADMIT = "battlelab_act_admit"
#: The scope key an HQ drive request's binding rides on (#2397): ``{pid, cid, generation,
#: last_turn, verb}``, what the card bound, which the structured routes admit it by
#: (`session_driver.check`). Like `IN_PROCESS`, no HTTP client can set it.
DRIVE = "battlelab_drive"
#: The scope key a call's session leg sets (#2341): the request carries the Commander's OWN words
#: spoken to an API session on a call (or their spoken Stop), not HQ's. The structured routes
#: treat it as the Commander's request: it takes a session HQ drives over, as typing does
#: (#2397). Only `voice_session` sets it (pinned by a source scan). No HTTP client can set it.
COMMANDER = "battlelab_commander"
#: The headers a confirmed operation forwards, lowercase as ASGI carries them.
FORWARDED = (b"cookie", b"x-csrf-token", b"origin")
#: The largest response body kept. Every catalog route answers a small JSON object.
RESPONSE_MAX = 1 << 20
#: The largest multipart body sent (#1786: a secret file is at most 256 KiB, plus its framing).
FORM_MAX = 256 * 1024 + 16 * 1024


@dataclass(frozen=True)
class Operator:
    """Who confirmed, as their decide request presented itself."""

    headers: tuple[tuple[bytes, bytes], ...]
    scheme: str
    server: tuple[str, int] | None
    client: tuple[str, int] | None
    root_path: str = ""

    @classmethod
    def from_scope(cls, scope: dict) -> Operator:
        kept = tuple(
            (k.lower(), v) for k, v in scope.get("headers") or [] if k.lower() in FORWARDED
        )
        host = tuple((k.lower(), v) for k, v in scope.get("headers") or [] if k.lower() == b"host")
        return cls(
            headers=kept + host,
            scheme=str(scope.get("scheme") or "http"),
            server=scope.get("server"),
            client=scope.get("client"),
            root_path=str(scope.get("root_path") or ""),
        )


def in_process(scope: dict) -> bool:
    """Whether a request was made in this process through :func:`call` (by HQ), not by a browser."""
    return scope.get(IN_PROCESS) is True


@dataclass(frozen=True)
class Response:
    status: int
    body: object  # parsed JSON, or None when the body is not JSON
    #: The route sent more than RESPONSE_MAX: the body was not kept, so it says nothing about what
    #: the route did — a 2xx with this set is "it ran, result unknown", never a failure (#1559).
    truncated: bool = False

    @property
    def detail(self) -> str:
        if isinstance(self.body, dict) and isinstance(self.body.get("detail"), str):
            return self.body["detail"]
        return ""


def _multipart(field: str, filename: str, payload: bytes) -> tuple[bytes, bytes]:
    """ONE file part, framed the way a browser sends it: ``(body, content-type)``. The file name
    is display text only (the route treats it so); quotes, backslashes and line breaks are
    dropped so it can never break the part's header."""
    import uuid

    boundary = f"battlelab-{uuid.uuid4().hex}".encode()
    safe = "".join(c for c in filename if c.isprintable() and c not in '"\\\r\n') or "file"
    head = (
        b"--" + boundary + b"\r\n"
        b'Content-Disposition: form-data; name="' + field.encode() + b'"; '
        b'filename="' + safe.encode("utf-8") + b'"\r\n'
        b"Content-Type: application/octet-stream\r\n\r\n"
    )
    body = head + payload + b"\r\n--" + boundary + b"--\r\n"
    return body, b"multipart/form-data; boundary=" + boundary


async def call(
    app,
    operator: Operator,
    method: str,
    path: str,
    body: dict | None,
    *,
    query: str = "",
    form: tuple[str, str, bytes] | None = None,
    act_admit=None,
    drive: dict | None = None,
    commander: bool = False,
) -> Response:
    """Send ``method path`` with ``body`` (JSON) through ``app`` as ``operator``. ``query`` is a
    fixed string the caller authors (a facts read), never part of a catalog path. ``form`` is
    instead ONE file part ``(field, file name, bytes)`` as a bounded multipart body (#1786: a
    secret file moved into the Library through the same upload route the page uses).
    ``act_admit``: an HQ act's grant check (#2391), carried to the route as `ACT_ADMIT`.
    ``drive``: an HQ drive request's binding (#2397), carried to the route as `DRIVE`.
    ``commander``: the Commander's own words on a call (#2341), carried as `COMMANDER`."""
    if commander and drive is not None:
        raise ValueError("a request is the Commander's or an HQ drive request, not both")
    if not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("a catalog path is an absolute path with no query")
    if form is not None and body is not None:
        raise ValueError("a call sends JSON or one file part, not both")
    headers = list(operator.headers)
    if form is not None:
        data, ctype = _multipart(*form)
        if len(data) > FORM_MAX:
            raise ValueError("that file is too large to send in process")
        headers += [(b"content-type", ctype)]
    else:
        data = b"" if body is None else json.dumps(body).encode()
        if body is not None:
            headers += [(b"content-type", b"application/json")]
    headers += [(b"content-length", str(len(data)).encode())]
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": operator.scheme,
        "path": path,
        "raw_path": path.encode(),
        "root_path": operator.root_path,
        "query_string": query.encode(),
        "headers": headers,
        "server": operator.server,
        "client": operator.client,
        "state": {},
        IN_PROCESS: True,
        **({ACT_ADMIT: act_admit} if act_admit is not None else {}),
        **({DRIVE: dict(drive)} if drive is not None else {}),
        **({COMMANDER: True} if commander else {}),
    }
    sent = {"done": False}

    async def receive() -> dict:
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": data, "more_body": False}

    status = {"code": 500}
    chunks: list[bytes] = []
    size = {"n": 0}

    started = {"v": False}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            started["v"] = True
            status["code"] = int(message["status"])
        elif message["type"] == "http.response.body":
            chunk = message.get("body") or b""
            size["n"] += len(chunk)
            if size["n"] <= RESPONSE_MAX:
                chunks.append(chunk)

    try:
        await app(scope, receive, send)
    except Exception:
        # Starlette's error middleware sends the 500 and THEN re-raises, for the server's log. A
        # browser sees only that 500, and so does a READ made here: HQ's facts tools treat it as
        # "not available" instead of failing the whole turn. A write still raises, as it always
        # did: whether its effect happened is not something a 500 can say.
        if method != "GET" or not started["v"] or status["code"] < 500:
            raise
        log.warning("in-process GET %s answered %d", path, status["code"], exc_info=True)
    raw = b"".join(chunks)
    try:
        parsed: object = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    if size["n"] > RESPONSE_MAX:
        return Response(status=status["code"], body=None, truncated=True)
    return Response(status=status["code"], body=parsed)
