"""One server-authored automation authority, from proposal input through byte one (#1019).

The owner and its durable generation are evidence, not routing hints. A proposal never adopts
today's owner to repair yesterday's authority. Read-only evaluation does not need this grant.
"""

from __future__ import annotations

import hashlib
import json

from . import missions, prefs


class AuthorityChanged(ValueError):
    """The original authority no longer holds; prepare a fresh proposal."""


def policy_revision(scope: str) -> str:
    cfg = prefs.get_automation_policy(scope)
    if cfg.get("revision") == "invalid":
        raise AuthorityChanged("automation authority is unreadable")
    # Also binds pre-cutover reads and hand-edited policy; no separate revision sidecar.
    values = {k: cfg[k] for k in prefs._policy_fields(scope) | {"revision"}}
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def capture(session_id: str, mission_id: str | None = None, *, path=None) -> dict:
    owner = missions.automation_ownership(session_id, path=path)
    if owner["mission_id"] != mission_id:
        raise AuthorityChanged("the session belongs to a different automation scope")
    scope = "mission" if mission_id else "session"
    return {"version": 1, "scope": scope, **owner, "policy_revision": policy_revision(scope)}


def capture_candidates(session_ids: list[str], mission_id: str | None = None) -> dict[str, dict]:
    """Original authority for one controller's candidate set, with bounded store reads."""
    if not session_ids:
        return {}
    scope = "mission" if mission_id else "session"
    revision = policy_revision(scope)
    owners = missions.automation_ownerships(session_ids)
    return {
        sid: {"version": 1, "scope": scope, **owner, "policy_revision": revision}
        for sid, owner in owners.items()
        if owner["mission_id"] == mission_id
    }


def scope_of(rec: dict) -> str:
    grant = rec.get("authority")
    if not isinstance(grant, dict) or grant.get("version") != 1:
        raise AuthorityChanged("this legacy action needs a fresh proposal")
    scope = grant.get("scope")
    if scope not in prefs.AUTOMATION_BLOCKS:
        raise AuthorityChanged("the action has no verified automation scope")
    return scope


def check(rec: dict, *, path=None) -> tuple[bool, str]:
    try:
        scope = scope_of(rec)
        original = rec["authority"]
        mission_id = original.get("mission_id")
        if (scope == "mission") != bool(mission_id) or mission_id != rec.get("mission_id"):
            raise AuthorityChanged("the action's mission provenance changed")
        current = capture(str(rec.get("session_id") or ""), mission_id, path=path)
        if original != current:
            raise AuthorityChanged(
                "ownership or automation policy changed; prepare a fresh proposal"
            )
    except Exception as e:  # ownership/policy stores must answer before any action is authorized
        return False, str(e) if isinstance(
            e, AuthorityChanged
        ) else "automation authority is unreadable"
    return True, ""


def append_operator_action(rec: dict) -> dict:
    """The explicit mission relay uses the same ownership fence as model proposals."""
    from . import orchestrator_ledger, session_input

    with session_input.mutation_fence():
        ok, why = check(rec)
        if not ok:
            raise AuthorityChanged(why)
        return orchestrator_ledger.append(rec)
