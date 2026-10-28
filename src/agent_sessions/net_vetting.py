"""The vetted-address rule shared by every transport that connects to an operator-chosen host.

Extracted from ``speech_endpoints`` (#1750) so the speech endpoints and the connectors apply ONE
rule instead of two copies that drift. It holds no HTTP client and makes no request: it resolves a
URL's host once, refuses the addresses no operator-configured service should live at, and returns
the address to CONNECT to, so a second lookup (a rebinding name answering differently the second
time) never happens (Hermes on #1621).

**Refused:** link-local (169.254/16, fe80::/10, where cloud metadata services answer), the named
metadata addresses outside those ranges, multicast, unspecified and reserved. Loopback and private
LAN addresses are allowed on purpose: self-hosting is the point. An IPv4 address embedded in IPv6
(mapped, 6to4, NAT64) is judged as itself.

**The scheme policy is the caller's.** Speech allows ``http`` to any vetted address (its token is
optional and its servers are mostly local). A token-bearing connector passes
``TOKEN_HTTPS_OR_LOOPBACK``: ``https`` always, ``http`` only when every resolved address is
loopback, so a token never crosses a network in clear text.

Errors are raised through the caller's own error type (``error(code, detail)``), so each transport
keeps its stable short codes.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

#: Scheme policies (`vet`). ``ANY_HTTP``: http or https to any vetted address.
#: ``TOKEN_HTTPS_OR_LOOPBACK``: https, or http only to an address that is loopback.
ANY_HTTP = "any_http"
TOKEN_HTTPS_OR_LOOPBACK = "token_https_or_loopback"  # noqa: S105 — a policy name
POLICIES = frozenset({ANY_HTTP, TOKEN_HTTPS_OR_LOOPBACK})

# Cloud metadata endpoints outside the link-local ranges (AWS IPv6, Alibaba's CGNAT address).
METADATA = frozenset(
    ipaddress.ip_address(a)
    for a in (
        "169.254.169.254", "fd00:ec2::254",  # AWS (IPv4, IPv6)
        "100.100.100.200",  # Alibaba Cloud
        "fd20:ce::254",  # Google Cloud IPv6
        "fd00:c1::a9fe:a9fe",  # Oracle Cloud IPv6
    )
)  # fmt: skip
_NAT64 = ipaddress.ip_network("64:ff9b::/96")

IP = ipaddress.IPv4Address | ipaddress.IPv6Address


def unsafe_ip(ip: IP) -> bool:
    """Refused destinations: link-local (metadata), named metadata addresses, multicast,
    unspecified, reserved. Loopback is allowed on purpose (a local server), including `::1`,
    which `ipaddress` files under reserved `::/8`. An IPv4 address embedded in IPv6 (mapped,
    6to4, NAT64) is judged as itself."""
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour
        if embedded is None and ip in _NAT64:
            embedded = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        if embedded is not None:
            # Loopback reached through a translator (NAT64, 6to4) is the GATEWAY's loopback, not
            # ours: only the plain mapped form (::ffff:127.x) counts as this host.
            if embedded.is_loopback and ip.ipv4_mapped is None:
                return True
            return unsafe_ip(embedded)
    if ip.is_loopback:
        return False
    return (
        ip in METADATA or ip.is_link_local or ip.is_multicast or ip.is_unspecified
        or ip.is_reserved
    )  # fmt: skip


def is_loopback(ip: IP) -> bool:
    """This host: 127.0.0.0/8, ``::1``, or the plain mapped ``::ffff:127.x`` form."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped.is_loopback
    return ip.is_loopback


@dataclass(frozen=True)
class Pinned:
    """A vetted destination: the URL to CONNECT to (the vetted address), the Host header and the
    TLS server name (the configured hostname, so the certificate is still checked against it)."""

    url: str
    host_header: str
    sni: str | None
    #: The vetted address the connection goes to.
    address: str = ""


async def vet(
    url: str,
    *,
    resolve: Callable[[str, int], Awaitable[list[str]]],
    error: Callable[[str, str], Exception],
    policy: str = ANY_HTTP,
) -> Pinned:
    """Resolve the URL's host ONCE (``resolve(host, port)``), refuse it when any address is
    unsafe, apply the scheme ``policy``, and pin the connection to a vetted address."""
    if policy not in POLICIES:
        raise ValueError(f"unknown scheme policy {policy!r}")
    try:
        parts = urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as e:
        raise error("invalid_url", "the endpoint URL is not valid") from e
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise error("invalid_url", "the endpoint URL must be http(s) with a host")
    if parts.username or parts.password:
        raise error("invalid_url", "the endpoint URL must not carry credentials")
    host = parts.hostname
    try:
        addrs = await resolve(host, port)
    except OSError as e:
        raise error("unreachable", f"{host} could not be resolved") from e
    vetted: list[IP] = []
    for addr in addrs:
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            raise error(
                "refused_destination", f"{host} resolves to an unreadable address"
            ) from None
        if unsafe_ip(ip):
            raise error("refused_destination", f"{host} resolves to a refused address")
        vetted.append(ip)
    if not vetted:
        raise error("unreachable", f"{host} could not be resolved")
    if (
        policy == TOKEN_HTTPS_OR_LOOPBACK
        and parts.scheme == "http"
        and not all(is_loopback(ip) for ip in vetted)
    ):
        raise error(
            "https_required",
            f"{host} is not this machine: a token is only sent over https (http only to loopback)",
        )
    ip = vetted[0]
    literal = f"[{ip}]" if ip.version == 6 else str(ip)
    netloc = f"{literal}:{port}"
    pinned = parts._replace(netloc=netloc).geturl()
    host_header = parts.netloc.rsplit("@", 1)[-1]
    return Pinned(pinned, host_header, host if parts.scheme == "https" else None, str(ip))
