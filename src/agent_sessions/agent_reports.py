"""What a running agent told HQ about itself (#1882, P1a of #1878): the agent-reports store.

A pty agent started with BattleLab's tools calls ``hq_report`` (`hq_report_mcp`), which reaches
the app over the report broker (`hq_report_broker`). The broker maps the caller's capability to
ITS launch — physical session key, engine, launch generation — and hands the report here. The
agent never names a session, so through the tool it reports only as itself (not against a
same-user process reading a sibling launch's token file: `docs/invariants/hq-reports.md`).

**One current report per physical session, plus a bounded history** (``HISTORY_MAX`` per
session, ``HISTORY_KEEP_S`` — enforced on every read and swept from idle sessions by the broker's
periodic pass, `sweep`), in ONE document written atomically under its lock. A newer report
replaces the current one; the old one goes to history.

**A report is untrusted agent text.** Every field is stripped of control and format characters
(bidi overrides, zero-widths), capped (longer is refused, never silently cut), and passed through
`ask_store.guard_text`: a field the boundary would change — a stored secret, a credential shape —
refuses the WHOLE report and nothing is stored. Its words never enter a notification row, a push,
a dedupe key or an episode key; Needs you names a report by its fixed kind label
(`notifications.KIND_REASON`).

**Rate limits per session:** one report per ``MIN_INTERVAL_S`` and ``PER_HOUR`` an hour.

**A report ENDS** (current → history, with the reason) on a newer report, an answer delivered to
that session (`orchestrator_ledger._settled`: a report's own answer ends only its report, any
other only a report filed before it was proposed; the needs-you sync re-ends one whose answer was
delivered), a dismissal, archive (`runtime_cleanup`), or its launch no longer live
(`hq_report_broker.reconcile`, a relaunch). All of them are server facts.

**Strict reads.** `current_checked` raises `StoreUnreadable` when the document exists but cannot
be read — the needs-you sync then skips (unknown is not "nobody"). A document that cannot be read
is never rewritten: every write starts from a strict read inside the lock.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path

from .atomicjson import atomic_write_json, json_write_lock

STATES = ("blocked", "needs_decision", "progress", "done")
#: The states that put a session in Needs you. `progress` / `done` never notify.
NEEDS_YOU_STATES = frozenset({"blocked", "needs_decision"})
#: The states the session row shows as one muted "agent says" line.
NOTE_STATES = frozenset({"progress", "done"})

SUMMARY_MAX = 280
QUESTION_MAX = 280
OPTION_MAX = 80
OPTIONS_MAX = 4

MIN_INTERVAL_S = 30.0
PER_HOUR = 20
HISTORY_MAX = 20
HISTORY_KEEP_S = 7 * 86400

#: Why a report ended — recorded in its history row, never shown as agent text.
END_REASONS = frozenset({"replaced", "answered", "dismissed", "archived", "launch_ended"})

#: Refusal codes: the only words a refusal carries (never input echoed back).
REFUSALS = frozenset(
    {
        "bad_request",
        "too_long",
        "rate_limited",
        "credential",
        "store_unavailable",
        "launch_ended",
        "launch_unconfirmed",
    }
)


class Refused(Exception):
    def __init__(self, code: str) -> None:
        assert code in REFUSALS
        super().__init__(code)
        self.code = code


class StoreUnreadable(Exception):
    """The reports document exists but cannot be read. It is never rewritten."""


def store_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_AGENT_REPORTS",
            str(Path.home() / ".config" / "agent-sessions" / "agent-reports.json"),
        )
    )


# ---- reading ---------------------------------------------------------------------------------


def _read_strict(path: Path) -> dict:
    """The document, ``{}`` when it does not exist; `StoreUnreadable` for anything else that is
    not a well-formed document. Writes are atomic replaces, so there is no torn state to wait
    out."""
    try:
        with path.open(encoding="utf-8") as fh:
            doc = json.load(fh)
    except (FileNotFoundError, NotADirectoryError):
        return {}  # ABSENT: nothing has been reported
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise StoreUnreadable(type(e).__name__) from e
    if not isinstance(doc, dict):
        raise StoreUnreadable("not a mapping")
    sessions = doc.get("sessions")
    if sessions is not None and not isinstance(sessions, dict):
        raise StoreUnreadable("sessions is not a mapping")
    # MALFORMED IS UNKNOWN (#1882, Hermes on #1884): an entry this module can't read is never
    # silently dropped — a strict reader would take it for "no report", and every writer starts
    # from this read and would delete it.
    for key, entry in (sessions or {}).items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise StoreUnreadable("a session entry is malformed")
        cur = entry.get("current")
        if cur is not None and not _valid_report(cur):
            raise StoreUnreadable("a current report is malformed")
        hist = entry.get("history")
        if hist is not None and (
            not isinstance(hist, list) or not all(_valid_report(r) for r in hist)
        ):
            raise StoreUnreadable("a report history is malformed")
    return doc


def _sessions(doc: dict) -> dict[str, dict]:
    raw = doc.get("sessions")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)}


def _valid_report(rep: object) -> bool:
    return (
        isinstance(rep, dict)
        and isinstance(rep.get("id"), str)
        and rep.get("state") in STATES
        and isinstance(rep.get("summary"), str)
        and isinstance(rep.get("at"), int | float)
    )


def current_checked() -> dict[str, dict]:
    """``physical key -> current report`` from ONE strict read (raises `StoreUnreadable`)."""
    out: dict[str, dict] = {}
    for phys, entry in _sessions(_read_strict(store_path())).items():
        rep = entry.get("current")
        if _valid_report(rep):
            out[phys] = rep  # type: ignore[assignment]
    return out


def current_map() -> dict[str, dict]:
    """`current_checked`, fail-soft: an unreadable store reads as no reports (list routes)."""
    try:
        return current_checked()
    except StoreUnreadable:
        return {}


def current(phys_key: str) -> dict | None:
    return current_map().get(phys_key)


def latest_notes() -> dict[str, dict]:
    """``physical key -> the newest progress/done report`` (current or history, within the
    history window), for the session row's one "agent says" line. Fail-soft."""
    try:
        doc = _read_strict(store_path())
    except StoreUnreadable:
        return {}
    now = time.time()
    out: dict[str, dict] = {}
    for phys, entry in _sessions(doc).items():
        cands = [entry.get("current"), *(entry.get("history") or [])]
        best = None
        for rep in cands:
            if not _valid_report(rep) or rep["state"] not in NOTE_STATES:
                continue
            if now - float(rep["at"]) > HISTORY_KEEP_S:
                continue
            if best is None or float(rep["at"]) > float(best["at"]):
                best = rep
        if best is not None:
            out[phys] = best
    return out


