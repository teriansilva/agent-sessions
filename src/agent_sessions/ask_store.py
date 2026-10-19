"""Ask's durable conversations (#1443 Phase 1).

The Ask sidebar's conversation used to live in the browser only. An orchestrator whose answers,
proposals and follow-up updates vanish on a reload cannot keep an audit trail, so each
conversation is now an append-only JSONL file on the server:

    <root>/<cid>.jsonl            the records, one JSON object per LF-terminated line
    <root>/proposals/<cid>.json   Phase 3's proposal sidecar (read here by retention only)
    <root>/follows/<cid>.json     Phase 4's follow sidecar (read here by retention only)
    <root>/.locks/<cid>.lock      the turn lock: held for as long as a turn runs

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
import os
import re
import stat
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

from . import missions, template_secrets, template_suggest

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
RECORD_TYPES = frozenset({"conversation", "user", "assistant", "tool", "status"})


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
    _lock_path(cid).unlink(missing_ok=True)


def _still_expired(cid: str, cutoff: float) -> bool:
    files = _conversation_files()
    for rank, (c, _p, mtime) in enumerate(files):
        if c == cid:
            return mtime < cutoff or rank >= MAX_CONVERSATIONS
    return False


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
            # Decided AGAIN under the lock — every writer holds it — so a turn that settled while
            # this waited is seen: fresh age and rank, and the proposal pin.
            if not _still_expired(cid, cutoff) or _pinned_by_proposals(cid):
                continue
            delete(cid)
        removed.append(cid)
    # Sidecars whose conversation is gone (a crash between deletes) follow it.
    for sub in ("proposals", "follows"):
        with contextlib.suppress(FileNotFoundError):
            for name in os.listdir(root() / sub):
                cid = name[: -len(".json")] if name.endswith(".json") else ""
                if valid_cid(cid) and not _path(cid).exists() and not _pinned_by_proposals(cid):
                    (root() / sub / name).unlink(missing_ok=True)
    return removed
