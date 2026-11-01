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
* GET only, a path the adapter KIND allows (`PATHS`), built by the adapter from validated parts;
* request headers fixed per kind (`HEADERS`), never supplied by an adapter;
* a bounded body (``BODY_MAX``), identity encoding, connect / read / total deadlines;
* errors are short codes with a content-free detail: never the token, never the reply body.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from . import net_vetting

BODY_MAX = 1024 * 1024
CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 10.0
TOTAL_TIMEOUT_S = 20.0
#: Which paths each adapter kind may request (#1826): anchored, and checked BEFORE the token is
#: fetched. A kind with no row reads nothing. Writes stay impossible whatever the path: the
#: transport sends GET only.
PATHS = {
    "forgejo-api": re.compile(r"/api/v1/[A-Za-z0-9._~%/-]*"),
    "github-rest": re.compile(r"/(?:user|search|repos)(?:/[A-Za-z0-9._~%/-]*)?"),
}
#: Extra request headers per kind, fixed here (an adapter cannot add one).
HEADERS = {
    "github-rest": {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    },
}
#: Reply headers a caller may ask for (`_request(headers_out=…)`); everything else stays here.
REPLY_HEADERS = frozenset({"x-oauth-scopes"})

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


def _token(bound: Bound) -> str:
    from .connectors import store

    try:
        inst, token = store.token_for(bound.instance_id, bound.generation, for_read=bound.for_read)
    except store.NotFound:
        raise ConnectorError("stale", "the connector was removed") from None
    except store.Refused as e:
        raise ConnectorError(e.code, str(e)) from None
    except store.StoreUnreadable as e:
        raise ConnectorError("store_unreadable", str(e)) from None
    if inst["origin"] != bound.origin:
        raise ConnectorError("stale", "the connector changed since this was asked")
    return token


async def _request(
    bound: Bound,
    method: str,
    path: str,
    params: dict | None = None,
    *,
    headers_out: dict[str, str] | None = None,
) -> object:
    """THE outbound call (inventoried). GET ``<origin><path>?<params>``; the parsed JSON reply.

    ``headers_out``, when given, receives the reply headers named in `REPLY_HEADERS` (lowercase
    keys) and nothing else."""
    if method != "GET":
        raise ConnectorError("refused", "connectors only read")
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
            token = await asyncio.to_thread(_token, bound)
            host = dest.host_header
            # The kind's fixed headers first, so they can set `Accept` but never the credential,
            # the Host pin or the identity encoding, which are written after them.
            headers = {
                "Accept": "application/json",
                **HEADERS.get(bound.adapter, {}),
                "Authorization": f"{bound.auth_scheme} {token}",
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
                req = client.build_request(
                    method, dest.url, params=clean, headers=headers, extensions=extensions
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
    except TimeoutError as e:
        raise ConnectorError("timeout", f"{host} did not answer in time") from e
    except httpx.TimeoutException as e:
        raise ConnectorError("timeout", f"{host} did not answer in time") from e
    except httpx.HTTPError as e:
        raise ConnectorError("unreachable", f"{host} could not be reached") from e
    try:
        return json.loads(bytes(buf))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ConnectorError("bad_reply", f"{host} did not answer JSON") from None