def _kept(rep: dict, now: float) -> bool:
    """Still inside the history bound (``HISTORY_KEEP_S`` from when it was filed)."""
    return now - float(rep["at"]) <= HISTORY_KEEP_S


def history_checked(phys_key: str, *, now: float | None = None) -> list[dict]:
    """`history`, STRICT: raises `StoreUnreadable` rather than reading as "no history". For a
    caller that would conclude something from absence (`routes.pulse`'s refusal wording)."""
    t = time.time() if now is None else now
    entry = _sessions(_read_strict(store_path())).get(phys_key) or {}
    rows = [r for r in entry.get("history") or [] if _valid_report(r) and _kept(r, t)]
    return sorted(rows, key=lambda r: float(r["at"]), reverse=True)


def history(phys_key: str, *, now: float | None = None) -> list[dict]:
    """The session's ended reports within the history bound, newest first — an expired one is
    never returned, whether or not `sweep` has removed it yet. Fail-soft."""
    t = time.time() if now is None else now
    try:
        entry = _sessions(_read_strict(store_path())).get(phys_key) or {}
    except StoreUnreadable:
        return []
    rows = [r for r in entry.get("history") or [] if _valid_report(r) and _kept(r, t)]
    return sorted(rows, key=lambda r: float(r["at"]), reverse=True)


def sweep(now: float | None = None) -> int:
    """Remove ended reports past the history bound from EVERY session, idle ones included (a
    session's own writes prune only it), so agent text never outlives the bound on disk. Called
    from the broker's periodic pass. A plain read decides whether anything expired; only then is
    the fenced rewrite taken. Returns how many rows went. Raises `StoreUnreadable` / `Busy`."""
    t = time.time() if now is None else now
    path = store_path()

    def expired(doc: dict) -> int:
        return sum(
            1
            for entry in _sessions(doc).values()
            for r in entry.get("history") or []
            if not (_valid_report(r) and _kept(r, t))
        )

    if not expired(_read_strict(path)):
        return 0
    with _fenced(path, fenced=False):
        doc = _read_strict(path)
        gone = expired(doc)
        if not gone:
            return 0
        sessions = {k: _prune(dict(v), t) for k, v in _sessions(doc).items()}
        _write(path, doc, sessions)
    return gone


