"""A create's client provenance — `choice: "automatic" | "explicit"` (#1728).

The New session form preselects its family's default client (API first) and says, with the create,
whether the client it sends IS that automatic default or the operator's explicit choice:

* ``automatic`` — refused when the family's base default has moved since the form showed it
  (readiness changed between display and save): HTTP 409 with the new default, or ws close 4412.
  Nothing launches; the form shows the new default and the operator starts again.
* ``explicit`` — honoured while that client can start, even when the base default moved: an
  intentional Terminal pick stays a Terminal pick. The create path's own readiness checks apply.
* absent — an older client: exactly the create it always was.

Not an authorization: it narrows nothing an operator may start, it only keeps an unattended-looking
"default" from launching something the form did not show.
"""

from __future__ import annotations

import asyncio

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from . import engines
from .engines import family

#: The terminal socket's code for a refused stale automatic default (HTTP 412's sibling): never
#: retried, and nothing was launched.
WS_STALE_DEFAULT = 4412


def stale_for(engine: str, choice: object) -> dict | None:
    """``None`` to proceed, else the new default. BLOCKING (readiness probes): off the loop.
    Raises ``ValueError`` for a choice that is not one of `family.CHOICES`."""
    if choice is None or choice == "explicit":
        return None
    if choice not in family.CHOICES:
        raise ValueError("choice must be automatic or explicit")
    return engines.stale_automatic(engine)


def _already_created(engine: str, operation_id: object) -> bool:
    """Whether the create ``operation_id`` names was already recorded (its id IS the session id).
    BLOCKING. Any doubt reads as "not recorded": the check then runs as for a new create.

    For a native API client the AUTHORITATIVE record is the native session record, written under
    the session lock BEFORE the conversation journal exists (`native_runtime._create_sync`). A
    create interrupted between the two has a record and no journal; its retry must still reach
    the create's own recovery path, so the record — not the journal — decides. Whether the retry
    is the SAME request (cwd, model, mode, …) is the create's own check, which conflicts otherwise.
    Any other engine is asked through its store (`resolve_session`)."""
    if not isinstance(operation_id, str) or not operation_id:
        return False
    try:
        m = engines.manifest_of(engine)
        if m is not None and m.runtime == "api":
            from . import native_state

            record = native_state.read_session(operation_id)
            return record is not None and record.get("session_key") == f"{engine}:{operation_id}"
        return engines.resolve_session(engine, operation_id) is not None
    except Exception:  # noqa: BLE001 — an unreadable store is not proof of a replay
        return False


def _stale_for_create(engine: str, choice: object, operation_id: object) -> dict | None:
    if choice not in (None, "explicit") and choice in family.CHOICES:
        # Idempotent replay first: a retried create whose response was lost (the SPA reuses the
        # operation id) answers with the session it already made, never "nothing was started".
        if _already_created(engine, operation_id):
            return None
    return stale_for(engine, choice)


async def refuse_stale(
    engine: str, choice: object, operation_id: object = None
) -> JSONResponse | None:
    """The structured create's check: ``None`` to proceed, else the 409 to return. A create whose
    ``operation_id`` is already recorded is a replay and is never refused here."""
    try:
        stale = await asyncio.to_thread(_stale_for_create, engine, choice, operation_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if stale is None:
        return None
    return JSONResponse({"detail": family.stale_words(stale), "default": stale}, status_code=409)
