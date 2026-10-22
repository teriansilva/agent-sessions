"""Ask's durable conversations (#1443 Phase 1).

The Ask sidebar's conversation used to live in the browser only. An orchestrator whose answers,
proposals and follow-up updates vanish on a reload cannot keep an audit trail, so each
conversation is now an append-only JSONL file on the server:

    <root>/<cid>.jsonl            the records, one JSON object per LF-terminated line
    <root>/proposals/<cid>.json   Phase 3's session proposals (``ask_proposals`` writes them)
    <root>/follows/<cid>.json     Phase 4's follows (``ask_follow`` writes them, through here)
    <root>/.locks/<cid>.lock      the turn lock: held for as long as a turn runs
    <root>/.locks/<cid>.proposals.lock   held across every read-modify-write of the sidecar
    <root>/.locks/<cid>.decide.lock      held for as long as a launch from this conversation runs
    <root>/.locks/<cid>.follows.lock     held across every read-modify-write of the follows

``root`` is ``AGENT_SESSIONS_ASK_DIR`` (default ``~/.config/agent-sessions/ask``), kept 0700 with
0600 files. Its parent is the app's config dir (often 0775 by umask), so the chain is not held to
``privatedir``'s rule — the chat store's and the memory store's stance.

**The store boundary.** :func:`append` is the only writer, and every record passes it:

* Free text — the operator's messages, the answers, match reasons and titles, failure details and
  tool arguments — first has every stored or recently typed template secret redacted (the
  redactor ``review._post_chat`` applies; again here because an answer can quote what it was
  shown), and then, if what is left shows ANY credential trigger (``template_suggest``'s
  ``credential_shaped``), the whole field is replaced by :data:`WITHHELD`. Withheld, never
  partially redacted (``template-suggest.md``: redacting needs to know where a value ends).
  Redaction that cannot run refuses the write (:class:`StoreRefused`): nothing unscrubbed lands.
* Structural fields (ids, keys, states, counts) are shape-checked; a record that fails is a bug
  and raises rather than being repaired.
* **Raw tool output never reaches this module.** A ``tool`` record carries the tool's name, its
  validated arguments and result METADATA (counts, the keys and mission ids returned, a length, an
  error code). Screen tails and transcript excerpts exist only in the running turn's memory.

**Retention** (:func:`sweep`): 30 days since a conversation last changed, and at most 50
conversations. A conversation's JSONL, proposal sidecar, follow sidecar and lock file go together.
A conversation is PINNED — kept past both limits — while a turn is running in it or while its
proposal sidecar holds a ``deciding`` / ``creating`` proposal (or cannot be read): retention never
deletes a record recovery still needs to tell whether an effect happened.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from . import hq_contacts, missions, template_secrets, template_suggest

log = logging.getLogger(__name__)

CID_RE = re.compile(r"^ask_[0-9a-f]{32}$")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: A session key's SHAPE (`<engine>:<native id>`). The roster is not consulted: a key whose engine
#: was removed is still a fact about what an answer named.
_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}:[A-Za-z0-9._:@-]{1,200}$")
_CALL_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_TOOL_NAME_RE = re.compile(r"^[a-z_]{1,40}$")
_ERROR_CODE_RE = re.compile(r"^[a-z_]{1,40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

#: What a field that looked like a credential is stored as.
WITHHELD = "[withheld: looked like a credential]"

RETENTION_DAYS = 30
MAX_CONVERSATIONS = 50
#: Turns one conversation may hold; past it the operator starts a new one (honest, not a cut).
MAX_TURNS = 50
#: The most of one conversation file read back. Above the worst case by construction: a turn is
#: at most a question and an answer (TEXT_MAX each), 8+8 matches, two status records and
#: 6 rounds × 8 tool records (ARG_MAX of arguments and META_IDS_MAX ids each). The worst case is
#: measured, not estimated, by `test_the_worst_case_conversation_fits_the_read_cap`.
READ_MAX_BYTES = 16 * 1024 * 1024
#: The head read for the conversation list: enough for the header and the first question.
_HEAD_BYTES = 64 * 1024

# Per-field caps, applied AFTER redaction and the credential check.
TEXT_MAX = 4_000
TITLE_MAX = 120
WHY_MAX = 200
DETAIL_MAX = 400
ARG_MAX = 300
#: Arguments kept per tool record — no read tool takes more than two.
ARGS_MAX_ITEMS = 2
LIST_TITLE_MAX = 80
MATCHES_MAX = 8
META_IDS_MAX = 20

#: Proposal states that pin a conversation past retention (Phase 3 writes them).
PINNING_PROPOSAL_STATES = frozenset({"deciding", "creating"})

TURN_STATUSES = frozenset({"pending", "done", "failed"})
RECORD_TYPES = frozenset(
    {"conversation", "user", "assistant", "tool", "status", "question", "proposal", "update"}
)

# Asking back (#1443 Phase 2): a question card's bounds.
QUESTION_MAX = 300
OPTION_MAX = 120
OPTIONS_MAX = 4
#: A nudge's text — the orchestrator's own `answer` cap, so the ledger never cuts what was shown.
NUDGE_MAX = 800
#: A session proposal's brief (#1443 Phase 3a), well under the seed cap so the attempt's own
#: identifying line always fits beside it.
BRIEF_MAX = 2_000
_ACTION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
#: An HQ operation (#1559): a catalog id, its risk class, and the caps of what the card shows.
_OP_RE = re.compile(r"^[a-z]{1,20}\.[a-z_]{1,40}$")
OPERATION_RISKS = frozenset({"reversible", "destructive", "external"})
OPERATION_STATES = frozenset(
    {
        "pending",
        "deciding",
        "done",
        "partly_done",
        "failed",
        "stale",
        "dismissed",
        "interrupted",
        "unconfirmed",
    }
)
OP_VERB_MAX = 40
#: What an operation card may ask the OPERATOR for (never the model): a folder they pick.
OPERATION_INPUTS = frozenset({"folder"})
OP_SUMMARY_MAX = 400
_OP_FIELD_RE = re.compile(r"^[a-z_]{1,40}$")
OP_FIELDS_MAX = 8
PROPOSAL_KINDS = frozenset({"nudge", "session", "operation"})
# Follows (#1443 Phase 4): what a followed session's update says, as a kind — the words are the
# client's, authored, so no model text is ever shown as an update.
UPDATE_KINDS = frozenset({"finished", "needs_you", "ended", "not_followed"})
#: Why a conversation follows a session: Ask STARTED it, or a nudge Ask proposed was DELIVERED to it
#: (#1517).
FOLLOW_VIAS = frozenset({"start", "nudge"})
#: Sessions one conversation follows at once, and entries kept (an unfollowed one is remembered so
#: the same delivered nudge never re-follows it).
MAX_FOLLOWS = 10
MAX_FOLLOW_ROWS = 50
#: Update records one conversation may hold; past it its follows end, so the size bound holds.
MAX_UPDATES = 300
#: Ids a conversation has followed FOR (a started proposal) — never
#: forgotten, so a replayed reason never re-follows after Unfollow. Bounded by construction: a
#: conversation holds at most MAX_TURNS × 3 proposals of either kind.
MAX_CONSUMED = MAX_TURNS * 3


class StoreRefused(Exception):
    """A write the store boundary will not make (redaction unavailable)."""


class ConversationNotFound(Exception):
    pass


def root() -> Path:
    raw = os.environ.get("AGENT_SESSIONS_ASK_DIR")
    return Path(raw) if raw else Path.home() / ".config" / "agent-sessions" / "ask"


def valid_cid(value: object) -> bool:
    return isinstance(value, str) and CID_RE.fullmatch(value) is not None


def valid_turn_id(value: object) -> bool:
    return isinstance(value, str) and _UUID_RE.fullmatch(value) is not None


def new_cid() -> str:
    return "ask_" + uuid.uuid4().hex


def _path(cid: str) -> Path:
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    return root() / f"{cid}.jsonl"


def _proposals_path(cid: str) -> Path:
    return root() / "proposals" / f"{cid}.json"


def _follows_path(cid: str) -> Path:
    return root() / "follows" / f"{cid}.json"


def _lock_path(cid: str) -> Path:
    return root() / ".locks" / f"{cid}.lock"


def _proposals_lock_path(cid: str) -> Path:
    return root() / ".locks" / f"{cid}.proposals.lock"


def _follows_lock_path(cid: str) -> Path:
    return root() / ".locks" / f"{cid}.follows.lock"


def _decide_lock_path(cid: str) -> Path:
    return root() / ".locks" / f"{cid}.decide.lock"


def _ensure_dir(path: Path) -> None:
    """A real directory of this user, kept 0700."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid():
        raise StoreRefused(f"{path.name}: not a directory of this user")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)