def public(rep: dict) -> dict:
    """The fields a surface may show. Every text field was guarded when it was filed."""
    out = {
        "id": rep["id"],
        "state": rep["state"],
        "summary": rep["summary"],
        "at": float(rep["at"]),
    }
    if rep["state"] == "needs_decision":
        out["question"] = str(rep.get("question") or "")
        opts = rep.get("options")
        out["options"] = [o for o in opts if isinstance(o, str)] if isinstance(opts, list) else []
    return out


# ---- filing ----------------------------------------------------------------------------------


def clean(value: object) -> str:
    """Plain one-line text: whitespace runs collapse to one space, control (Cc) and format (Cf:
    bidi overrides, zero-widths) characters are dropped, the ends are trimmed."""
    if not isinstance(value, str):
        return ""
    out = []
    for ch in value:
        if ch.isspace():
            out.append(" ")
            continue
        if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co", "Cn"):
            continue
        out.append(ch)
    return " ".join("".join(out).split())


def _field(value: object, cap: int) -> str:
    text = clean(value)
    if len(text) > cap:
        raise Refused("too_long")
    return text


def validate(args: object) -> dict:
    """The report an agent asked to file, normalized, or `Refused`. No guard yet (`file`)."""
    if not isinstance(args, dict) or set(args) - {"state", "summary", "question", "options"}:
        raise Refused("bad_request")
    state = args.get("state")
    if state not in STATES:
        raise Refused("bad_request")
    summary = _field(args.get("summary"), SUMMARY_MAX)
    if not summary:
        raise Refused("bad_request")
    rep: dict = {"state": state, "summary": summary}
    question = args.get("question")
    options = args.get("options")
    if state != "needs_decision":
        if question not in (None, "") or options not in (None, []):
            raise Refused("bad_request")
        return rep
    # A decision the Commander can MAKE: a question and 1–4 options, each a button on the card.
    # Without them the agent waits for a choice nobody can tap — that is `blocked`.
    rep["question"] = _field(question, QUESTION_MAX)
    if not rep["question"]:
        raise Refused("bad_request")
    if not isinstance(options, list) or not 1 <= len(options) <= OPTIONS_MAX:
        raise Refused("bad_request")
    cleaned: list[str] = []
    for o in options:
        text = _field(o, OPTION_MAX)
        if not text or text in cleaned:
            raise Refused("bad_request")
        cleaned.append(text)
    rep["options"] = cleaned
    return rep


def _guarded(rep: dict) -> None:
    """Every field through the store boundary: a field it would change refuses the WHOLE report
    (`credential`), and redaction that cannot run refuses too (`store_unavailable`)."""
    from . import ask_store

    texts = [rep["summary"], rep.get("question") or "", *(rep.get("options") or [])]
    for text in texts:
        if not text:
            continue
        try:
            if ask_store.guard_text(text, len(text)) != text:
                raise Refused("credential")
        except ask_store.StoreRefused:
            raise Refused("store_unavailable") from None
        except Refused:
            raise
        except Exception:  # noqa: BLE001 — redaction that cannot run authorizes nothing
            raise Refused("store_unavailable") from None


def _prune(entry: dict, now: float) -> dict:
    hist = [
        r
        for r in entry.get("history") or []
        if _valid_report(r) and now - float(r["at"]) <= HISTORY_KEEP_S
    ]
    hist.sort(key=lambda r: float(r["at"]))
    entry["history"] = hist[-HISTORY_MAX:]
    entry["recent"] = [
        t
        for t in entry.get("recent") or []
        if isinstance(t, int | float) and not isinstance(t, bool) and now - t < 3600
    ]
    return entry


