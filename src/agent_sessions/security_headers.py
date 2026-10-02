"""Browser containment headers (#612 P1).

A defense-in-depth response-header set stamped on every HTTP response by the middleware in
``main.create_app`` — CSP, frame-ancestors / X-Frame-Options (clickjacking), ``nosniff``,
Referrer-Policy, Permissions-Policy, and HSTS on TLS. Tuned against exactly what the app ships:

* **SPA (Vite build).** Scripts are hashed same-origin modules plus an external
  ``/registerSW.js``; the ONE inline ``<script>`` is the synchronous theme-init (a
  flash-of-wrong-theme guard) in ``index.html``. Its ``sha256`` is computed from the *served*
  file, so ``script-src`` locks to ``'self'`` + that hash with **no** ``'unsafe-inline'`` and
  self-updates if the script changes.
* **Styles.** xterm renders with runtime-generated inline styles, and the login /
  change-password Jinja templates each carry an inline ``<style>`` — neither can be hashed, so
  ``style-src`` keeps ``'unsafe-inline'``. Style injection is far lower risk than script
  injection, which stays locked down.
* **connect-src.** The terminal WebSocket and ``/api`` are same-origin ⇒ ``'self'``. The Home
  Free connect page (``connect.html``) is served OUTSIDE this app (its own edge/nginx) and its
  cross-origin relay traffic is therefore not this middleware's concern.

The exact directive list is also mirrored in ``deploy/nginx.example.conf`` for the TLS-terminating
layer; either layer may own the headers (they use ``setdefault`` so they don't stack).
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

# Fixed-value headers stamped on every response.
STATIC_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # Turn off device/capability APIs the app never uses. `microphone=(self)` (not `()`) keeps
    # push-to-talk dictation (#483/#486) working — the Web Speech API needs the mic granted to the
    # app's own origin, and an empty allowlist denies it to self too. Clipboard + fullscreen are
    # left at their defaults on purpose — the terminal and Compose paste depend on them.
    "Permissions-Policy": "camera=(), microphone=(self), geolocation=(), payment=(), usb=()",
}

# Only meaningful over HTTPS; the middleware sets it when the request arrived over TLS.
HSTS = "max-age=31536000; includeSubDomains"

# An inline <script> is one with a body and no ``src=`` attribute.
_INLINE_SCRIPT = re.compile(rb"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.S | re.I)


def inline_script_hashes(web_dist: Path) -> list[str]:
    """CSP ``'sha256-<b64>'`` source-expressions for each inline ``<script>`` (no ``src``) in
    the built ``index.html``. Hashing the app's own inline theme-init lets ``script-src`` allow
    it without ``'unsafe-inline'``; computing it from the *served* file keeps it correct across
    edits. Returns ``[]`` when there's no built index (dev / test) — ``script-src`` is then just
    ``'self'``. The hashed bytes are exactly the text node the browser hashes, so they match."""
    try:
        html = (web_dist / "index.html").read_bytes()
    except OSError:
        return []
    hashes: list[str] = []
    for body in _INLINE_SCRIPT.findall(html):
        digest = hashlib.sha256(body).digest()
        hashes.append("'sha256-" + base64.b64encode(digest).decode("ascii") + "'")
    return hashes


def content_security_policy(script_hashes: list[str]) -> str:
    """The CSP string. ``script-src`` = ``'self'`` + the built index's inline-script hashes."""
    script_src = " ".join(["'self'", *script_hashes])
    directives = [
        "default-src 'self'",
        "base-uri 'self'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        f"script-src {script_src}",
        # Dynamic (xterm) + template inline styles can't be hashed; scripts stay locked down.
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",  # Compose image previews are data:/blob:
        "font-src 'self' data:",
        "connect-src 'self'",  # same-origin /api + terminal WebSocket
        "worker-src 'self' blob:",
        "manifest-src 'self'",
    ]
    return "; ".join(directives)


def base_headers(web_dist: Path) -> dict[str, str]:
    """The full non-HSTS header set, with the CSP resolved once against the built index."""
    return {
        **STATIC_HEADERS,
        "Content-Security-Policy": content_security_policy(inline_script_hashes(web_dist)),
    }


def is_https(*, forwarded_proto: str | None, scheme: str) -> bool:
    """Whether the client reached us over TLS — the ``X-Forwarded-Proto`` the LB/nginx sets
    (authoritative even when uvicorn runs without ``--proxy-headers``, since it's the raw header),
    falling back to the request scheme for a direct TLS connection."""
    return (forwarded_proto or scheme) == "https"
