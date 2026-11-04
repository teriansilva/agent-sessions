"""THE outbound site for connectors (#1746 D2, #1750): every connector request goes through
`_request`, the one call inventoried in ``tests/test_prompts_registry.py``.

**The adapter never sees the token.** An adapter supplies a `Bound` (the instance id, its
generation, its origin — no secret) and a path under that origin. This module reads the token from
the store for exactly that generation (`store.token_for`: a changed instance answers ``stale``)
and attaches ``Authorization: <scheme> <token>`` itself.

**Transport rules** (the AI endpoint's and the speech endpoints', ``net_vetting``):

* the origin's host is resolved ONCE; a link-local, metadata, multicast, unspecified or reserved
  address is refused; the connection goes to the VETTED address, with the Host header and the TLS
  server name kept on the configured hostname, so the certificate is still verified against it;
* a token travels over ``https`` only — ``http`` only when the origin resolves to loopback, where
  it never leaves this machine (``net_vetting.TOKEN_HTTPS_OR_LOOPBACK``);
* no ambient proxies (``trust_env=False``), no redirects (one could carry the token elsewhere);
* only methods that READ, per kind (`METHODS`: GET; WebDAV's PROPFIND for `nextcloud`, its body
  fixed here in `PROPFIND_BODIES`), on a path the adapter KIND allows (`PATHS`), built by the
  adapter from validated parts;
* request headers fixed per kind (`HEADERS`), never supplied by an adapter;
* a bounded body (``BODY_MAX``), identity encoding, connect / read / total deadlines;
* errors are short codes with a content-free detail: never the token, never the reply body.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import socket
import xml.etree.ElementTree as ET  # noqa: S405 — parsed only after `_parse_xml` refuses DTDs
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit
from xml.parsers import expat  # noqa: S410 — used only to REFUSE declarations (`_parse_xml`)

import httpx

from . import net_vetting

BODY_MAX = 1024 * 1024
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 10.0
TOTAL_TIMEOUT_S = 20.0
#: Which paths each adapter kind may request (#1826): anchored, and checked BEFORE the token is
#: fetched. A kind with no row reads nothing. Writes stay impossible whatever the path: every
#: method in `METHODS` only reads.
PATHS = {
    "forgejo-api": re.compile(r"/api/v1/[A-Za-z0-9._~%/-]*"),
    "github-rest": re.compile(r"/(?:user|search|repos)(?:/[A-Za-z0-9._~%/-]*)?"),
    "nextcloud": re.compile(
        r"/remote\.php/dav/files/[A-Za-z0-9._~%-]+(?:/[A-Za-z0-9._~%/-]*)?"
        r"|/ocs/v2\.php/(?:cloud/user|search/providers/files/search)"
    ),
    # Telegram (#1832): the method only; the transport prefixes `/bot<token>` after vetting.
    "telegram-bot": re.compile(r"/(?:getMe|getUpdates)"),
    # Matrix (#1832): who the bot is, its rooms, and per room its name, encryption and members.
    "matrix-cs": re.compile(
        r"/_matrix/client/v3/(?:account/whoami|joined_rooms"
        r"|rooms/[A-Za-z0-9._~%-]+/(?:state/m\.room\.(?:name|encryption)|joined_members))"
    ),
}
#: The methods each kind may send (#1829). PUT, DELETE, MOVE, COPY, MKCOL, PROPPATCH, POST: never.
METHODS = {
    "forgejo-api": frozenset({"GET"}),
    "github-rest": frozenset({"GET"}),
    "nextcloud": frozenset({"GET", "PROPFIND"}),
    "telegram-bot": frozenset({"GET"}),
    "matrix-cs": frozenset({"GET"}),
}
#: The PROPFIND bodies, fixed per kind and named by the adapter; it never supplies one.
PROPFIND_BODIES = {
    "nextcloud": {
        "entries": (
            b'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="DAV:"><d:prop>'
            b"<d:resourcetype/><d:getcontentlength/><d:getlastmodified/><d:getcontenttype/>"
            b"</d:prop></d:propfind>"
        ),
    },
}
#: Reply kinds `_request` returns: parsed JSON, a parsed XML element, or the raw bytes.
REPLIES = frozenset({"json", "xml", "text"})
#: Extra request headers per kind, fixed here (an adapter cannot add one).
HEADERS = {
    "github-rest": {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    },
    # Nextcloud's OCS routes refuse a request without it (CSRF protection); WebDAV ignores it.
    "nextcloud": {"OCS-APIRequest": "true"},
}
#: Reply headers a caller may ask for (`_request(headers_out=…)`); everything else stays here.
REPLY_HEADERS = frozenset({"x-oauth-scopes"})

#: A Telegram token as it appears in a request URL (#1832).
_BOT_PATH_RE = re.compile(r"/bot[0-9]{1,20}:[A-Za-z0-9_-]+")


class _RedactBotPaths(logging.Filter):
    """httpx logs every request's URL at INFO (``HTTP Request: GET https://…/bot<token>/getMe``),
    and a Telegram URL holds the bot token. This filter, on httpx's own logger, rewrites the token
    out of the record before any handler sees it, whatever level the app logs at (#1832: a test
    caught the token in the captured log)."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 — a malformed record is not ours to fix
            return True
        if "/bot" in msg:
            record.msg = _BOT_PATH_RE.sub("/bot<redacted>", msg)
            record.args = ()
        return True


