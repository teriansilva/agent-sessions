"""How an instance says where it can be reached, and how a dialer checks that (#1246 §4).

An endpoint is a small dict of one of two kinds::

    {"t": "direct", "url": "wss://host[:port][/prefix]/ws/fed"}
    {"t": "relay", "url": "wss://relay-host/relay/ws", "name": "<console name>"}

``relay`` names an instance by its console name on a Home Free relay (#1246 P3). It is dialed
through this instance's own Home Free agent and only on the agent's own relay.

An endpoint always comes from the other instance (inside an invite, or in ``pair.hello``), so
:func:`clean` treats it as untrusted input: unknown kinds and malformed entries are dropped,
never repaired.
"""

from __future__ import annotations

import ipaddress
import re
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


#: The relay's rule for a console name, lowercased.
RELAY_NAME_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{2,30}[a-z0-9]\Z")


def _relay(url: object, name: object) -> dict | None:
    if not isinstance(name, str) or not RELAY_NAME_RE.fullmatch(name.lower()):
        return None
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
    return {"t": "relay", "url": url, "name": name.lower()}


def clean(raw: object) -> list[dict]:
    """The well-formed endpoints in ``raw``, in order, without duplicates, at most
    :data:`ENDPOINTS_MAX`."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    for item in raw[: ENDPOINTS_MAX * 4]:
        if not isinstance(item, dict):
            continue
        if item.get("t") == "direct":
            entry = _direct(item.get("url"))
        elif item.get("t") == "relay":
            entry = _relay(item.get("url"), item.get("name"))
        else:
            entry = None
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


def ordered(endpoints) -> list[dict]:
    """Dial order: a direct address first (one hop, no relay session cap), then the relay."""
    return sorted(endpoints, key=lambda e: 0 if e.get("t") == "direct" else 1)