def _answer_evidence(rep: dict, reason: str) -> dict:
    """What an ENDED report keeps about whether it was answered (#1882, Hermes on #1884): the
    ledger rows that prove it are pinned only while the report is current, so the history row
    itself must carry it for as long as it is kept. ``answered_by_delivery``: an ordinary
    answer/relay was delivered to the session after the report was filed; ``answer_unknown``: the
    ledger could not be read to say. A report ended BY its answer, or holding a reservation,
    already says so (``ended_by``, ``answer``)."""
    if reason == "answered" or answer_mark(rep):
        return {}
    from . import orchestrator_ledger

    phys = str(rep.get("phys_key") or "")
    if not phys:
        return {"answer_unknown": True}
    try:
        if orchestrator_ledger.answered_after(phys, float(rep["at"])):
            return {"answered_by_delivery": True}
    except Exception:  # noqa: BLE001 — unknown is recorded as unknown, never as "unanswered"
        return {"answer_unknown": True}
    return {}


def _end_into_history(entry: dict, reason: str, now: float) -> dict | None:
    rep = entry.get("current")
    entry["current"] = None
    if not _valid_report(rep):
        return None
    ended = {**rep, **_answer_evidence(rep, reason), "ended_at": now, "ended_by": reason}
    entry.setdefault("history", []).append(ended)
    return ended


def _write(path: Path, doc: dict, sessions: dict[str, dict]) -> None:
    keep = {
        k: v
        for k, v in sessions.items()
        if _valid_report(v.get("current")) or v.get("history") or v.get("recent")
    }
    doc["version"] = 1
    doc["sessions"] = keep
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, doc)


class Busy(Exception):
    """The write fence could not be taken in time; nothing was changed. Retry."""


@contextlib.contextmanager
def _fenced(path: Path, *, fenced: bool):
    """A report mutation, ordered against every PTY write (#1882, Hermes on the issue).

    An answer's authority (`authority_state`) is re-read in the actuator's in-fence fingerprint
    right before byte one. That re-read only orders a report change if the change commits under
    the same cross-process fence, as template edits do (`session_input.mutation_fence`): then a
    replacement or dismissal lands either before the re-read (the answer settles `stale`, no
    bytes) or after the bytes. Fence first, then this store's lock, the order every fenced store
    takes. ``fenced=True`` is for a caller that already holds the fence (a launch inside
    `session_input.launch_fence`): the fence is a fresh-fd `flock`, so taking it twice in one
    flow would wait out its own budget.

    Every report write holds `template_secrets.registration_fence` between the two, the order
    `template_vars` takes (mutation fence → registration fence → store lock): a secret becomes
    known strictly before a report's guard or strictly after its commit, never in between."""
    from . import session_input, template_secrets

    if fenced:
        with template_secrets.registration_fence(), json_write_lock(path):
            yield
        return
    try:
        with (
            session_input.mutation_fence(),
            template_secrets.registration_fence(),
            json_write_lock(path),
        ):
            yield
    except session_input.AuthorityFenceBusy:
        raise Busy("the write fence is busy; try again") from None


def file(
    args: object,
    *,
    session_key: str,
    phys_key: str,
    engine: str,
    generation: str,
    now: float | None = None,
    still_current: Callable[[], bool | None] | None = None,
) -> dict:
    """File one report for the launch the broker resolved. Returns the stored report; raises
    `Refused`. Nothing is written for a refused report.

    ``still_current`` re-asks, UNDER the fence, whether the capability is still this session's
    live launch: a revocation commits under the same fence (`hq_report_broker._revoke`), so one
    that landed after the broker's check refuses here (`launch_ended`) instead of filing a
    report for a launch that no longer exists."""
    rep = validate(args)
    t = time.time() if now is None else now
    path = store_path()
    try:
        with _fenced(path, fenced=False):
            # The guard INSIDE the registration fence, so the set it redacts against is the set
            # known when the report commits (`_fenced`).
            _guarded(rep)
            if still_current is not None:
                verdict = still_current()
                if verdict is None:
                    raise Refused("launch_unconfirmed")  # unknown: refused, nothing stored
                if not verdict:
                    raise Refused("launch_ended")
            doc = _read_strict(path)
            sessions = _sessions(doc)
            entry = _prune(dict(sessions.get(phys_key) or {}), t)
            recent = entry["recent"]
            if recent and t - max(recent) < MIN_INTERVAL_S:
                raise Refused("rate_limited")
            if len(recent) >= PER_HOUR:
                raise Refused("rate_limited")
            _end_into_history(entry, "replaced", t)
            stored = {
                **rep,
                "id": secrets.token_hex(8),
                "at": t,
                "session_key": session_key,
                "phys_key": phys_key,
                "engine": engine,
                "generation": generation,
            }
            entry["current"] = stored
            entry["recent"] = [*recent, t]
            entry = _prune(entry, t)
            sessions[phys_key] = entry
            _write(path, doc, sessions)
    except (StoreUnreadable, OSError, Busy):
        raise Refused("store_unavailable") from None
    _changed(stored)
    return stored