def _ensure_root() -> None:
    _ensure_dir(root())
    _ensure_dir(root() / ".locks")


# ---- the store boundary ------------------------------------------------------------------------


def _mask_ids(text: str) -> str:
    """Mission ids are identifiers, not secrets: a 32-hex run behind ``msn_`` would otherwise read
    as a random-looking credential and withhold an ordinary answer that names a mission."""
    return re.sub(r"msn_[0-9a-f]{32}", "msn_id", text)


def guard_text(value: object, cap: int, *, secrets: list[str] | None = None) -> str:
    """One persisted text field, through the boundary: redact stored/typed secrets, withhold the
    whole field if it still looks like a credential, then cap."""
    if not isinstance(value, str):
        return ""
    try:
        text = template_secrets.redact_text(value, secrets)
    except template_secrets.RedactionUnavailable as e:
        raise StoreRefused(f"{e} — nothing was stored") from None
    if template_suggest.credential_shaped(_mask_ids(text)):
        return WITHHELD
    return text[:cap]


def question_digest(text: str) -> str:
    """What binds a ``turn_id`` to the question it was sent with: an HMAC of the ORIGINAL text
    under the template-secret store's key — never the stored text, which redaction and the withhold
    rule can make identical for two different questions. Fails closed (StoreRefused)."""
    try:
        return template_secrets.keyed_digest("ask-question", text)
    except template_secrets.SecretKeyUnavailable as e:
        raise StoreRefused(f"{e} — nothing was stored") from None


def _turn_id(rec: dict) -> str:
    tid = rec.get("turn_id")
    if not valid_turn_id(tid):
        raise ValueError("malformed turn id")
    return tid  # type: ignore[return-value]


def _key_list(values: object, *, missions_: bool = False) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for v in values[:META_IDS_MAX]:
        ok = (
            isinstance(v, str) and missions.MISSION_ID_RE.match(v) is not None
            if missions_
            else isinstance(v, str) and _KEY_RE.match(v) is not None
        )
        if not ok:
            raise ValueError("malformed id in tool metadata")
        out.append(v)
    return out