logging.getLogger("httpx").addFilter(_RedactBotPaths())

#: Test seams: tests mount an `httpx.MockTransport` and pin resolution.
_TRANSPORT: httpx.AsyncBaseTransport | None = None


class ConnectorError(Exception):
    """A connector request failed. ``code`` is stable and short; ``detail`` is safe to show."""

    def __init__(self, code: str, detail: str, *, status: int | None = None):
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.status = status


@dataclass(frozen=True)
class Bound:
    """One instance as a request is bound to it. It carries NO secret."""

    instance_id: str
    generation: int
    origin: str
    auth_scheme: str
    label: str
    #: The manifest's `adapter.kind`: selects the path rule and the fixed headers.
    adapter: str = "forgejo-api"
    #: The account's id on the service where paths name it (Nextcloud's DAV user id, #1829):
    #: taken from the instance's current test, never from model text.
    principal: str = ""
    #: A send (#1833): the token fetch checks SENDING, not reading (`store.token_for(for_send)`).
    for_send: bool = False
    #: An HQ read (True) needs read to be enabled AT THE TOKEN FETCH, not only when the tool
    #: began; the connection test (False) runs before read can be enabled.
    for_read: bool = True


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def _status_error(status: int, host: str, headers: httpx.Headers | None = None) -> ConnectorError:
    remaining = (headers or {}).get("x-ratelimit-remaining")
    if status in (403, 429) and (remaining == "0" or status == 429):
        # GitHub answers an exhausted rate limit with 403 (or 429): not a bad token (#1826).
        return ConnectorError(
            "rate_limited", f"{host} says this token's rate limit is used up for now", status=status
        )
    if status in (401, 403):
        return ConnectorError("rejected", f"{host} rejected the token ({status})", status=status)
    if status == 404:
        return ConnectorError("not_found", f"{host} has no such item (404)", status=status)
    if 300 <= status < 400:
        return ConnectorError(
            "redirect", f"{host} answered with a redirect, which is not followed", status=status
        )
    return ConnectorError("failed", f"{host} answered {status}", status=status)


#: A Telegram bot token as it may stand in a URL path (`<digits>:<base64url>`): anything else is
#: refused before it can change what the path means.
_PATH_TOKEN_RE = re.compile(r"[0-9]{1,20}:[A-Za-z0-9_-]{20,100}")


