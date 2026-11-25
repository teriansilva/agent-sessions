"""Is this session archived? The one reader every start path, the sweep and the freeze ask (#2064).

An archived session is frozen: no runtime, and nothing may start or feed it until it is
unarchived. The launch gate, the archived-but-running sweep and the archive writers must agree on
what "archived" means, so they all ask :func:`effective_state`:

* the **sidecar override first**, read under the sidecar's own lock through the checked reader
  (`metadata.archive_state_held`), over every identity of the session in precedence order —
  logical (real) id first, then the key as given, then the physical (placeholder) id — exactly the
  precedence discovery uses. A damaged row is ``unreadable``, never ``active``;
* only where the sidecar is silent, the **engine's own store**: its ``archive_state`` when it has
  one (claude moves the transcript), else its per-session ``lookup``. Never a full ``scan()`` —
  this runs on every send.

``unreadable`` is a real third answer. Launchers fail **closed** on it (nothing is lost: the
session was not running); the sweep fails **open** (it never stops a runtime on uncertainty).
"""

from __future__ import annotations

from . import engines, metadata

#: THE refusal every start path gives an archived session. The SPA matches it (and re-reads the
#: row) to switch a pane to its archived state — keep `web/src/lib/archived.ts` in step.
ARCHIVED_DETAIL = "this session is archived — unarchive it to continue"
UNREADABLE_DETAIL = "this session's archive state could not be read — nothing was started"

ARCHIVED = "archived"
ACTIVE = "active"
UNREADABLE = "unreadable"


class Refused(Exception):
    """A start path must not proceed. ``status`` is 409 (archived) or 503 (unreadable)."""

    def __init__(self, state: str):
        self.state = state
        self.status = 409 if state == ARCHIVED else 503
        self.detail = ARCHIVED_DETAIL if state == ARCHIVED else UNREADABLE_DETAIL
        super().__init__(self.detail)


def _identities(key: str) -> list[str]:
    out: list[str] = []
    for fn in (engines.logical_key, None, engines.physical_key):
        try:
            k = key if fn is None else fn(key)
        except Exception:  # noqa: BLE001, S112 — an unreadable alias store adds no identity
            continue
        if k and k not in out:
            out.append(k)
    return out


def _engine_state(prov, native: str) -> str:
    if getattr(prov, "archive_state", None) is not None:
        state = engines.archive_state(prov, native)
    else:
        lookup = getattr(prov, "lookup", None)
        if lookup is None:
            state = engines.archive_state(prov, native)
        else:
            try:
                row = lookup(native)
            except Exception:  # noqa: BLE001 — a store that raised has told us it cannot answer
                return UNREADABLE
            return ARCHIVED if row is not None and getattr(row, "archived", False) else ACTIVE
    return {"archived": ARCHIVED, "not-archived": ACTIVE}.get(state, UNREADABLE)


def _is_new_placeholder(native: str) -> bool:
    from .engines import base

    return bool(base._NEW_PLACEHOLDER_RE.fullmatch(native or ""))


def effective_state(prov, native: str, *, new_session: bool = False) -> str:
    """``archived`` · ``active`` · ``unreadable`` for ``prov``'s session ``native``. Blocking.

    ``new_session`` (a terminal LAUNCH with ``new=1``) is a CALLER'S claim, never proof that the id
    is fresh: the sidecar override is read for every identity exactly as for any session, and so
    is the engine's store. The one allowance is a store that cannot answer for a ``new-…``
    placeholder — a server-recognised shape no engine store holds — which reads ``active`` instead
    of ``unreadable`` (a placeholder's provider may refuse to look it up at all)."""
    key = f"{prov.engine_id}:{native}"
    with metadata.archive_state_held(*_identities(key)) as side:
        sidecar = side
    if sidecar == "archived":
        return ARCHIVED
    if sidecar == "active":
        return ACTIVE
    if sidecar != "unset":
        return UNREADABLE
    state = _engine_state(prov, native)
    if state == UNREADABLE and new_session and _is_new_placeholder(native):
        return ACTIVE
    return state


def check(prov, native: str, *, new_session: bool = False) -> None:
    """Raise `Refused` unless the session is definitely active. Blocking."""
    state = effective_state(prov, native, new_session=new_session)
    if state != ACTIVE:
        raise Refused(state)


def check_key(session_key: str) -> None:
    """`check` by ``engine:native``. An unparseable key is left to the caller's own 404."""
    try:
        prov, native = engines.parse_key(session_key)
    except Exception:  # noqa: BLE001
        return
    check(prov, native)


__all__ = [
    "ACTIVE",
    "ARCHIVED",
    "ARCHIVED_DETAIL",
    "UNREADABLE",
    "UNREADABLE_DETAIL",
    "Refused",
    "check",
    "check_key",
    "effective_state",
]
