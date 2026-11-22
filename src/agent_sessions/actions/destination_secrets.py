"""The signing secrets of "Call a webhook" destinations (#2248 P2).

One secret per automation AND destination: made when an automation that calls a webhook is saved
with a destination that has none, returned ONCE by that call (`POST`/`PATCH /api/automations`, or
`POST /api/automations/{aid}/destination-secret` to make a new one), and never again. A changed
destination gets a new secret; the old one is dropped in the same write, so a secret never signs
for an address it was not shown for.

**At rest** it is only an AES-GCM envelope (`template_secrets.encrypt`, the key file the webhook
and variable secrets use) whose AAD binds it to the automation, the generation and the
destination: ``action-webhook:<aid>:<gen>:<sha256(destination)>``. An envelope copied to another
automation or another destination does not open. The file (``AGENT_SESSIONS_ACTION_SECRETS``,
default ``action-secrets.json`` beside the automations store) is 0600, replaced atomically under a
sidecar lock. A missing, damaged or unopenable entry reads as "no secret": the run is refused, and
nothing is ever sent unsigned.

**Never out.** Only `signing_secret` returns a plaintext, and only the plugin's request site calls
it. No read route, run record, log line or consent line carries one.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets as _secrets
import time
from collections.abc import Callable
from pathlib import Path

from .. import atomicjson, template_secrets

_AID_RE = re.compile(r"^[a-z0-9]{1,64}$")


class SecretError(RuntimeError):
    """The secret could not be stored (no usable key file)."""


def path() -> Path:
    env = os.environ.get("AGENT_SESSIONS_ACTION_SECRETS")
    if env:
        return Path(env)
    from .. import automations_store

    return Path(automations_store.db_path()).with_name("action-secrets.json")


def _aad(aid: str, gen: int, destination: str) -> str:
    digest = hashlib.sha256(destination.encode("utf-8")).hexdigest()
    return f"action-webhook:{aid}:{gen}:{digest}"


def _entry(doc: dict, aid: str) -> dict | None:
    e = doc.get(aid)
    if not (
        isinstance(e, dict)
        and isinstance(e.get("gen"), int)
        and isinstance(e.get("destination"), str)
        and template_secrets.valid_envelope(e.get("env"))
    ):
        return None
    return e


def has(aid: str, destination: str) -> bool:
    """Whether a secret exists for this automation and exactly this destination (opened, so a
    replaced key file reads as none)."""
    return signing_secret(aid, destination) is not None


def state(aid: str, destination: str) -> tuple[bool, float | None]:
    """``(secret set, made at)`` for this destination, from ONE read of the document (the read
    view). Set means it opens: a replaced key file reads as not set."""
    e = _entry(atomicjson.read_json_doc(path()), aid)
    if e is None or e["destination"] != destination:
        return False, None
    if template_secrets.decrypt(_aad(aid, e["gen"], destination), e["env"]) is None:
        return False, None
    at = e.get("at")
    return True, float(at) if isinstance(at, int | float) else None


def made_at(aid: str, destination: str) -> float | None:
    return state(aid, destination)[1]


def mint(
    aid: str, destination: str | None = None, *, current: Callable[[], str | None] | None = None
) -> str | None:
    """A NEW secret for ``aid``, replacing any earlier one; the plaintext is returned to show once.

    ``current`` (the caller's read of the automation's destination AS STORED NOW) is called inside
    this file's write lock and wins over ``destination``: a secret is only ever made for the
    address the automation has at that moment, so a rotation that read an older address can
    never overwrite the secret of the address a concurrent save just set. ``None`` from it (the
    automation no longer calls a webhook) makes nothing and returns None. A save that only needs
    a secret when there is none uses `mint_if_missing`."""
    return _mint(aid, destination, current=current, only_if_missing=False)


def mint_if_missing(aid: str, current: Callable[[], str | None]) -> str | None:
    """A secret for the automation's CURRENT destination only when it has none (a save that set a
    new address). Decided inside the write lock, so it never replaces one a concurrent rotation
    just made for that same address."""
    return _mint(aid, None, current=current, only_if_missing=True)


def _mint(
    aid: str,
    destination: str | None,
    *,
    current: Callable[[], str | None] | None,
    only_if_missing: bool,
) -> str | None:
    if not _AID_RE.match(aid):
        raise ValueError("bad automation id")
    plaintext = _secrets.token_hex(32)
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with atomicjson.json_write_lock(p):
        if current is not None:
            destination = current()
        if destination is None:
            return None
        doc = atomicjson.read_json_doc(p)
        prev = _entry(doc, aid)
        if (
            only_if_missing
            and prev is not None
            and prev["destination"] == destination
            and template_secrets.decrypt(_aad(aid, prev["gen"], destination), prev["env"])
            is not None
        ):
            return None
        gen = (prev["gen"] if prev else 0) + 1
        try:
            env = template_secrets.encrypt(_aad(aid, gen, destination), plaintext)
        except template_secrets.SecretKeyUnavailable as e:
            raise SecretError(f"the signing secret could not be stored ({e})") from None
        doc[aid] = {"gen": gen, "destination": destination, "env": env, "at": time.time()}
        atomicjson.atomic_write_json(p, doc)
    return plaintext


def signing_secret(aid: str, destination: str) -> str | None:
    """The plaintext for THIS destination, or None (none made, another destination, unopenable)."""
    e = _entry(atomicjson.read_json_doc(path()), aid)
    if e is None or e["destination"] != destination:
        return None
    return template_secrets.decrypt(_aad(aid, e["gen"], destination), e["env"])


def drop(aid: str) -> None:
    """Forget an automation's secret (it was deleted). Best effort: a left entry opens nothing,
    since an automation id is never reused."""
    p = path()
    if not p.exists():
        return
    with atomicjson.json_write_lock(p):
        doc = atomicjson.read_json_doc(p)
        if aid in doc:
            del doc[aid]
            atomicjson.atomic_write_json(p, doc)