def _credential(bound: Bound) -> tuple[str | None, str]:
    """``(Authorization value, path prefix)`` for ``bound``, from the store at THIS generation.
    A `path-token` (Telegram, #1832) has no header: its token is the URL's first path segment,
    `/bot<token>`, so the returned prefix is a secret and must never reach an error or a log."""
    token, account = _token(bound)
    if bound.auth_scheme == "bot":
        if not _PATH_TOKEN_RE.fullmatch(token):
            raise ConnectorError("invalid_token", "that is not a Telegram bot token")
        return None, f"/bot{token}"
    if bound.auth_scheme != "Basic":
        return f"{bound.auth_scheme} {token}", ""
    if not account:
        raise ConnectorError("needs_account", "the connector's account needs to be entered")
    basic = base64.b64encode(f"{account}:{token}".encode()).decode("ascii")
    return f"Basic {basic}", ""


#: An XML declaration's encoding, if it names one.
_XML_ENCODING_RE = re.compile(rb"""^\s*<\?xml[^>]*\bencoding\s*=\s*["']([A-Za-z0-9._-]+)["']""")


class _Refused(Exception):
    """Raised from inside the parser when a document type or entity is declared."""


def _parse_xml(raw: bytes, host: str) -> ET.Element:
    """A reply's XML: UTF-8 only, and no document type or entity declared at all, so nothing can
    be expanded or fetched. Two layers (Hermes on #1834: a UTF-16 document hid its `<!ENTITY>`
    from a byte scan):

    1. the bytes must BE UTF-8 — no UTF-16/32 byte-order mark, no NUL byte, no declared encoding
       other than UTF-8/ASCII, a strict decode — and the decoded text must hold no `<!DOCTYPE` /
       `<!ENTITY`;
    2. the parser itself refuses any document-type or entity declaration it meets, whatever the
       encoding, so the first layer is not the only one."""
    bad = ConnectorError("bad_reply", f"{host} answered XML with a document type")
    if raw.startswith((b"\xfe\xff", b"\xff\xfe", b"\x00\x00\xfe\xff", b"\xff\xfe\x00\x00")):
        raise bad
    if b"\x00" in raw:
        raise bad
    declared = _XML_ENCODING_RE.match(raw)
    if declared and declared.group(1).lower() not in (b"utf-8", b"utf8", b"us-ascii", b"ascii"):
        raise bad
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ConnectorError("bad_reply", f"{host} did not answer UTF-8 XML") from None
    upper = text.upper()
    if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
        raise bad

    def refuse(*_args) -> None:
        raise _Refused()

    # The second layer: a bare expat pass whose declaration handlers refuse, BEFORE ElementTree
    # (whose C parser exposes no handlers) ever sees the bytes. Nothing is expanded on this pass:
    # it stops at the first declaration.
    scan = expat.ParserCreate()
    scan.StartDoctypeDeclHandler = refuse
    scan.EntityDeclHandler = refuse
    scan.UnparsedEntityDeclHandler = refuse
    scan.ExternalEntityRefHandler = refuse
    try:
        scan.Parse(raw, True)
    except _Refused:
        raise bad from None
    except expat.ExpatError:
        raise ConnectorError("bad_reply", f"{host} did not answer XML") from None
    try:
        return ET.fromstring(raw)  # noqa: S314 — no declaration got past both layers above
    except ET.ParseError:
        raise ConnectorError("bad_reply", f"{host} did not answer XML") from None


def _token(bound: Bound) -> tuple[str, str]:
    from .connectors import store

    try:
        inst, token = store.token_for(
            bound.instance_id, bound.generation, for_read=bound.for_read, for_send=bound.for_send
        )
    except store.NotFound:
        raise ConnectorError("stale", "the connector was removed") from None
    except store.Refused as e:
        raise ConnectorError(e.code, str(e)) from None
    except store.StoreUnreadable as e:
        raise ConnectorError("store_unreadable", str(e)) from None
    if inst["origin"] != bound.origin:
        raise ConnectorError("stale", "the connector changed since this was asked")
    return token, inst.get("account") or ""


