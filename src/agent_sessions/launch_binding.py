"""Which session did this unattended launch become? (#989)

An engine that mints its own id reveals it only after its first turn, so a dispatch launches under
a `<engine>:new-<uuid>` placeholder and has to find the real id afterwards. The interactive path
already does that by diffing the engine's store: the one new id in the cwd is taken to be ours.
With an operator watching that is fine. With nobody watching it is not, and two orderings show why:

* an interactive session in the same cwd writes its id first, while ours is still pending — there
  is exactly one new candidate, and it is somebody else's;
* that session was given the SAME text, a paste of the mission instructions — so matching the
  brief is correlation, not attribution, and it binds the wrong session just as confidently.

## The nonce

Each attempt mints 128 random bits and delivers them as the brief's last line. The value is
written to the dispatch record before the launch, but it is never exposed to another session's
input before this launch's paste lands — so a session whose first user turn carries it is a
session that received this launch's paste. That is the `nonce` proof, and `bind_by_nonce` is the
reusable implementation of it for any provider whose transcript records the pasted text.

The nonce is a discriminator, not a secret: it grants nothing, and knowing it lets nobody do
anything except be mistaken for the session that received it — which requires receiving it.

## What this module does not decide

Whether a given engine's transcript actually preserves the pasted line is a fact about that engine
and is proved per engine against the real CLI (#989 Phase 2). Nothing here enables an engine.
"""

from __future__ import annotations

import re
import secrets
from pathlib import Path

from . import transcript
from .engines import base

NONCE_BYTES = 16
_NONCE_RE = re.compile(r"^[0-9a-f]{32}$")


def mint_nonce() -> str:
    """A fresh per-attempt nonce: 32 lowercase hex characters."""
    return secrets.token_hex(NONCE_BYTES)


def nonce_line(nonce: str) -> str:
    """The exact line a late-id launch appends. Refuses a malformed nonce rather than typing one."""
    if not isinstance(nonce, str) or not _NONCE_RE.match(nonce):
        raise ValueError("malformed attempt nonce")
    return f"[mission attempt {nonce}]"


def envelope(brief: str, nonce: str) -> str:
    """The text a late-id launch types: the brief, a blank line, then the nonce line.

    The caller passes the result through `handoff.sanitize_seed` as ONE text, so the byte cap
    covers the nonce too and a brief with no room left for it is refused rather than truncated.
    """
    return f"{brief}\n\n{nonce_line(nonce)}"


def _first_user_text(engine: str, native: str, home: Path) -> str | None:
    """The first user text turn of one session, or None when it has none yet.

    Raises when the engine has no transcript adapter, or when its adapter cannot read the session.
    """
    adapter = transcript.adapter_for(engine)
    if adapter is None:
        raise LookupError(f"{engine} registers no transcript adapter")
    for turn in adapter(native, home) or []:
        if turn.kind == "text" and turn.role == "user" and (turn.text or "").strip():
            return turn.text
    return None


def bind_by_nonce(prov, launch: base.LaunchContext, *, home: Path | None = None) -> base.Binding:
    """`bind_session` by the nonce proof. Read-only, never raises, publishes nothing.

    Candidates are the ids new in the pinned cwd since the pre-launch snapshot. One proves itself by
    carrying this attempt's nonce line in its first user turn.

    * exactly one proven, and every candidate could be read → `bound`;
    * none proven → `pending` — including a session whose first turn is the same brief without
      this nonce;
    * more than one proven → `ambiguous`; the dispatcher fails rather than picking;
    * the store, or any candidate's transcript, could not be read → `unreadable`. A candidate that
      could not be read is one that might have contradicted a positive, so it defeats a unique
      match as well as an absence — the rule `start_evidence` follows for the same reason.
    """
    engine = launch.engine
    if launch.snapshot is None:
        return base.Binding(
            base.BIND_UNREADABLE, detail="there is no pre-launch snapshot to compare against"
        )
    try:
        line = nonce_line(launch.nonce)
    except ValueError:
        return base.Binding(base.BIND_UNREADABLE, detail="the launch carries no valid nonce")
    try:
        current = prov.snapshot_session_ids(launch.cwd)
    except Exception as e:  # noqa: BLE001 — a store that raises has told us it cannot answer
        return base.Binding(
            base.BIND_UNREADABLE, detail=f"{engine}'s store could not be read ({type(e).__name__})"
        )
    if current is None:
        return base.Binding(base.BIND_UNREADABLE, detail=f"{engine}'s store could not be read")
    root = home if home is not None else Path.home()
    proven: list[str] = []
    unreadable = 0
    for native in sorted(set(current) - set(launch.snapshot)):
        if not prov.id_pattern.match(str(native)):
            continue
        try:
            text = _first_user_text(engine, str(native), root)
        except Exception:  # noqa: BLE001 — one unreadable candidate is counted, never skipped
            unreadable += 1
            continue
        if text is not None and line in text:
            proven.append(str(native))
    if len(proven) > 1:
        return base.Binding(
            base.BIND_AMBIGUOUS,
            detail=f"{len(proven)} sessions carry this attempt's nonce",
        )
    if unreadable:
        return base.Binding(
            base.BIND_UNREADABLE,
            detail=f"{unreadable} new session(s) in the folder could not be read",
        )
    if proven:
        return base.Binding(base.BIND_BOUND, native=proven[0], proof=base.PROOF_NONCE)
    return base.Binding(base.BIND_PENDING)
