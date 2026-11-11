"""The `smtp` adapter (#1836): mail through the Commander's own account. It reads nothing and
discovers nothing (the Commander types each address in Settings); its probe and its send are the
mail transport's (`connector_smtp`), which alone holds the password."""

from __future__ import annotations

from ... import connector_smtp
from ...connector_http import Bound


async def probe(bound: Bound) -> dict:
    """Connect, TLS, AUTH, QUIT: nothing is sent. The login is the account it signed in as."""
    return await connector_smtp.probe(bound)