async def _request(
    bound: Bound,
    method: str,
    path: str,
    params: dict | None = None,
    *,
    headers_out: dict[str, str] | None = None,
    propfind: str | None = None,
    depth: str | None = None,
    reply: str = "json",
) -> object:
    """THE outbound call (inventoried). ``<method> <origin><path>?<params>``; the reply as asked
    (`REPLIES`): parsed JSON (default), a parsed XML element, or the raw bytes.

    ``headers_out``, when given, receives the reply headers named in `REPLY_HEADERS` (lowercase
    keys) and nothing else. A ``PROPFIND`` names its body (``propfind``, from `PROPFIND_BODIES`)
    and its ``depth`` (``"0"`` or ``"1"``); nothing else may carry either."""
    if method not in METHODS.get(bound.adapter, ()):
        raise ConnectorError("refused", "connectors only read")
    if reply not in REPLIES:
        raise ConnectorError("refused", "not a reply this transport reads")
    body: bytes | None = None
    if method == "PROPFIND":
        body = PROPFIND_BODIES.get(bound.adapter, {}).get(propfind or "")
        if body is None or depth not in ("0", "1"):
            raise ConnectorError("refused", "not a PROPFIND this connector sends")
    elif propfind is not None or depth is not None:
        raise ConnectorError("refused", "only a PROPFIND carries a body")
    rule = PATHS.get(bound.adapter)
    if (
        rule is None
        or not rule.fullmatch(path)
        or "//" in path
        or "/../" in f"{path}/"
        or "/./" in path
    ):
        raise ConnectorError("refused", "not a path this connector reads")
    clean: dict[str, str] = {}
    for k, v in (params or {}).items():
        if not isinstance(k, str) or not isinstance(v, str | int) or isinstance(v, bool):
            raise ConnectorError("refused", "bad query parameter")
        clean[k] = str(v)
    url = f"{bound.origin}{path}"
    host = urlsplit(url).netloc
    try:
        # ONE budget for everything: resolving (a stalled resolver must not hold an HQ tool),
        # the token fetch, connecting, and reading the body.
        async with asyncio.timeout(TOTAL_TIMEOUT_S):
            dest = await net_vetting.vet(
                url,
                resolve=_resolve,
                error=ConnectorError,
                policy=net_vetting.TOKEN_HTTPS_OR_LOOPBACK,
            )
            # The token is fetched AFTER vetting and right before sending, under the read check:
            # a read disabled while the name was resolving sends nothing.
            credential, prefix = await asyncio.to_thread(_credential, bound)
            host = dest.host_header
            # The kind's fixed headers first, so they can set `Accept` but never the credential,
            # the Host pin or the identity encoding, which are written after them.
            headers = {
                "Accept": "application/json",
                **HEADERS.get(bound.adapter, {}),
                **({"Authorization": credential} if credential else {}),
                "Host": dest.host_header,
                "Accept-Encoding": "identity",
            }
            extensions = {"sni_hostname": dest.sni} if dest.sni else None
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                transport=_TRANSPORT,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                if body is not None:
                    headers["Depth"] = depth or "0"
                    headers["Content-Type"] = "application/xml; charset=utf-8"
                target = dest.url
                if prefix:
                    parts = urlsplit(dest.url)
                    target = parts._replace(path=prefix + parts.path).geturl()
                req = client.build_request(
                    method,
                    target,
                    params=clean,
                    headers=headers,
                    content=body,
                    extensions=extensions,
                )
                resp = await client.send(req, stream=True)
                try:
                    if not 200 <= resp.status_code < 300:
                        raise _status_error(resp.status_code, host, resp.headers)
                    if headers_out is not None:
                        for name in REPLY_HEADERS:
                            if name in resp.headers:
                                headers_out[name] = resp.headers[name][:500]
                    encoding = resp.headers.get("content-encoding", "identity").strip().lower()
                    if encoding not in ("", "identity"):
                        raise ConnectorError("bad_reply", f"{host} answered compressed")
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():  # identity only (checked above)
                        buf += chunk
                        if len(buf) > BODY_MAX:
                            raise ConnectorError("too_large", f"{host} answered too much")
                finally:
                    await resp.aclose()
    except ConnectorError:
        raise
    except TimeoutError:
        raise ConnectorError("timeout", f"{host} did not answer in time") from None
    except httpx.TimeoutException:
        # `from None` everywhere here: an httpx error can carry the request URL, and a Telegram
        # URL holds the bot token (#1832). The short code is all a caller ever needs.
        raise ConnectorError("timeout", f"{host} did not answer in time") from None
    except httpx.HTTPError:
        raise ConnectorError("unreachable", f"{host} could not be reached") from None
    if reply == "text":
        return bytes(buf)
    if reply == "xml":
        return _parse_xml(bytes(buf), host)
    try:
        return json.loads(bytes(buf))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ConnectorError("bad_reply", f"{host} did not answer JSON") from None


