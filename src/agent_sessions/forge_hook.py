"""A webhook delivery's signature, and the display form of an unsigned header (#2202, #2221).

Read `docs/invariants/webhooks.md` before changing anything here. This module once also typed the
bespoke review loop's events (#2202); that loop was retired (#2222) and only what the generic
webhook route uses is left: the body cap, the signature check and the header display form.

**The signature** is Forgejo's, Gitea's or GitHub's: a hex HMAC-SHA256 of the raw body, keyed with
the webhook secret (`webhooks.signature_header` finds it in any of their header forms). Compared in
constant time against every secret currently valid for the webhook (the current one, and a rotated
one inside its overlap window).
"""

from __future__ import annotations

import hashlib
import hmac
import re

#: The largest body a delivery may carry (#1201 §4).
BODY_MAX = 256 * 1024

_SIG_RE = re.compile(r"^[0-9a-f]{64}$")
_DISPLAY_RE = re.compile(r"[^A-Za-z0-9_.:-]")


def signature_ok(body: bytes, header: str | None, secrets: list[str]) -> bool:
    """Whether ``header`` is the hex HMAC-SHA256 of ``body`` under any of ``secrets``.

    Every secret is tried (no early exit on the first mismatch), so the time taken does not depend
    on which secret matched or how far a forged digest got."""
    if not isinstance(header, str):
        return False
    got = header.strip().lower()
    if not _SIG_RE.match(got):
        # Still spend a comparison per secret: an unsigned and a badly signed delivery take the
        # same time class.
        got = "0" * 64
        ok = False
    else:
        ok = True
    match = False
    for secret in secrets:
        want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        match = hmac.compare_digest(want, got) or match
    return ok and match


def display(value: object, limit: int = 64) -> str:
    """A header value as the run history may show it: a closed alphabet, bounded, never raw."""
    if not isinstance(value, str):
        return ""
    return _DISPLAY_RE.sub("", value)[:limit]
