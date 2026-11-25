"""Optional HQ capture after a successful committed turn, never on the answer path (#2036).

The final JSONL `done` record IS the durable intent. A bounded reconciler recovers a crash before
enqueue, persists a validated candidate batch, then admits each with a source receipt in the
same notebook transaction. Restart/retry cannot duplicate or resurrect forgotten sources.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import sqlite3
import time

from . import (
    ai_providers,
    ask_store,
    hq_usage,
    memory,
    prompts,
    review,
    template_secrets,
)
from . import (
    memory_service as service,
)

log = logging.getLogger(__name__)
HQ = memory.HQ_SCOPE
RECONCILE_RECORDS = 50
CANDIDATES_MAX = 3
_UNBOUND = object()


def intent(question: str | None = None, *, expected=_UNBOUND) -> dict | None:
    if expected is None:
        return None
    try:
        cfg = service.capture_settings()
        if expected is None or (
            expected is not _UNBOUND
            and any(expected[k] != cfg[k] for k in ("enabled", "revision", "generation"))
        ):
            return None
        return (
            {
                **{k: cfg[k] for k in ("generation", "revision")},
                "digest": ask_store.question_digest(question),
            }
            if cfg["enabled"] and question is not None
            else None
        )
    except (
        OSError,
        sqlite3.Error,
        ValueError,
        memory.MemoryStoreError,
        template_secrets.SecretKeyUnavailable,
    ):
        return None


def _connection():
    return memory._connect(HQ)


def _enqueue_record(conn, cid, record):
    cap = record.get("memory_capture")
    tid = record.get("turn_id")
    if (
        record.get("type") != "status"
        or record.get("status") != "done"
        or not ask_store.valid_turn_id(tid)
        or not isinstance(cap, dict)
        or set(cap) != {"generation", "revision", "digest"}
        or any(type(cap[k]) is not int for k in ("generation", "revision"))
        or not isinstance(cap["digest"], str)
        or not ask_store._DIGEST_RE.fullmatch(cap["digest"])
    ):
        return
    conn.execute(
        "INSERT OR IGNORE INTO memory_capture_jobs(cid,tid,generation,revision,digest) "
        "VALUES(?,?,?,?,?)",
        (cid, tid, cap["generation"], cap["revision"], cap["digest"]),
    )


def enqueue_committed(cid: str, tid: str) -> None:
    """Accelerate a durable intent, never synthesize one. Failure is recovered by reconcile."""
    if not memory._exists(HQ) or not ask_store.exists(cid):
        return
    records = [r for r in ask_store.read(cid) if r.get("turn_id") == tid]
    with memory._scope_lock(HQ), contextlib.closing(_connection()) as conn, memory._txn(conn):
        for record in records:
            _enqueue_record(conn, cid, record)


def reconcile() -> None:
    if not memory._exists(HQ):
        return
    files = ask_store._conversation_files()
    with memory._scope_lock(HQ), contextlib.closing(_connection()) as conn:
        known = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT cid FROM memory_capture_jobs WHERE state NOT IN ('done','invalid')"
            )
        ]
        known += [
            r[0].partition("/")[0]
            for r in conn.execute(
                "SELECT DISTINCT source FROM memory_sources WHERE deleted=0 AND source "
                "LIKE 'ask_%/%'"
            )
        ]
    for cid in known:
        if ask_store.valid_cid(cid) and not ask_store.exists(cid):
            invalidate_source(cid)
    if not files:
        return
    with memory._scope_lock(HQ), contextlib.closing(_connection()) as conn:
        after = memory._meta(conn, "capture_scan_after", "")
    ids = sorted(cid for cid, _, _ in files)
    cid = next((c for c in ids if c > after), ids[0])
    # The turn lock orders deletion against admission and keeps a draft/running turn out. A
    # busy conversation is retried on the next bounded walk; no answer waits for this worker.
    with ask_store.turn_lock(cid) as free:
        if not free or not ask_store.exists(cid):
            return
        records = ask_store.read(cid)
        with memory._scope_lock(HQ), contextlib.closing(_connection()) as conn, memory._txn(conn):
            position = conn.execute(
                "SELECT position FROM memory_capture_cursors WHERE cid=?", (cid,)
            ).fetchone()
            start = position[0] if position else 0
            end = min(len(records), start + RECONCILE_RECORDS)
            for record in records[start:end]:
                _enqueue_record(conn, cid, record)
            conn.execute("INSERT OR REPLACE INTO memory_capture_cursors VALUES(?,?)", (cid, end))
            memory._set_meta(conn, "capture_scan_after", cid if end == len(records) else after)


def invalidate_source(cid: str) -> None:
    """Immediate source invalidation; retrieval also checks existence if cleanup was interrupted."""
    if not ask_store.valid_cid(cid) or not memory._exists(HQ):
        return
    with memory._fenced(), memory._scope_lock(HQ), contextlib.closing(_connection()) as conn:
        with memory._txn(conn):
            rows = conn.execute(
                "SELECT DISTINCT slug FROM memory_sources WHERE source LIKE ? AND deleted=0",
                (cid + "/%",),
            ).fetchall()
            conn.execute(
                "UPDATE memory_capture_jobs SET state='invalid',candidates='' WHERE cid=?", (cid,)
            )
            for row in rows:
                # Keep the existing deletion receipts/tombstones. publish happens below, once.
                conn.execute(
                    (
                        "INSERT OR REPLACE INTO receipts SELECT digest,'deleted',? FROM versions "
                        "WHERE slug=?"
                    ),
                    (time.time(), row[0]),
                )
                conn.execute("DELETE FROM notes WHERE slug=?", (row[0],))
                conn.execute("DELETE FROM versions WHERE slug=?", (row[0],))
        memory._render(HQ, conn)


def retains_source(cid: str) -> bool:
    """Admitted notes retain sources across routine expiry, never explicit deletion.

    Metadata only; fail closed as the conversation's other retention pins do. Sweep rechecks
    while holding the turn lock, which admission also uses. A pending job alone retains nothing.
    """
    if not memory._exists(HQ):
        return False
    try:
        with service.readonly(HQ) as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM memory_sources WHERE deleted=0 "
                    "AND source>=? AND source<? LIMIT 1",
                    (cid + "/", cid + "0"),
                ).fetchone()
                is not None
            )
    except (sqlite3.Error, OSError, memory.MemoryStoreError):
        return True


def _operator_text(cid: str, tid: str, digest: str) -> str:
    records = ask_store.read(cid)
    done = any(
        r.get("type") == "status" and r.get("turn_id") == tid and r.get("status") == "done"
        for r in records
    )
    users = [r for r in records if r.get("type") == "user" and r.get("turn_id") == tid]
    if not done or len(users) != 1:
        raise ValueError("source is not a completed operator turn")
    text = users[0].get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("source is empty")
    if users[0].get("qd") != digest or ask_store.question_digest(text) != digest:
        raise ValueError("source changed or was redacted")
    with memory._fenced():
        if (
            memory._withheld(text, template_secrets.redaction_values())
            or "[withheld" in text.lower()
        ):
            raise ValueError("source is withheld")
    return text


def _validate(payload: dict, text: str) -> list[dict]:
    items = payload.get("memories")
    if not isinstance(items, list) or len(items) > CANDIDATES_MAX:
        raise ValueError("invalid candidate batch")
    checked = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {"title", "body", "quote", "type"}:
            raise ValueError("invalid candidate")
        if any(not isinstance(v, str) for v in item.values()) or item["type"] not in memory.TYPES:
            raise ValueError("invalid candidate")
        if (
            not item["quote"].strip()
            or item["quote"] not in text
            or len(item["quote"]) > 2000
            or len(item["title"]) > 120
            or len(item["body"].encode()) > 3800
        ):
            raise ValueError("candidate not grounded in the operator")
        # Whole source and candidate are withheld, never credential-shaped fragments redacted
        # into a newly asserted fact. A question/hypothesis must be refused by the extractor.
        checked.append(item)
    return checked


def _next_job():
    if not memory._exists(HQ):
        return None
    with memory._scope_lock(HQ), contextlib.closing(_connection()) as conn:
        row = conn.execute(
            (
                "SELECT * FROM memory_capture_jobs WHERE state IN "
                "('queued','candidates') AND retry<=? ORDER BY rowid LIMIT 1"
            ),
            (time.time(),),
        ).fetchone()
        return dict(row) if row else None


def _current(job) -> bool:
    cfg = service.capture_settings()
    return (
        cfg["enabled"]
        and cfg["generation"] == job["generation"]
        and cfg["revision"] == job["revision"]
    )


def _state(job, state: str, *, candidates: str = "", retry: bool = False):
    with memory._scope_lock(HQ), contextlib.closing(_connection()) as conn:
        conn.execute(
            (
                "UPDATE memory_capture_jobs SET "
                "state=?,candidates=?,attempts=attempts+?,retry=? WHERE cid=? AND tid=?"
            ),
            (
                state,
                candidates,
                int(retry),
                time.time() + min(300, 15 * 2 ** min(5, job["attempts"])) if retry else 0,
                job["cid"],
                job["tid"],
            ),
        )


def _persist(job, items, text):
    with memory._fenced():
        fields = "\n".join(value for item in items for value in item.values())
        if memory._withheld(text + "\n" + fields, template_secrets.redaction_values()):
            raise ValueError("candidate is withheld")
        if not _current(job):
            raise ValueError("capture changed")
        _state(job, "candidates", candidates=json.dumps(items))


def _admit(job, items):
    cid, tid = job["cid"], job["tid"]
    with ask_store.turn_lock(cid) as free:
        if not free:
            return False
        if not ask_store.exists(cid) or not _current(job):
            _state(job, "invalid")
            return True
        text = _operator_text(cid, tid, job["digest"])
        for item in _validate({"memories": items}, text):
            service.submit(
                service.operator(),
                HQ,
                title=item["title"],
                body=item["quote"],
                kind=item["type"],
                origin=service.source_origin(cid, tid, item["quote"]),
                source=f"{cid}/{tid}",
                quote=item["quote"],
                source_kind="capture",
                expected_generation=job["generation"],
                capture_revision=job["revision"],
            )
        _state(job, "done")
        return True


async def _step() -> bool:
    await asyncio.to_thread(reconcile)
    job = await asyncio.to_thread(_next_job)
    if job is None:
        return False

    try:
        if not await asyncio.to_thread(_current, job) or not ask_store.exists(job["cid"]):
            await asyncio.to_thread(_state, job, "invalid")
            return True
        if job["state"] == "candidates":
            items = json.loads(job["candidates"])
        else:
            text = await asyncio.to_thread(_operator_text, job["cid"], job["tid"], job["digest"])
            with hq_usage.hq_call():
                payload = await review.complete_json(
                    [
                        {"role": "system", "content": prompts.effective("memory_capture")},
                        {"role": "user", "content": text},
                    ]
                )
            items = _validate(payload, text)
            await asyncio.to_thread(_persist, job, items, text)
        return await asyncio.to_thread(_admit, job, items)
    except (ValueError, ask_store.ConversationNotFound):
        await asyncio.to_thread(_state, job, "invalid")
        return True
    except memory.MemoryStoreError as exc:
        if exc.status == 409:
            await asyncio.to_thread(_state, job, "invalid")
            return True
        raise
    except review.ReviewError:
        await asyncio.to_thread(_state, job, job["state"], candidates=job["candidates"], retry=True)
        return False


@ai_providers.independent_model_work
async def step() -> bool:
    if not memory._exists(HQ):
        return False
    fd = os.open(
        memory._scope(HQ) / "capture.lock",
        os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return await _step()
    finally:
        os.close(fd)


async def run():
    if os.environ.get("AGENT_SESSIONS_MEMORY_LOOP", "1") == "0":
        return
    while True:
        delay = 15
        try:
            if await step():
                delay = 0.2
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a later pass retries durable work, never the answer
            log.warning("HQ memory capture could not advance", exc_info=True)
        await asyncio.sleep(delay)