# ---- sending (#1833) ---------------------------------------------------------------------------

#: Each sending kind's ONE request, fixed here: the method, the path (with the recipient and the
#: idempotency key as encoded segments) and the body's shape. An adapter supplies only the
#: recipient id and the text; it never names a route.
SENDS = {
    "telegram-bot": ("POST", "/sendMessage"),
    "matrix-cs": ("PUT", "/_matrix/client/v3/rooms/{room}/send/m.room.message/{txn}"),
}
_SEND_PATHS = {
    "telegram-bot": re.compile(r"/sendMessage"),
    "matrix-cs": re.compile(
        r"/_matrix/client/v3/rooms/[A-Za-z0-9._~%-]+/send/m\.room\.message/[A-Za-z0-9._~%-]+"
    ),
}
#: Before any byte of the request left: nothing was sent.
_PRE_WRITE = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
)


def _send_request(
    adapter: str, recipient_id: str, text: str, idem_key: str
) -> tuple[str, str, dict]:
    from urllib.parse import quote

    method, template = SENDS[adapter]
    if adapter == "telegram-bot":
        if not re.fullmatch(r"-?[0-9]{1,20}", recipient_id):
            raise ConnectorError("refused", "not a Telegram chat id")
        body = {
            "chat_id": int(recipient_id),
            "text": text,
            "link_preview_options": {"is_disabled": True},
        }
        return method, template, body
    path = template.format(room=quote(recipient_id, safe=""), txn=quote(idem_key, safe=""))
    return method, path, {"msgtype": "m.text", "body": text}


def _message_id(adapter: str, reply: object) -> str:
    if adapter == "telegram-bot":
        res = reply.get("result") if isinstance(reply, dict) and reply.get("ok") is True else None
        mid = res.get("message_id") if isinstance(res, dict) else None
        return str(mid) if isinstance(mid, int) and not isinstance(mid, bool) else ""
    eid = reply.get("event_id") if isinstance(reply, dict) else None
    return eid[:255] if isinstance(eid, str) and eid.startswith("$") else ""


#: ``final_check(fenced)`` raises `ConnectorError` when the request may no longer leave (#1833,
#: Hermes 7610): the recipient withdrawn, a secret now in the text. Called twice per request.
FinalCheck = Callable[[bool], None]