def _changed(rep: dict | None) -> None:
    """A report started or ended: the needs-you sync looks again now (one producer, D3), and a
    mission-held session's supervisor is woken (#1214's wake path). Best-effort."""
    import contextlib

    with contextlib.suppress(Exception):
        from . import needs_you_notify

        needs_you_notify.poke()
    if rep is None:
        return
    with contextlib.suppress(Exception):
        from . import mission_supervisor_loop

        mission_supervisor_loop.wake_for_report(str(rep.get("session_key") or ""), rep["phys_key"])


def end(
    phys_key: str,
    reason: str,
    *,
    report_id: str | None = None,
    generation: str | None = None,
    before: float | None = None,
    now: float | None = None,
    fenced: bool = False,
) -> dict | None:
    """End the session's current report (into history). With ``report_id`` / ``generation``,
    only when the current report still matches them; with ``before``, only when it was filed
    before that time (an answer proposed before the agent asked is not its answer). Returns the
    ended report, or None.
    Raises `StoreUnreadable` (never rewrites an unreadable store) and `Busy` (the write fence
    could not be taken; nothing changed). ``fenced``: the caller already holds the fence."""
    assert reason in END_REASONS
    t = time.time() if now is None else now
    path = store_path()
    with _fenced(path, fenced=fenced):
        doc = _read_strict(path)
        sessions = _sessions(doc)
        entry = sessions.get(phys_key)
        if not entry or not _valid_report(entry.get("current")):
            return None
        cur = entry["current"]
        if report_id is not None and cur.get("id") != report_id:
            return None
        if generation is not None and cur.get("generation") != generation:
            return None
        if before is not None and not float(cur["at"]) < before:
            return None
        entry = dict(entry)
        ended = _end_into_history(entry, reason, t)
        sessions[phys_key] = _prune(entry, t)
        _write(path, doc, sessions)
    _changed(None)
    return ended


def answered_or_maybe(rep: dict) -> str | None:
    """For an ENDED report: ``"spent"`` when its record shows an answer went (ended by it, a
    reservation, an ordinary delivery after it), ``"uncertain"`` when it could not be told, else
    None — the only case in which a refusal may say "nothing was sent"."""
    if rep.get("ended_by") == "answered" or answer_mark(rep) or rep.get("answered_by_delivery"):
        return "spent"
    if rep.get("answer_unknown"):
        return "uncertain"
    return None


def answer_mark(rep: object) -> str | None:
    """The action holding this report's ONE answer (`mark_answered`), or None."""
    a = rep.get("answer") if isinstance(rep, dict) else None
    aid = a.get("action_id") if isinstance(a, dict) else None
    return aid if isinstance(aid, str) and aid else None


def mark_answered(
    phys_key: str, report_id: str, action_id: str, *, now: float | None = None
) -> str:
    """Reserve the current report's ONE answer for ``action_id`` (#1882), on the REPORT record —
    durable for the report's whole life, whatever the ledger compacts away. ``"ok"``; ``"spent"``
    when another action holds it (its bytes may be on the pty); ``"gone"`` when ``report_id`` is
    no longer the session's current report. Atomic under the fence and this store's lock, so two
    taps reserve it once. Raises `StoreUnreadable` / `Busy`. Never call it inside the write fence
    (a delivery's write window): it takes `session_input.mutation_fence` itself."""
    t = time.time() if now is None else now
    path = store_path()
    with _fenced(path, fenced=False):
        doc = _read_strict(path)
        sessions = _sessions(doc)
        entry = sessions.get(phys_key)
        cur = entry.get("current") if entry else None
        if not _valid_report(cur) or cur.get("id") != report_id:
            return "gone"
        held = answer_mark(cur)
        if held is not None:
            return "ok" if held == action_id else "spent"
        entry = dict(entry)
        entry["current"] = {**cur, "answer": {"action_id": action_id, "at": t}}
        sessions[phys_key] = entry
        _write(path, doc, sessions)
    return "ok"


