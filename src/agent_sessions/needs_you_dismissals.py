"""Dismissed NEEDS YOU rows (#1086 Phase 3): "not now, until this session's screen changes".

A dismissal is keyed to the session AND the screen the operator dismissed — the server reads the
screen fingerprint itself, never the client — so the row stays hidden exactly as long as the
session is still showing what the operator already looked at, and returns the moment it moves on
(a new question is a new reason to need you). It is persisted, so a restart does not bring a
dismissed row back.

Its own small file rather than a key inside the notifications store: that store rewrites its whole
document on every write (`notifications._write`), which would silently drop a foreign key. #1086
Phase 4's notification episodes build on this record.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .atomicjson import atomic_write_json, json_write_lock, read_json_doc

#: A dismissal older than this is dropped on the next write — it is a "not now", not a mute.
KEEP_S = 7 * 86400
MAX_ENTRIES = 500


def _path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_NEEDS_YOU_DISMISSED",
            str(Path.home() / ".config" / "agent-sessions" / "needs-you-dismissed.json"),
        )
    )


def dismiss(
    session_key: str,
    fingerprint: str,
    now: float | None = None,
    *,
    report_id: str | None = None,
    action_ids: list[str] | None = None,
) -> None:
    """Record that ``session_key`` was dismissed while showing ``fingerprint`` — and WHICH
    occurrence it dismissed: the agent's HQ report the row showed (#1882) and the pending
    decisions it carried. A screen dismissal hides only those, never a newer report or a newly
    actionable decision on the same unchanged screen."""
    now = time.time() if now is None else now
    path = _path()
    with json_write_lock(path):
        doc = read_json_doc(path)
        rows = doc.get("dismissed") if isinstance(doc.get("dismissed"), dict) else {}
        rows = {k: v for k, v in rows.items() if isinstance(v, dict) and _fresh(v, now)}
        rows[session_key] = {"fingerprint": fingerprint, "ts": now}
        if report_id:
            rows[session_key]["report_id"] = report_id
        if action_ids:
            rows[session_key]["action_ids"] = sorted({str(a) for a in action_ids if a})
        if len(rows) > MAX_ENTRIES:
            keep = sorted(rows.items(), key=lambda kv: float(kv[1].get("ts") or 0))[-MAX_ENTRIES:]
            rows = dict(keep)
        doc["dismissed"] = rows
        atomic_write_json(path, doc)
    # A dismissal ends the session's needs-you episode (#1086 Phase 4): retract it now.
    from . import needs_you_notify

    needs_you_notify.poke()


class DismissalsUnreadable(Exception):
    """The dismissal record exists but could not be read (#1086 Phase 4, Hermes 5231)."""


def suppressed_checked(now: float | None = None) -> dict[str, str]:
    """:func:`suppressed`, but a record that EXISTS and cannot be read or parsed raises
    :class:`DismissalsUnreadable` instead of reading as "nothing dismissed" — the notification
    sync must not re-announce a dismissed screen because a read failed. A missing file is a
    genuinely empty record. Writes are atomic replaces, so there is no torn state to wait out."""
    return {k: v["fingerprint"] for k, v in suppressed_detail_checked(now).items()}


def suppressed_detail_checked(now: float | None = None) -> dict[str, dict]:
    """``session_key -> {"fingerprint", "report_id", "action_ids"}`` (``report_id`` ``""`` when
    the row showed no report; ``action_ids`` the pending decisions it carried), strict like
    :func:`suppressed_checked` — ONE read for all of them."""
    now = time.time() if now is None else now
    path = _path()
    try:
        with path.open(encoding="utf-8") as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise DismissalsUnreadable(type(e).__name__) from e
    if not isinstance(doc, dict):
        raise DismissalsUnreadable("not a mapping")
    rows = doc.get("dismissed")
    if rows is None:
        return {}
    if not isinstance(rows, dict):
        raise DismissalsUnreadable("dismissed is not a mapping")
    return _detail(rows, now, strict=True)


def _malformed(k: object, v: object) -> bool:
    if (
        not isinstance(k, str)
        or not isinstance(v, dict)
        or not isinstance(v.get("fingerprint"), str)
    ):
        return True
    rid, aids = v.get("report_id"), v.get("action_ids")
    return (rid is not None and not isinstance(rid, str)) or (
        aids is not None
        and (not isinstance(aids, list) or not all(isinstance(a, str) for a in aids))
    )


def _detail(rows: dict, now: float, *, strict: bool = False) -> dict[str, dict]:
    """``strict``: a malformed entry raises (unknown is never "not dismissed" to a reader that
    retracts or announces on the answer, #1882); the lenient read skips it."""
    if strict and any(_malformed(k, v) for k, v in rows.items()):
        raise DismissalsUnreadable("a dismissal is malformed")
    out: dict[str, dict] = {}
    for k, v in rows.items():
        if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("fingerprint"), str):
            if _fresh(v, now):
                rid = v.get("report_id")
                aids = v.get("action_ids")
                out[k] = {
                    "fingerprint": v["fingerprint"],
                    "report_id": rid if isinstance(rid, str) else "",
                    "action_ids": frozenset(
                        a for a in (aids if isinstance(aids, list) else []) if isinstance(a, str)
                    ),
                }
    return out


def suppressed(now: float | None = None) -> dict[str, str]:
    """``session_key -> fingerprint`` for every live dismissal. Read-lenient: a bad file is {}."""
    return {k: v["fingerprint"] for k, v in suppressed_detail(now).items()}


def suppressed_detail(now: float | None = None) -> dict[str, dict]:
    """:func:`suppressed_detail_checked`, read-lenient: a bad file is {}."""
    now = time.time() if now is None else now
    try:
        doc = read_json_doc(_path())
    except Exception:  # noqa: BLE001 — an unreadable file dismisses nothing
        return {}
    rows = doc.get("dismissed") if isinstance(doc, dict) else None
    return _detail(rows, now) if isinstance(rows, dict) else {}


def _fresh(row: dict, now: float) -> bool:
    try:
        return now - float(row.get("ts") or 0) < KEEP_S
    except (TypeError, ValueError, OverflowError):
        return False
