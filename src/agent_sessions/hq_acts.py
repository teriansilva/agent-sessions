"""What HQ did on its own (#2390, epic #2070 A1): one log the Commander reads back.

Every act (an operation HQ ran under the Commander's Authority setting, without a tap) writes two
JSON lines to ``<ask store>/acts.jsonl``, both keyed by the proposal id: one at the CLAIM, before
the route runs (``state: "deciding"``, fsynced), and one when it settles. A reader folds the rows
by id, the newest winning, so a repeated write is harmless.

**Truthful across crashes.** An id whose newest row is still ``deciding`` and that no task of this
process runs any more is resolved against its sidecar entry, the source of truth: ``done`` when
the route settled before a crash ate the second line, ``interrupted`` once ``ask_proposals.recover``
has looked, ``unknown`` when the entry is gone. Nothing is ever replayed from here.

**Bounded.** At most :data:`KEEP` acts are kept, none older than :data:`RETENTION_S`. Compaction
runs on a READ, under the log's lock, once the file holds more than :data:`COMPACT_AT` lines, so
the per-act write is an append and nothing more. Text fields pass ``ask_store.guard_text``; a row
whose text can't be guarded is written without it, never with it unguarded.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
from pathlib import Path

from . import ask_store
from .atomicjson import fsync_dir, json_write_lock

log = logging.getLogger(__name__)

KEEP = 500
COMPACT_AT = 1000
RETENTION_S = 30 * 86400
#: The most a read returns (the route's `limit`) and the `list_acts` tool's own cap.
READ_MAX = 100
TOOL_MAX = 30
TEXT_MAX = 200
SETTLED = frozenset(
    {"done", "partly_done", "failed", "stale", "interrupted", "unconfirmed", "refused", "unsettled"}
)


def path() -> Path:
    return ask_store.root() / "acts.jsonl"


def _guard(text: object) -> str:
    if not isinstance(text, str) or not text:
        return ""
    try:
        # The WHOLE text first: redaction must see a known secret entire, and only then is the
        # result cut (`guard_text` caps after redacting). A cut before it could keep a prefix.
        return ask_store.guard_text(text, TEXT_MAX)
    except Exception:  # noqa: BLE001 — redaction unavailable: the row keeps no text
        return ""


def append(row: dict) -> bool:
    """Append one row (the claim's or the settlement's). Returns whether it was written; never
    raises. The claim's row must land before anything runs (the caller fails closed on False);
    the settlement's cannot change what the act did. Blocking."""
    out = {
        "ts": float(row.get("ts") or time.time()),
        "pid": str(row.get("pid") or ""),
        "cid": str(row.get("cid") or ""),
        "turn_id": str(row.get("turn_id") or ""),
        "op": str(row.get("op") or "")[:64],
        "label": _guard(row.get("label")),
        "title": _guard(row.get("title")),
        "state": str(row.get("state") or "")[:32],
        "outcome": _guard(row.get("outcome")),
        "authority_rev": int(row.get("authority_rev") or 0),
        "preset": str(row.get("preset") or "")[:16],
        "tainted": bool(row.get("tainted")),
        "via": "call" if row.get("via") == "call" else "typed",
    }
    if not out["pid"]:
        return False
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        # The log's lock: a compaction's rewrite never drops a line appended while it runs.
        with json_write_lock(p):
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, (json.dumps(out, ensure_ascii=False) + "\n").encode())
                os.fsync(fd)
            finally:
                os.close(fd)
    except OSError as e:
        log.warning("the acts log could not be written (%s)", type(e).__name__)
        return False
    return True


def _lines(p: Path) -> list[dict]:
    rows: list[dict] = []
    with contextlib.suppress(FileNotFoundError):
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            with contextlib.suppress(ValueError):
                r = json.loads(line)
                if isinstance(r, dict) and isinstance(r.get("pid"), str) and r["pid"]:
                    rows.append(r)
    return rows


def _fold(rows: list[dict]) -> list[dict]:
    """One row per act, the newest of its lines, newest act first (by its first line's time)."""
    first: dict[str, float] = {}
    last: dict[str, dict] = {}
    for r in rows:
        first.setdefault(r["pid"], float(r.get("ts") or 0))
        last[r["pid"]] = r
    out = [{**last[pid], "ts": first[pid]} for pid in last]
    out.sort(key=lambda r: r["ts"], reverse=True)
    return out


def _resolve(row: dict) -> dict:
    """A ``deciding`` row no task here runs any more: the sidecar entry's state."""
    from . import ask_operations, ask_proposals

    if row.get("state") != "deciding" or ask_operations.running(row["pid"]):
        return row
    try:
        cid = row.get("cid") or ""
        entry = ask_proposals._row(cid, row["pid"]) if ask_store.valid_cid(cid) else None
        if entry is not None and entry.get("state") == "deciding":
            ask_proposals.recover(cid)  # a free decide lock: interrupted, never replayed
            entry = ask_proposals._row(cid, row["pid"])
    except Exception:  # noqa: BLE001
        entry = None
    if entry is None:
        return {**row, "state": "unknown", "outcome": ""}
    state = str(entry.get("state") or "unknown")
    said = entry.get("outcome") or entry.get("reason") or ""
    return {**row, "state": state, "outcome": _guard(said)}


def _compact(p: Path, now: float) -> None:
    with json_write_lock(p):
        rows = _lines(p)
        if len(rows) <= COMPACT_AT:
            return
        # One line per act (its newest, stamped with its first line's time), newest KEEP within
        # the retention, oldest first: the file shrinks to at most KEEP lines.
        kept = [r for r in _fold(rows) if now - float(r["ts"]) <= RETENTION_S][:KEEP]
        body = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in reversed(kept))
        tmp = p.with_name(p.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, body.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, p)
        fsync_dir(p.parent)


def recent(limit: int = 50, since: float | None = None) -> list[dict]:
    """The newest acts, folded and resolved, at most ``limit`` (≤ READ_MAX). Blocking."""
    p = path()
    now = time.time()
    rows = _lines(p)
    if len(rows) > COMPACT_AT:
        with contextlib.suppress(OSError):
            _compact(p, now)
            rows = _lines(p)
    folded = [r for r in _fold(rows) if now - float(r["ts"]) <= RETENTION_S]
    if since is not None:
        folded = [r for r in folded if float(r["ts"]) >= since]
    out = []
    for r in folded:
        if len(out) >= max(1, min(int(limit), READ_MAX)):
            break
        r = _resolve(r)
        if r.get("state") == "pending":
            continue  # refused at the effect: nothing ran, the card waits for the tap
        out.append(
            {
                "ts": r["ts"],
                "op": r.get("op") or "",
                "label": r.get("label") or "",
                "title": r.get("title") or "",
                "state": r.get("state") or "unknown",
                "outcome": r.get("outcome") or "",
                "via": r.get("via") or "typed",
                "conversation": r.get("cid") or "",
            }
        )
    return out