def _admit(rec: dict, secrets: list[str]) -> dict:
    """The record as it may be written, or ValueError for a record no caller should produce."""
    kind = rec.get("type")
    if kind not in RECORD_TYPES:
        raise ValueError(f"unknown record type {kind!r}")
    ts = rec.get("ts")
    out: dict = {"type": kind, "ts": float(ts) if isinstance(ts, int | float) else time.time()}

    def text(value: object, cap: int) -> str:
        return guard_text(value, cap, secrets=secrets)

    if kind == "conversation":
        cid = rec.get("id")
        if not valid_cid(cid):
            raise ValueError("malformed conversation id")
        out["id"] = cid
    elif kind == "user":
        out["turn_id"] = _turn_id(rec)
        out["text"] = text(rec.get("text"), TEXT_MAX)
        qd = rec.get("qd")
        if not (isinstance(qd, str) and _DIGEST_RE.match(qd)):
            raise ValueError("a question needs its digest")
        out["qd"] = qd
        answers = rec.get("answers")
        if answers is not None:
            if not valid_turn_id(answers):
                raise ValueError("malformed question id")
            out["answers"] = answers
        # Who answers this turn (#1525): a known HQ contact id or "off", resolved by the route.
        # Stored on the turn so an earlier answer keeps its speaker after the operator switches.
        contact = rec.get("contact")
        if contact is not None:
            if not isinstance(contact, str) or contact not in (*hq_contacts.IDS, hq_contacts.OFF):
                raise ValueError("unknown HQ contact")
            out["contact"] = contact
    elif kind == "question":
        out["turn_id"] = _turn_id(rec)
        qid = rec.get("question_id")
        if not valid_turn_id(qid):
            raise ValueError("malformed question id")
        out["question_id"] = qid
        out["question"] = text(rec.get("question"), QUESTION_MAX)
        options = rec.get("options") or []
        if not isinstance(options, list) or len(options) > OPTIONS_MAX:
            raise ValueError("malformed question options")
        out["options"] = [text(o, OPTION_MAX) for o in options]
        out["allow_text"] = rec.get("allow_text") is True
    elif kind == "proposal":
        out["turn_id"] = _turn_id(rec)
        aid, key, pkind = rec.get("action_id"), rec.get("key"), rec.get("kind")
        if not (isinstance(aid, str) and _ACTION_ID_RE.match(aid)):
            raise ValueError("malformed action id")
        if pkind not in PROPOSAL_KINDS:
            raise ValueError("unknown proposal kind")
        if pkind == "nudge":
            if not (isinstance(key, str) and _KEY_RE.match(key)):
                raise ValueError("malformed proposal session key")
            out.update(action_id=aid, key=key, kind=pkind)
            out["title"] = text(rec.get("title"), TITLE_MAX)
            out["text"] = text(rec.get("text"), NUDGE_MAX)
        elif pkind == "operation":
            # An operation HQ proposed (#1559): the card's server-authored wording. What a tap
            # runs is the sidecar's entry, bound to this card by `binding` (`ask_operations`).
            op, risk, bind = rec.get("op"), rec.get("risk"), rec.get("binding")
            if not (isinstance(op, str) and _OP_RE.match(op)):
                raise ValueError("malformed operation id")
            if risk not in OPERATION_RISKS:
                raise ValueError("unknown operation risk")
            if not (isinstance(bind, str) and _DIGEST_RE.match(bind)):
                raise ValueError("an operation card needs its binding")
            out.update(action_id=aid, kind=pkind, op=op, risk=risk, binding=bind)
            out["title"] = text(rec.get("title"), TITLE_MAX)
            out["label"] = text(rec.get("label"), TITLE_MAX)
            out["verb"] = text(rec.get("verb"), OP_VERB_MAX)
            out["text"] = text(rec.get("text"), OP_SUMMARY_MAX)
            needs = rec.get("needs") or []
            if not isinstance(needs, list) or not set(needs) <= OPERATION_INPUTS:
                raise ValueError("unknown operation input")
            out["needs"] = list(needs)
        else:
            # A session to start: the project's name, the agent's name and the brief, for the
            # card. What a tap launches is the sidecar's entry (``ask_proposals``), never this.
            out.update(action_id=aid, kind=pkind)
            out["title"] = text(rec.get("title"), TITLE_MAX)
            out["agent"] = text(rec.get("agent"), TITLE_MAX)
            out["text"] = text(rec.get("text"), BRIEF_MAX)
    elif kind == "status":
        out["turn_id"] = _turn_id(rec)
        status = rec.get("status")
        if status not in TURN_STATUSES:
            raise ValueError("unknown turn status")
        out["status"] = status
        if rec.get("detail"):
            out["detail"] = text(rec.get("detail"), DETAIL_MAX)
        code = rec.get("code")
        if code is not None:
            if not (isinstance(code, str) and _ERROR_CODE_RE.match(code)):
                raise ValueError("malformed status code")
            out["code"] = code
    elif kind == "assistant":
        out["turn_id"] = _turn_id(rec)
        out["text"] = text(rec.get("text"), TEXT_MAX)
        matches: list[dict] = []
        for m in (rec.get("matches") or [])[:MATCHES_MAX]:
            if not (isinstance(m, dict) and isinstance(m.get("id"), str)):
                raise ValueError("malformed match")
            if not _KEY_RE.match(m["id"]):
                raise ValueError("malformed match id")
            matches.append(
                {
                    "id": m["id"],
                    "title": text(m.get("title"), TITLE_MAX),
                    "why": text(m.get("why"), WHY_MAX),
                }
            )
        out["matches"] = matches
        mission_matches: list[dict] = []
        for m in (rec.get("mission_matches") or [])[:MATCHES_MAX]:
            mid = m.get("id") if isinstance(m, dict) else None
            if not (isinstance(mid, str) and missions.MISSION_ID_RE.match(mid)):
                raise ValueError("malformed mission match")
            state = m.get("state")
            mission_matches.append(
                {
                    "id": mid,
                    "title": text(m.get("title"), TITLE_MAX),
                    "state": state if state in missions.STATES else "",
                    "why": text(m.get("why"), WHY_MAX),
                }
            )
        out["mission_matches"] = mission_matches
    elif kind == "update":
        # A followed session changed (#1443 Phase 4): an AUTHORED line, never model text.
        key, ukind = rec.get("key"), rec.get("kind")
        if not (isinstance(key, str) and _KEY_RE.match(key)):
            raise ValueError("malformed update session key")
        if ukind not in UPDATE_KINDS:
            raise ValueError("unknown update kind")
        eid = rec.get("event_id")
        if not (isinstance(eid, str) and _ACTION_ID_RE.match(eid)):
            raise ValueError("an update needs its event id")
        out.update(key=key, kind=ukind, event_id=eid)
        out["title"] = text(rec.get("title"), TITLE_MAX)
    elif kind == "tool":
        out["turn_id"] = _turn_id(rec)
        cid = rec.get("call_id")
        name = rec.get("name")
        if not (isinstance(cid, str) and _CALL_ID_RE.match(cid)):
            raise ValueError("malformed call id")
        if not (isinstance(name, str) and _TOOL_NAME_RE.match(name)):
            raise ValueError("malformed tool name")
        out["call_id"], out["name"] = cid, name
        args: dict = {}
        raw_args = rec.get("args") if isinstance(rec.get("args"), dict) else {}
        for k, v in list(raw_args.items())[:ARGS_MAX_ITEMS]:
            if not (isinstance(k, str) and _TOOL_NAME_RE.match(k)):
                raise ValueError("malformed tool argument name")
            # Strings only, and every one through the guard: a number or a bool is model output
            # too (a numeric secret echoed as an argument), and no read tool takes one.
            if isinstance(v, str):
                args[k] = text(v, ARG_MAX)
        out["args"] = args
        meta_in = rec.get("meta") if isinstance(rec.get("meta"), dict) else {}
        meta: dict = {}
        for k in ("count", "length", "rounds"):
            v = meta_in.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                meta[k] = v
        if meta_in.get("keys"):
            meta["keys"] = _key_list(meta_in.get("keys"))
        if meta_in.get("missions"):
            meta["missions"] = _key_list(meta_in.get("missions"), missions_=True)
        err = meta_in.get("error")
        if err is not None:
            if not (isinstance(err, str) and _ERROR_CODE_RE.match(err)):
                raise ValueError("malformed tool error code")
            meta["error"] = err
        out["meta"] = meta
    return out