async def _send_once(
    bound: Bound, method: str, path: str, body: dict, final_check: FinalCheck | None = None
) -> dict:
    """ONE provider request. ``{outcome, code, message_id}``, where `failed` means it is PROVEN
    that nothing was sent (refused before any byte left, or the provider answered 4xx), `sent`
    needs the provider's own message id, and anything else is `unconfirmed`.

    THE FINAL CHECK runs at the outbound boundary, twice. First, after vetting and the token, in
    a worker thread under the caller's fence. Then again, with no fence, at the moment the first
    byte would be written, from httpcore's ``send_request_headers.started`` trace (a connect and
    a TLS handshake can take seconds; the check moves past them). A refusal at either point
    raises before anything was written, so it is `failed`, which is proven unsent. What is left is
    the interval from the last check's return to the end of the request's socket write: a change
    landing there is not caught. Leaving it open is the operator's decision
    (#1839, 2026-11-01; `docs/invariants/connectors.md`): closing it would mean holding the app-wide
    registration fence across network I/O."""
    url = f"{bound.origin}{path}"
    try:
        async with asyncio.timeout(TOTAL_TIMEOUT_S):
            dest = await net_vetting.vet(
                url,
                resolve=_resolve,
                error=ConnectorError,
                policy=net_vetting.TOKEN_HTTPS_OR_LOOPBACK,
            )
            credential, prefix = await asyncio.to_thread(_credential, bound)
            if final_check is not None:
                await asyncio.to_thread(final_check, True)
            headers = {
                "Accept": "application/json",
                **HEADERS.get(bound.adapter, {}),
                **({"Authorization": credential} if credential else {}),
                "Host": dest.host_header,
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
            }
            target = dest.url
            if prefix:
                parts = urlsplit(dest.url)
                target = parts._replace(path=prefix + parts.path).geturl()
            extensions: dict = {"sni_hostname": dest.sni} if dest.sni else {}
            if final_check is not None:

                async def trace(name: str, info: dict) -> None:
                    if name.endswith("send_request_headers.started"):
                        final_check(False)  # raises before the first byte is written

                extensions["trace"] = trace
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
                transport=_TRANSPORT,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                req = client.build_request(
                    method, target, headers=headers, content=json.dumps(body).encode(),
                    extensions=extensions or None,
                )  # fmt: skip
                try:
                    resp = await client.send(req, stream=True)
                except _PRE_WRITE:
                    return {"outcome": "failed", "code": "unreachable", "message_id": ""}
                try:
                    status = resp.status_code
                    buf = bytearray()
                    async for chunk in resp.aiter_bytes():
                        buf += chunk
                        if len(buf) > 64 * 1024:
                            break
                finally:
                    await resp.aclose()
    except ConnectorError as e:
        # Vetting or the credential refused it: no request was written.
        return {"outcome": "failed", "code": e.code[:40], "message_id": ""}
    except (TimeoutError, httpx.HTTPError):
        return {"outcome": "unconfirmed", "code": "no_answer", "message_id": ""}
    if 400 <= status < 500:
        code = (
            "rejected" if status in (401, 403) else "rate_limited" if status == 429 else "refused"
        )
        return {"outcome": "failed", "code": code, "message_id": "", "status": status}
    if not 200 <= status < 300:
        return {
            "outcome": "unconfirmed",
            "code": "server_error",
            "message_id": "",
            "status": status,
        }
    try:
        reply = json.loads(bytes(buf))
    except (ValueError, UnicodeDecodeError, RecursionError):
        reply = None
    mid = _message_id(bound.adapter, reply)
    if not mid:
        return {"outcome": "unconfirmed", "code": "no_message_id", "message_id": ""}
    return {"outcome": "sent", "code": "", "message_id": mid}


async def _send(
    bound: Bound,
    recipient_id: str,
    text: str,
    idem_key: str,
    *,
    final_check: FinalCheck | None = None,
    recheck: Callable[[], Awaitable[bool]] | None = None,
) -> dict:
    """THE send site (#1833). One claim, one message: a non-idempotent provider (Telegram) gets
    exactly one request whatever happens; an idempotent one (Matrix, keyed by ``idem_key``) may be
    asked once more with the SAME key after an ambiguous end, within this one call only."""
    from .connectors import kinds

    if bound.adapter not in SENDS or not bound.for_send:
        return {"outcome": "failed", "code": "refused", "message_id": ""}
    try:
        method, path, body = _send_request(bound.adapter, recipient_id, text, idem_key)
    except ConnectorError as e:
        return {"outcome": "failed", "code": e.code, "message_id": ""}
    if not _SEND_PATHS[bound.adapter].fullmatch(path):
        return {"outcome": "failed", "code": "refused", "message_id": ""}
    out = await _send_once(bound, method, path, body, final_check)
    if out["outcome"] == "unconfirmed" and bound.adapter in kinds.IDEMPOTENT_SENDS:
        # The same key again: the homeserver answers a repeat with the event it already stored.
        # Only a definite `sent` replaces the first outcome; any other answer to the repeat says
        # nothing about whether the FIRST request was stored, so it stays `unconfirmed`.
        # The repeat is a new request to a room that may have changed meanwhile (#1833, Hermes
        # 7612): ``recheck`` looks at it live again, and a room no longer eligible, or one that
        # can't be checked, gets no repeat.
        if recheck is None or await recheck():
            again = await _send_once(bound, method, path, body, final_check)
            if again["outcome"] == "sent":
                out = again
    return out
