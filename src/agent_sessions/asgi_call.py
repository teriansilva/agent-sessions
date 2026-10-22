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
from dataclasses import dataclass

#: The headers a confirmed operation forwards, lowercase as ASGI carries them.
FORWARDED = (b"cookie", b"x-csrf-token", b"origin")
#: The largest response body kept. Every catalog route answers a small JSON object.
RESPONSE_MAX = 1 << 20


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


async def call(
    app, operator: Operator, method: str, path: str, body: dict | None, *, query: str = ""
) -> Response:
    """Send ``method path`` with ``body`` (JSON) through ``app`` as ``operator``. ``query`` is a
    fixed string the caller authors (a facts read), never part of a catalog path."""
    if not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("a catalog path is an absolute path with no query")
    data = b"" if body is None else json.dumps(body).encode()
    headers = list(operator.headers)
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

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            status["code"] = int(message["status"])
        elif message["type"] == "http.response.body":
            chunk = message.get("body") or b""
            size["n"] += len(chunk)
            if size["n"] <= RESPONSE_MAX:
                chunks.append(chunk)

    await app(scope, receive, send)
    raw = b"".join(chunks)
    try:
        parsed: object = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    if size["n"] > RESPONSE_MAX:
        return Response(status=status["code"], body=None, truncated=True)
    return Response(status=status["code"], body=parsed)