def _records_bytes(records: list[dict]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode(
        "utf-8", "replace"
    )


def _write_all(fd: int, data: bytes) -> None:
    size = os.fstat(fd).st_size
    # A torn tail (a crash mid-record) is terminated first, so the next record is not glued onto
    # it and lost with it; the torn line stays behind as one the reader skips.
    if size and os.pread(fd, 1, size - 1) != b"\n":
        data = b"\n" + data
    view = memoryview(data)
    while view:
        n = os.write(fd, view)
        if not n:
            raise OSError("conversation append made no progress")
        view = view[n:]
    os.fsync(fd)


def create(cid: str | None = None) -> str:
    """A new, empty conversation (just its header). Returns its id."""
    _ensure_root()
    cid = cid or new_cid()
    path = _path(cid)
    header = _admit({"type": "conversation", "id": cid}, [])
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        _write_all(fd, _records_bytes([header]))
    finally:
        os.close(fd)
    _sync_dir(root())
    return cid


def _sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append(cid: str, *records: dict) -> list[dict]:
    """Append records through the store boundary, durably and under the file's own lock. Returns
    the records exactly as written. Raises ConversationNotFound, StoreRefused or ValueError —
    in every case before a byte is written."""
    if not records:
        return []
    # Snapshot, admission and write under ONE hold of the registration fence: a secret registered
    # concurrently is known either before the snapshot (redacted here) or only after the write.
    with template_secrets.registration_fence():
        try:
            secrets = template_secrets.redaction_values()
        except template_secrets.RedactionUnavailable as e:
            raise StoreRefused(f"{e} — nothing was stored") from None
        admitted = [_admit(r, secrets) for r in records]
        try:
            fd = os.open(_path(cid), os.O_RDWR | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise ConversationNotFound(cid) from None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            _write_all(fd, _records_bytes(admitted))
        finally:
            os.close(fd)
    return admitted


def read(cid: str) -> list[dict]:
    """Every readable record, in order. A torn or malformed line is skipped, never fatal."""
    try:
        fd = os.open(_path(cid), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ConversationNotFound(cid) from None
    try:
        if os.fstat(fd).st_size > READ_MAX_BYTES:
            # Unreachable by construction (MAX_TURNS of worst-case turns fit); if it ever happens
            # the conversation says so rather than silently losing its newest turns.
            raise StoreRefused("this conversation is too large to read — start a new one")
        data = os.read(fd, READ_MAX_BYTES)
    finally:
        os.close(fd)
    return _parse(data)


def _parse(data: bytes) -> list[dict]:
    out: list[dict] = []
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("type") in RECORD_TYPES:
            out.append(rec)
    return out


def exists(cid: str) -> bool:
    return valid_cid(cid) and _path(cid).is_file()


# ---- the turn lock -----------------------------------------------------------------------------


def try_turn_lock(cid: str) -> int | None:
    """The conversation's turn lock, non-blocking: an fd to hold (and close to release), or None
    when another process — or another turn in this one — already holds it."""
    _ensure_root()
    fd = os.open(_lock_path(cid), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


@contextlib.contextmanager
def turn_lock(cid: str) -> Iterator[bool]:
    """``try_turn_lock`` as a context: yields whether it was acquired."""
    fd = try_turn_lock(cid)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)


def _open_lock(path: Path) -> int:
    _ensure_root()
    return os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)


@contextlib.contextmanager
def proposals_lock(cid: str) -> Iterator[None]:
    """The proposal sidecar's lock, BLOCKING and short: held across one read-modify-write of
    ``proposals/<cid>.json``, and by retention across its last look and the delete. Lock order:
    the turn lock, then the decide lock, then this one."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    fd = _open_lock(_proposals_lock_path(cid))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def try_decide_lock(cid: str) -> int | None:
    """Held for as long as a launch from this conversation runs (``ask_proposals``), non-blocking:
    an fd to hold and close, or None while another launch — in this process or another — holds
    it. A ``deciding`` proposal whose lock is free is one nobody is still deciding."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    fd = _open_lock(_decide_lock_path(cid))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


# ---- the conversation list ---------------------------------------------------------------------


def _head(path: Path) -> list[dict]:
    fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        data = os.read(fd, _HEAD_BYTES)
    finally:
        os.close(fd)
    # The last line of a partial head may be cut; `_parse` skips it.
    return _parse(data)


def _conversation_files() -> list[tuple[str, Path, float]]:
    """``(cid, path, mtime)`` for every conversation, newest-changed first."""
    base = root()
    try:
        names = os.listdir(base)
    except FileNotFoundError:
        return []
    out: list[tuple[str, Path, float]] = []
    for name in names:
        if not name.endswith(".jsonl"):
            continue
        cid = name[: -len(".jsonl")]
        if not valid_cid(cid):
            continue
        path = base / name
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(st.st_mode):
            out.append((cid, path, st.st_mtime))
    out.sort(key=lambda t: (-t[2], t[0]))
    return out


def summary(cid: str, path: Path, mtime: float) -> dict:
    """What the switcher lists: the id, a title (the first question, already through the store
    boundary when it was written) and when it was created and last changed."""
    created = mtime
    title = ""
    try:
        head = _head(path)
    except OSError:
        head = []
    for rec in head:
        if rec.get("type") == "conversation" and isinstance(rec.get("ts"), int | float):
            created = float(rec["ts"])
        if rec.get("type") == "user" and isinstance(rec.get("text"), str):
            title = " ".join(rec["text"].split())[:LIST_TITLE_MAX]
            break
    return {"id": cid, "title": title, "created_at": created, "updated_at": mtime}


def list_conversations() -> list[dict]:
    return [summary(cid, path, mtime) for cid, path, mtime in _conversation_files()]


# ---- the session-proposal sidecar (#1443 Phase 3a) ---------------------------------------------

#: A session proposal's life. ``pending`` waits for a tap; ``deciding`` is a launch in flight; every
#: other state is settled and never left again — an uncertain launch is never retried.
SESSION_PROPOSAL_STATES = frozenset(
    {
        "pending",
        "deciding",
        "started",
        "unconfirmed",
        "failed",
        "stale",
        "dismissed",
        "interrupted",
    }
)
#: Proposals one conversation may hold — the most its turns can make.
MAX_SESSION_PROPOSALS = MAX_TURNS * 3
NOTICES = frozenset({"", "default_changed", "busy"})
_ENGINE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
PATH_MAX = 4_096
#: The reason a pending brief becomes ``stale`` when a secret it contains is registered later.
BRIEF_NOW_SECRET = "the brief now contains a secret, so it will not be sent — ask again"  # noqa: S105 — a message, not a credential


class SidecarUnreadable(Exception):
    """The proposal sidecar exists and cannot be read: nothing in it can be decided."""


class Withheld(Exception):
    """A new proposal whose text the boundary would change: it is refused, never stored altered —
    a redacted brief would be a different launch from the one shown."""


def ensure_proposals_dir() -> Path:
    _ensure_root()
    d = root() / "proposals"
    _ensure_dir(d)
    return d


def read_proposals(cid: str) -> list[dict]:
    """The sidecar's entries, in order; ``[]`` when there is none. SidecarUnreadable otherwise —
    never a partial list, so nothing is decided on a guess."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    try:
        fd = os.open(_proposals_path(cid), os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return []
    except OSError as e:
        raise SidecarUnreadable(type(e).__name__) from None
    try:
        raw = b""
        while chunk := os.read(fd, 1 << 16):
            raw += chunk
            if len(raw) > READ_MAX_BYTES:
                raise SidecarUnreadable("too large")
    finally:
        os.close(fd)
    try:
        data = json.loads(raw)
    except ValueError:
        raise SidecarUnreadable("not JSON") from None
    rows = data.get("proposals") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
        raise SidecarUnreadable("malformed")
    return rows


def _admit_entry(e: dict, secrets: list[str], *, fresh: bool) -> dict:
    """One sidecar entry as it may be written; ValueError for a shape no caller should produce."""
    pid, tid = e.get("id"), e.get("turn_id")
    if not (isinstance(pid, str) and _ACTION_ID_RE.match(pid)):
        raise ValueError("malformed proposal id")
    if not valid_turn_id(tid):
        raise ValueError("malformed turn id")
    if e.get("kind") == "operation":
        return _admit_operation(e, pid, tid, secrets, fresh=fresh)
    if e.get("kind") != "session":
        raise ValueError("unknown proposal kind")
    state = e.get("state")
    if state not in SESSION_PROPOSAL_STATES:
        raise ValueError("unknown proposal state")
    engine, project_id, cwd = e.get("engine"), e.get("project_id"), e.get("cwd")
    if not (isinstance(engine, str) and _ENGINE_RE.match(engine)):
        raise ValueError("malformed engine id")
    if not (isinstance(project_id, str) and _PROJECT_ID_RE.match(project_id)):
        raise ValueError("malformed project id")
    # The project's own folder, from the project store — a path, not model text.
    if not (
        isinstance(cwd, str)
        and cwd.startswith("/")
        and len(cwd) <= PATH_MAX
        and not any(c in cwd for c in "\x00\n\r")
    ):
        raise ValueError("malformed project folder")
    # Where the folder RESOLVED when the card was shown: a start compares against it, so a
    # symlink repointed before the tap is refused rather than taken as the new baseline.
    real = e.get("real")
    if not (
        isinstance(real, str)
        and real.startswith("/")
        and len(real) <= PATH_MAX
        and not any(c in real for c in "\x00\n\r")
    ):
        raise ValueError("malformed resolved folder")
    for k in ("bypass_default",):
        if not isinstance(e.get(k), bool):
            raise ValueError(f"{k} must be a boolean")
    bypass = e.get("bypass")
    if bypass is not None and not isinstance(bypass, bool):
        raise ValueError("bypass must be a boolean")
    skey = e.get("session_key") or ""
    if skey and not (isinstance(skey, str) and _KEY_RE.match(skey)):
        raise ValueError("malformed session key")
    notice = e.get("notice") or ""
    if notice not in NOTICES:
        raise ValueError("unknown notice")
    out = {
        "id": pid,
        "turn_id": tid,
        "kind": "session",
        "state": state,
        "engine": engine,
        "project_id": project_id,
        "cwd": cwd,
        "real": real,
        "bypass_default": e["bypass_default"],
        "bypass": bypass,
        "session_key": skey,
        "notice": notice,
        "project": guard_text(e.get("project"), TITLE_MAX, secrets=secrets),
        "agent": guard_text(e.get("agent"), TITLE_MAX, secrets=secrets),
        "reason": guard_text(e.get("reason") or "", DETAIL_MAX, secrets=secrets),
    }
    for k in ("ts", "decided_at", "settled_at"):
        v = e.get(k)
        if isinstance(v, int | float) and not isinstance(v, bool):
            out[k] = float(v)
    brief = e.get("brief")
    if not isinstance(brief, str) or not brief:
        raise ValueError("a session proposal needs its brief")
    guarded = guard_text(brief, BRIEF_MAX, secrets=secrets)
    if guarded != brief:
        if fresh:
            raise Withheld("the brief contains a secret or looks like a credential")
        # An entry written earlier whose brief NOW matches a secret registered since: kept only
        # in its scrubbed form, and a pending one can no longer be started — what would launch is
        # no longer what was shown.
        if out["state"] == "pending":
            out["state"], out["reason"] = "stale", BRIEF_NOW_SECRET
    out["brief"] = guarded
    return out


def _admit_values(raw: object, secrets: list[str], *, fresh: bool, what: str) -> dict:
    """An operation's typed fields or facts: short keys; str (guarded), int, bool or None values.
    A NEW entry whose text the boundary would change is refused (Withheld), never stored altered."""
    if not isinstance(raw, dict) or len(raw) > OP_FIELDS_MAX:
        raise ValueError(f"malformed operation {what}")
    out: dict = {}
    for k, v in raw.items():
        if not (isinstance(k, str) and _OP_FIELD_RE.match(k)):
            raise ValueError(f"malformed operation {what} name")
        if isinstance(v, str):
            guarded = guard_text(v, OP_SUMMARY_MAX, secrets=secrets)
            if guarded != v and fresh:
                raise Withheld(f"the operation's {what} contain a secret or look like a credential")
            out[k] = guarded
        elif v is None or isinstance(v, bool) or (isinstance(v, int) and abs(v) < 10**12):
            out[k] = v
        else:
            raise ValueError(f"malformed operation {what} value")
    return out


def _admit_operation(e: dict, pid: str, tid: str, secrets: list[str], *, fresh: bool) -> dict:
    """One HQ operation entry (#1559). Method, path and body are never stored: the catalog rebuilds
    them at the tap, so this shape cannot name a route."""
    state, op, target, bind = e.get("state"), e.get("op"), e.get("target"), e.get("binding")
    if state not in OPERATION_STATES:
        raise ValueError("unknown operation state")
    if not (isinstance(op, str) and _OP_RE.match(op)):
        raise ValueError("malformed operation id")
    if not (
        isinstance(target, str)
        and (target == "" or _KEY_RE.match(target) or _PROJECT_ID_RE.match(target))
    ):
        raise ValueError("malformed operation target")
    if not (isinstance(bind, str) and _DIGEST_RE.match(bind)):
        raise ValueError("an operation needs its binding")
    out = {
        "id": pid,
        "turn_id": tid,
        "kind": "operation",
        "state": state,
        "op": op,
        "target": target,
        "binding": bind,
        "fields": _admit_values(e.get("fields") or {}, secrets, fresh=fresh, what="fields"),
        "facts": _admit_values(e.get("facts") or {}, secrets, fresh=fresh, what="facts"),
        "title": guard_text(e.get("title") or "", TITLE_MAX, secrets=secrets),
        "outcome": guard_text(e.get("outcome") or "", OP_SUMMARY_MAX, secrets=secrets),
        "reason": guard_text(e.get("reason") or "", DETAIL_MAX, secrets=secrets),
    }
    for k in ("ts", "decided_at", "settled_at"):
        v = e.get(k)
        if isinstance(v, int | float) and not isinstance(v, bool):
            out[k] = float(v)
    return out


def write_proposals(cid: str, rows: list[dict], *, fresh: str | None = None) -> list[dict]:
    """Replace the sidecar, atomically and durably, through the store boundary. The CALLER holds
    :func:`proposals_lock`. ``fresh`` names a NEW entry, which is refused (Withheld) rather than
    stored altered. Returns the entries as written."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    if len(rows) > MAX_SESSION_PROPOSALS:
        raise StoreRefused("this conversation holds as many proposals as it can")
    d = ensure_proposals_dir()
    with template_secrets.registration_fence():
        try:
            secrets = template_secrets.redaction_values()
        except template_secrets.RedactionUnavailable as e:
            raise StoreRefused(f"{e} — nothing was stored") from None
        admitted = [_admit_entry(r, secrets, fresh=r.get("id") == fresh) for r in rows]
        data = json.dumps({"proposals": admitted}, ensure_ascii=False).encode("utf-8", "replace")
        tmp = d / f".{cid}.{uuid.uuid4().hex}.tmp"
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                _write_all(fd, data)
            finally:
                os.close(fd)
            os.replace(tmp, _proposals_path(cid))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    _sync_dir(d)
    return admitted


# ---- the follows sidecar (#1443 Phase 4) --------------------------------------------------------


@contextlib.contextmanager
def follows_lock(cid: str) -> Iterator[None]:
    """The follows sidecar's lock, blocking and short, across one read-modify-write."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    fd = _open_lock(_follows_lock_path(cid))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _empty_follows() -> dict:
    return {
        "follows": [],
        "seen_at": 0.0,
        "last_update_at": 0.0,
        "updates": 0,
        "consumed": [],
        "outbox": [],
    }


def read_follows(cid: str) -> dict:
    """The conversation's follows; an empty document when there is none. SidecarUnreadable when
    it exists and cannot be read — never a partial document."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    try:
        raw = _follows_path(cid).read_bytes()
    except FileNotFoundError:
        return _empty_follows()
    except OSError as e:
        raise SidecarUnreadable(type(e).__name__) from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise SidecarUnreadable("not JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("follows"), list):
        raise SidecarUnreadable("malformed")
    out = _empty_follows()
    out["follows"] = [f for f in data["follows"] if isinstance(f, dict)]
    for k in ("seen_at", "last_update_at"):
        v = data.get(k)
        if isinstance(v, int | float) and not isinstance(v, bool):
            out[k] = float(v)
    n = data.get("updates")
    if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
        out["updates"] = n
    consumed = data.get("consumed") or []
    outbox = data.get("outbox") or []
    if not isinstance(consumed, list) or not isinstance(outbox, list):
        raise SidecarUnreadable("malformed")
    out["consumed"] = [c for c in consumed if isinstance(c, str)]
    out["outbox"] = [o for o in outbox if isinstance(o, dict)]
    return out


def _admit_follow(f: dict, secrets: list[str]) -> dict:
    key = f.get("key")
    if not (isinstance(key, str) and _KEY_RE.match(key)):
        raise ValueError("malformed followed session key")
    if f.get("via") not in FOLLOW_VIAS:
        raise ValueError("unknown follow origin")
    out: dict = {
        "key": key,
        "via": f["via"],
        "title": guard_text(f.get("title"), TITLE_MAX, secrets=secrets),
        "active": f.get("active") is True,
        "misses": f.get("misses") if isinstance(f.get("misses"), int) else 0,
    }
    v = f.get("needs_you")
    out["needs_you"] = v if isinstance(v, bool) else None
    # The newest visible-output time a `finished` has accounted for (#1443 Phase 4): a turn that
    # started AND ended between two passes still leaves a newer output time behind.
    t = f.get("out_through")
    out["out_through"] = (
        float(t) if isinstance(t, int | float) and not isinstance(t, bool) else None
    )
    since = f.get("since")
    out["since"] = float(since) if isinstance(since, int | float) else time.time()
    return out


def write_follows(cid: str, doc: dict) -> dict:
    """Replace the follows sidecar, atomically and durably, through the store boundary (titles are
    guarded). The CALLER holds :func:`follows_lock`. Returns the document as written."""
    if not valid_cid(cid):
        raise ConversationNotFound(cid)
    rows = doc.get("follows") or []
    if len(rows) > MAX_FOLLOW_ROWS:
        raise ValueError("too many follow rows")
    if sum(1 for f in rows if f.get("active") is True) > MAX_FOLLOWS:
        raise ValueError("too many active follows")
    _ensure_root()
    d = root() / "follows"
    _ensure_dir(d)
    with template_secrets.registration_fence():
        try:
            secrets = template_secrets.redaction_values()
        except template_secrets.RedactionUnavailable as e:
            raise StoreRefused(f"{e} — nothing was stored") from None
        consumed = list(doc.get("consumed") or [])
        if len(consumed) > MAX_CONSUMED or not all(
            isinstance(c, str) and _ACTION_ID_RE.match(c) for c in consumed
        ):
            raise ValueError("malformed consumed ids")
        out = {
            "follows": [_admit_follow(f, secrets) for f in rows],
            "seen_at": float(doc.get("seen_at") or 0.0),
            "last_update_at": float(doc.get("last_update_at") or 0.0),
            "updates": int(doc.get("updates") or 0),
            "consumed": consumed,
            # Pending update records, admitted exactly as the conversation will store them.
            "outbox": [_admit(dict(o, type="update"), secrets) for o in doc.get("outbox") or []],
        }
        data = json.dumps(out, ensure_ascii=False).encode("utf-8", "replace")
        tmp = d / f".{cid}.{uuid.uuid4().hex}.tmp"
        fd = os.open(
            tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600
        )
        try:
            try:
                _write_all(fd, data)
            finally:
                os.close(fd)
            os.replace(tmp, _follows_path(cid))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    _sync_dir(d)
    return out


# ---- retention ---------------------------------------------------------------------------------


def _pinned_by_proposals(cid: str) -> bool:
    """A proposal mid-decision pins its conversation. An unreadable sidecar pins too: retention may
    not delete what recovery might still need."""
    path = _proposals_path(cid)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        data = json.loads(raw)
    except ValueError:
        return True
    rows = data.get("proposals") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return True
    return any(isinstance(p, dict) and p.get("state") in PINNING_PROPOSAL_STATES for p in rows)


def delete(cid: str) -> None:
    """Remove a conversation and everything keyed by its id. The sidecars go first and the JSONL
    last, so a crash part-way leaves the conversation listed and the next sweep finishes it."""
    for p in (_proposals_path(cid), _follows_path(cid)):
        p.unlink(missing_ok=True)
    _path(cid).unlink(missing_ok=True)
    for p in (
        _lock_path(cid),
        _decide_lock_path(cid),
        _proposals_lock_path(cid),
        _follows_lock_path(cid),
    ):
        p.unlink(missing_ok=True)


def _still_expired(cid: str, cutoff: float) -> bool:
    files = _conversation_files()
    for rank, (c, _p, mtime) in enumerate(files):
        if c == cid:
            return mtime < cutoff or rank >= MAX_CONVERSATIONS
    return False


class ConversationNotDeleted(Exception):
    """The conversation's own file could not be removed; nothing was removed."""


class ConversationBusy(Exception):
    """The operator asked to delete a conversation that cannot go yet: a turn is running in it, or
    a proposal in it is being decided (a launch whose record recovery may still need)."""


def delete_conversation(cid: str) -> None:
    """The operator deletes one conversation: the same locks and pins as retention's, decided
    under them. Raises ConversationNotFound, or ConversationBusy while a turn runs in it or a
    proposal in it is mid-decision. Blocking."""
    if not exists(cid):
        raise ConversationNotFound(cid)
    with turn_lock(cid) as free:
        if not free:
            raise ConversationBusy("HQ is still answering in this conversation")
        # Lock order: turn → proposals → follows, as retention takes them.
        with proposals_lock(cid), follows_lock(cid):
            if not exists(cid):
                raise ConversationNotFound(cid)
            if _pinned_by_proposals(cid):
                raise ConversationBusy("a session from this conversation is still starting")
            # The CONVERSATION goes first, in one unlink: until it is gone nothing else is
            # touched, so a failure here leaves it whole (its cards and follows intact). The unlink
            # is made DURABLE (the store directory fsynced) before any sidecar goes, so a power
            # loss can never bring the conversation back without its cards and follows. If the
            # sync fails, the sidecars are left: either the unlink survives and they are orphans,
            # or it doesn't and the conversation comes back whole. Orphans are removed by
            # retention's orphan pass (`sweep`), which takes no age into account. Retention's own
            # `delete` runs the other way round because an EXPIRED conversation left half-deleted
            # is still expired and the next sweep finishes it; a fresh one would not be.
            try:
                _path(cid).unlink()
            except FileNotFoundError:
                raise ConversationNotFound(cid) from None
            except OSError as e:
                raise ConversationNotDeleted(type(e).__name__) from None
            try:
                _sync_dir(root())
            except OSError:
                log.warning("ask conversation delete not yet durable; its sidecars stay for now")
                return
            for p in (
                _proposals_path(cid),
                _follows_path(cid),
                _lock_path(cid),
                _decide_lock_path(cid),
                _proposals_lock_path(cid),
                _follows_lock_path(cid),
            ):
                with contextlib.suppress(OSError):
                    p.unlink(missing_ok=True)


def sweep(now: float | None = None) -> list[str]:
    """Apply retention. Returns the ids removed."""
    now = time.time() if now is None else now
    cutoff = now - RETENTION_DAYS * 86400
    removed: list[str] = []
    for n, (cid, _p, mtime) in enumerate(_conversation_files()):
        if mtime >= cutoff and n < MAX_CONVERSATIONS:
            continue
        if _pinned_by_proposals(cid):
            continue
        with turn_lock(cid) as free:
            if not free:
                continue  # a turn is running in it
            # Decided AGAIN under the locks — every writer holds one — so a turn that settled or
            # a tap that started deciding while this waited is seen: fresh age and rank, and the
            # proposal pin read under the sidecar's own lock.
            # …and the follows lock, so a follower write that passed its own existence check is
            # finished before this deletes, never left behind after it. Lock order: turn →
            # proposals → follows.
            with proposals_lock(cid), follows_lock(cid):
                if not _still_expired(cid, cutoff) or _pinned_by_proposals(cid):
                    continue
                delete(cid)
        removed.append(cid)
    # Sidecars whose conversation is gone (a crash between deletes, or an operator delete whose
    # cleanup didn't finish) follow it — but only once that ABSENCE is durable. Under the
    # sidecars' own locks (the order a delete takes them: proposals → follows), so this never
    # runs between a delete's unlink and its directory sync; the conversation's absence is
    # checked again there, and the store directory is synced before anything goes. A sync that
    # fails leaves the orphans for a later sweep: a crash could still bring the conversation
    # back, and it must come back whole (Hermes on #1564).
    for sub in ("proposals", "follows"):
        with contextlib.suppress(FileNotFoundError):
            for name in os.listdir(root() / sub):
                cid = name[: -len(".json")] if name.endswith(".json") else ""
                if not valid_cid(cid) or _path(cid).exists():
                    continue
                # Best effort, file by file: an orphan that can't go now is tried again at the
                # next sweep, and never fails the list or create request running this.
                try:
                    with proposals_lock(cid), follows_lock(cid):
                        if _path(cid).exists() or _pinned_by_proposals(cid):
                            continue
                        _sync_dir(root())
                        (root() / sub / name).unlink(missing_ok=True)
                        if sub == "follows":
                            _follows_lock_path(cid).unlink(missing_ok=True)
                        else:
                            _proposals_lock_path(cid).unlink(missing_ok=True)
                except OSError as e:
                    log.warning("ask orphan sidecar not removed yet (%s)", type(e).__name__)
    return removed
