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


def dismiss(session_key: str, fingerprint: str, now: float | None = None) -> None:
    """Record that ``session_key`` was dismissed while showing ``fingerprint``."""
    now = time.time() if now is None else now
    path = _path()
    with json_write_lock(path):
        doc = read_json_doc(path)
        rows = doc.get("dismissed") if isinstance(doc.get("dismissed"), dict) else {}
        rows = {k: v for k, v in rows.items() if isinstance(v, dict) and _fresh(v, now)}
        rows[session_key] = {"fingerprint": fingerprint, "ts": now}
        if len(rows) > MAX_ENTRIES:
            keep = sorted(rows.items(), key=lambda kv: float(kv[1].get("ts") or 0))[-MAX_ENTRIES:]
            rows = dict(keep)
        doc["dismissed"] = rows
        atomic_write_json(path, doc)


def suppressed(now: float | None = None) -> dict[str, str]:
    """``session_key -> fingerprint`` for every live dismissal. Read-lenient: a bad file is {}."""
    now = time.time() if now is None else now
    try:
        doc = read_json_doc(_path())
    except Exception:  # noqa: BLE001 — an unreadable file dismisses nothing
        return {}
    rows = doc.get("dismissed") if isinstance(doc, dict) else None
    out: dict[str, str] = {}
    for k, v in (rows or {}).items() if isinstance(rows, dict) else ():
        if isinstance(k, str) and isinstance(v, dict) and isinstance(v.get("fingerprint"), str):
            if _fresh(v, now):
                out[k] = v["fingerprint"]
    return out


def _fresh(row: dict, now: float) -> bool:
    try:
        return now - float(row.get("ts") or 0) < KEEP_S
    except (TypeError, ValueError, OverflowError):
        return False