def clear_answer(phys_key: str, report_id: str, action_id: str) -> bool:
    """Give the report's answer back: ``action_id`` ended before byte one, so a fresh tap may
    answer. Only where ``action_id`` holds ``report_id``'s reservation — on the current report,
    or on its ENDED row in history (the report ended while the action was in flight), so that
    row's evidence says what really happened: nothing was typed. Raises like `end`."""
    path = store_path()
    with _fenced(path, fenced=False):
        doc = _read_strict(path)
        sessions = _sessions(doc)
        entry = sessions.get(phys_key)
        if not entry:
            return False
        entry = dict(entry)
        cur = entry.get("current")
        if _valid_report(cur) and cur.get("id") == report_id and answer_mark(cur) == action_id:
            entry["current"] = {k: v for k, v in cur.items() if k != "answer"}
        else:
            hist = list(entry.get("history") or [])
            hit = next(
                (
                    i
                    for i, r in enumerate(hist)
                    if r.get("id") == report_id and answer_mark(r) == action_id
                ),
                None,
            )
            if hit is None:
                return False
            hist[hit] = {k: v for k, v in hist[hit].items() if k != "answer"}
            entry["history"] = hist
        sessions[phys_key] = entry
        _write(path, doc, sessions)
    _changed(None)
    return True


def clear_answer_quietly(phys_key: str, report_id: str, action_id: str) -> bool:
    """`clear_answer`, best-effort: a failure leaves the report reserved (it refuses, never
    double-types) and is logged."""
    try:
        return clear_answer(phys_key, report_id, action_id)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).warning(
            "agent reports: releasing a report's answer failed", exc_info=True
        )
        return False


def end_quietly(phys_key: str, reason: str, **kw) -> dict | None:
    """`end`, best-effort: teardown and settlement hooks never fail on this store."""
    try:
        return end(phys_key, reason, **kw)
    except Exception:  # noqa: BLE001 — the next sync or reconcile retries
        import logging

        logging.getLogger(__name__).warning("agent reports: ending a report failed", exc_info=True)
        return None


# ---- delivery authority ----------------------------------------------------------------------


def authority_state(phys_key: str) -> tuple:
    """What a report answer's authority rests on, read NOW: the current report's id and launch
    generation, and whether that generation is still the session's live launch. Folded into the
    actuator's in-fence fingerprint. An unreadable store is its own distinct value."""
    from . import hq_report_broker

    try:
        cur = current_checked().get(phys_key)
    except StoreUnreadable:
        return ("unreadable",)
    if cur is None:
        return ("none",)
    gen = str(cur.get("generation") or "")
    return (
        cur["id"],
        gen,
        hq_report_broker.generation_current(phys_key, gen),
        answer_mark(cur),
    )


#: An answer refused because ANOTHER answer to the report holds its one answer (#1882).
ANSWER_SPENT = "an answer to this report was already sent, or may have been"
#: An answer refused because whether one was already sent can't be read (#1882): fail closed.
ANSWER_UNKNOWN = "the decision ledger can't be read, so an earlier answer can't be ruled out"


def authority(
    phys_key: str, report_id: str, generation: str, action_id: str | None = None
) -> tuple[bool, str]:
    """May an answer bound to ``report_id`` / ``generation`` still be typed into ``phys_key``?
    With ``action_id``, also refused when another action holds the report's one answer.
    Fails closed."""
    from . import hq_report_broker

    if not report_id or not generation:
        return False, "the answer is not bound to a report"
    try:
        cur = current_checked().get(phys_key)
    except StoreUnreadable:
        return False, "the agent-reports store could not be read, so the report is unverifiable"
    if cur is None or cur.get("id") != report_id:
        return False, "the agent's report was replaced, answered or dismissed"
    held = answer_mark(cur)
    if action_id is not None and held is not None and held != action_id:
        return False, ANSWER_SPENT
    if cur.get("generation") != generation:
        return False, "the session was restarted since it reported"
    if not hq_report_broker.generation_current(phys_key, generation):
        return False, "the session was restarted since it reported"
    return True, ""
