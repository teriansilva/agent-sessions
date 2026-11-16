"""How an instance says where it can be reached, and how a dialer checks that (#1246 §4).

An endpoint is a small dict. This phase knows one kind::

    {"t": "direct", "url": "wss://host[:port][/prefix]/ws/fed"}

The relay kind arrives with the relay transport. An endpoint always comes from the other
instance (inside an invite, or in ``pair.hello``), so :func:`clean` treats it as untrusted input:
unknown kinds and malformed entries are dropped, never repaired.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

WS_PATH = "/ws/fed"
URL_MAX = 512
ENDPOINTS_MAX = 4


def _direct(url: object) -> dict | None:
    if not isinstance(url, str) or not url or len(url) > URL_MAX or not url.isascii():
        return None
    if any(ch.isspace() for ch in url):
        return None
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return None
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        return None
    if parts.username or parts.password or parts.query or parts.fragment:
        return None
    if not parts.path.endswith(WS_PATH):
        return None
    return {"t": "direct", "url": url}


def clean(raw: object) -> list[dict]:
    """The well-formed endpoints in ``raw``, in order, without duplicates, at most
    :data:`ENDPOINTS_MAX`."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw[: ENDPOINTS_MAX * 4]:
        if not isinstance(item, dict):
            continue
        entry = _direct(item.get("url")) if item.get("t") == "direct" else None
        if entry is not None and entry not in out:
            out.append(entry)
        if len(out) >= ENDPOINTS_MAX:
            break
    return out


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def from_origin(origin: str | None) -> list[dict]:
    """This instance's own direct endpoint, from its configured public origin.

    A loopback origin is not advertised: another host cannot reach it, and a peer dialing its own
    loopback would only reach itself. Such an instance is reachable through the relay transport.
    """
    if not origin:
        return []
    try:
        parts = urlsplit(origin)
    except ValueError:
        return []
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return []
    if _is_loopback_host(parts.hostname):
        return []
    scheme = "wss" if parts.scheme == "https" else "ws"
    entry = _direct(f"{scheme}://{parts.netloc}{parts.path.rstrip('/')}{WS_PATH}")
    return [entry] if entry else []
