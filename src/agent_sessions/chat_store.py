"""BattleLab's own conversation store for `chat`-runtime agents (#853 P9a, #1209).

A `chat` agent has no vendor store to read: BattleLab IS the agent, so it keeps the conversation.
One append-only JSONL file per session, ``<store root>/<uuid>.jsonl``, mode 0600 in a 0700
directory. Records are never rewritten: a turn's current state is FOLDED from every record that
names its ``turn_id`` (a later record wins), so a crash mid-write can cost at most the line being
written, never the history before it.

Record shapes (``type`` discriminates; unknown types and malformed lines are skipped — fail-soft
per record, like every other store reader):

    {"type": "session", "id", "cwd", "created_at"}                       first line, written once
    {"type": "user", "turn_id", "text", "ts"}                            the operator's message
    {"type": "status", "turn_id", "status": pending|awaiting_approval|done|failed, "reason"?, "ts"}
    {"type": "assistant", "turn_id", "text", "ts", "usage"?, "truncated"?, "dropped"?}
    {"type": "budget", "factor", "ts"}                                   after a context rejection
    {"type": "tool", "turn_id", "call_id", "name", "path", "outcome": running|ok|refused,
     "start_line"?, "end_line"?, "total_lines"?, "entries"?, "reason"?, "ts"}   #1222, a SUMMARY

Proposal audit records carry only ids, hashes and decisions; their pending content lives in
private sidecars (chat_edits), cleared on decision. Resume statuses preserve the tool summaries.

Structured requests (#1275) add an immutable ``request`` and configured ``model`` to a caller-id
session header, ``execution_binding`` and bounded ``context`` to user records, and ``decision``
records binding a decision UUID to its exact request before mutation. They are correlation and
provenance, never executable authority. ``revision`` counts complete LF record boundaries; an
unterminated tail is not accepted. Status ``code`` distinguishes interrupted and uncertain work.
Headers are fully synced before no-replace publication, so creation replay never sees half a
binding. A malformed user binding keeps its turn id occupied but refuses further execution.

A ``tool`` record never carries file contents — only what the pane shows. A turn's tool list
belongs to its LATEST attempt: a ``pending`` status (send or Retry) starts it afresh. A call still
``running`` when its turn settles is shown as ``stopped``.

The store kind below plugs this into the roster exactly as `ShellProvider` plugs its records in:
``scan``/``lookup`` list sessions, archive rides the metadata sidecar.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import metadata as _metadata
from .scanner import Session, fs_created_at
from .structured_types import normalize_context

_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: One operator message's size cap (chars) — refused above it at send (413), so a read never cuts.
TEXT_MAX = 200_000
#: One stored reply's cap (chars): above any configurable output (1M tokens × 4 chars). Applied —
#: and recorded as `truncated` — where the reply is ACCEPTED (`chat_runtime`), never silently here.
REPLY_MAX = 4_000_000
STATUSES = ("pending", "awaiting_approval", "done", "failed")
TOOL_OUTCOMES = ("running", "ok", "refused")
_TOOL_TEXT_MAX = 500
_TOOL_INT_FIELDS = ("start_line", "end_line", "total_lines", "entries")


@dataclass
class ChatTurn:
    """One exchange: the operator's message and, once settled, the agent's reply."""

    turn_id: str
    text: str
    ts: float
    status: str = "pending"
    reason: str | None = None
    code: str | None = None
    reply: str | None = None
    reply_ts: float | None = None
    usage: dict[str, int] | None = None
    truncated: bool = False
    #: Earlier exchanges left out of the request by the history budget (the pane's notice).
    dropped: int = 0
    #: The latest attempt's tool-call summaries, by call id, in first-seen order (#1222).
    tools: dict[str, dict] = field(default_factory=dict)
    #: Server-authored execution provenance and flow correlation; neither is a permission grant.
    execution_binding: str | None = None
    context: dict = field(default_factory=dict)
    binding_valid: bool = True

    def tool_list(self) -> list[dict]:
        settled = self.status != "pending"
        out = []
        for c in self.tools.values():
            c = dict(c)
            if settled and c["outcome"] == "running":
                c["outcome"] = "stopped"
            out.append(c)
        return out

    def as_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "text": self.text,
            "ts": self.ts,
            "status": self.status,
            "reason": self.reason,
            "code": self.code,
            "reply": self.reply,
            "reply_ts": self.reply_ts,
            "usage": self.usage,
            "truncated": self.truncated,
            "dropped": self.dropped,
            "tools": self.tool_list(),
            "execution_binding": self.execution_binding,
            "context": dict(self.context),
        }


@dataclass
class ChatLog:
    session_id: str
    cwd: str
    created_at: float
    turns: list[ChatTurn] = field(default_factory=list)
    budget_factor: float = 1.0
    #: Complete append-only record boundaries, including audit records. Never a wall clock.
    revision: int = 0
    request: dict | None = None
    model: str | None = None
    decisions: dict[str, dict] = field(default_factory=dict)
    proposal_audits: dict[str, dict] = field(default_factory=dict)

    def turn(self, turn_id: str) -> ChatTurn | None:
        return next((t for t in self.turns if t.turn_id == turn_id), None)

    def pending(self) -> ChatTurn | None:
        return next((t for t in self.turns if t.status == "pending"), None)


def valid_turn_id(value: object) -> bool:
    return isinstance(value, str) and _UUID_RE.fullmatch(value) is not None


def _path(root: Path, session_id: str) -> Path:
    if not _UUID_RE.fullmatch(session_id or ""):
        raise ValueError("malformed chat session id")
    return root / f"{session_id}.jsonl"


def _ensure_root(root: Path) -> None:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(root, 0o700)


def create(
    root: Path, session_id: str, *, cwd: str, request: dict | None = None, model: str | None = None
) -> None:
    """Publish one complete durable header, refusing an existing id without replacing it.

    A private temporary file is synced before the no-replace link. Concurrent readers therefore
    see either no conversation or its complete immutable request binding, never a partial header.
    """
    _ensure_root(root)
    path = _path(root, session_id)
    temporary = root / f".{session_id}-{uuid.uuid4().hex}.new"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            rec = {"type": "session", "id": session_id, "cwd": cwd, "created_at": time.time()}
            if request is not None:
                rec.update(request=request, model=model)
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.link(temporary, path)
        sync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)


def sync_directory(root: Path) -> None:
    """Retry the publication durability barrier even when a previous create already linked."""
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def append(root: Path, session_id: str, *records: dict) -> None:
    """Append records atomically with respect to other writers (flock) and durably (fsync).

    Framing is one record per LF-terminated line. A crash can leave an UNTERMINATED tail (a torn
    record); written straight after, the next record would be glued onto it and lost with it — so
    the tail is terminated first, under the lock, and stays behind as one unreadable line the
    reader skips (Hermes on #1216). `os.write` may write fewer bytes than asked: it is looped."""
    path = _path(root, session_id)
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # "replace": a lone surrogate from anywhere upstream is written as "?" rather than raising
        # and losing the whole batch of records (Hermes on #1228). Callers already pass
        # `chat_tools.printable` text; this is the last line of defence, not the first.
        data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records).encode(
            "utf-8", "replace"
        )
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            data = b"\n" + data
        view = memoryview(data)
        while view:
            n = os.write(fd, view)
            view = view[n:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _num(v: object) -> float | None:
    return float(v) if isinstance(v, int | float) and not isinstance(v, bool) else None


def _usage(v: object) -> dict[str, int] | None:
    if not isinstance(v, dict):
        return None
    out = {}
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        x = v.get(k)
        if isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 2**40:
            out[k] = x
    return out or None


def read(root: Path, session_id: str, *, durable: bool = False) -> ChatLog | None:
    """The folded conversation, or None when the file is absent or has no valid header."""
    try:
        path = _path(root, session_id)
        if durable:
            # Replay may race a peer's append between write and fsync. Its append lock is held
            # through sync; acquiring it and syncing again also covers the writer's failed fsync.
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                os.fsync(fh.fileno())
                raw = fh.read()
        else:
            raw = path.read_bytes()
    except (ValueError, OSError):
        return None
    log: ChatLog | None = None
    by_id: dict[str, ChatTurn] = {}
    # Split on the LF byte ONLY. `str.splitlines()` also splits on U+0085/U+2028/U+2029, which are
    # valid characters inside a JSON string — a message containing one vanished (Hermes on #1216).
    # Each record decodes on its own, so one torn or corrupt line costs only itself.
    # The unfinished tail is not an accepted record, even if its bytes happen to form JSON.
    for line in raw.split(b"\n")[:-1]:
        if not line:
            continue
        try:
            rec = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(rec, dict):
            continue
        t = rec.get("type")
        if t == "session" and log is None:
            cwd = rec.get("cwd")
            if rec.get("id") != session_id or not isinstance(cwd, str) or not cwd:
                return None
            log = ChatLog(session_id, cwd, _num(rec.get("created_at")) or 0.0)
            log.revision = raw.count(b"\n")
            log.request = rec.get("request") if isinstance(rec.get("request"), dict) else None
            log.model = rec.get("model") if isinstance(rec.get("model"), str) else None
            continue
        if log is None:
            continue
        tid = rec.get("turn_id")
        if t == "decision" and valid_turn_id(rec.get("decision_id")):
            log.decisions[rec["decision_id"]] = rec
            continue
        if t == "proposal" and valid_turn_id(rec.get("id")):
            log.proposal_audits[rec["id"]] = rec
            continue
        if t == "budget":
            f = _num(rec.get("factor"))
            if f is not None and 0.05 <= f <= 1.0:
                log.budget_factor = f
            continue
        if not valid_turn_id(tid):
            continue
        if t == "user" and tid not in by_id:
            text = rec.get("text")
            if isinstance(text, str):
                turn = ChatTurn(tid, text[:TEXT_MAX], _num(rec.get("ts")) or 0.0)
                binding = rec.get("execution_binding")
                turn.execution_binding = binding if isinstance(binding, str) else None
                turn.binding_valid = binding is None or isinstance(binding, str) and bool(binding)
                try:
                    turn.context = normalize_context(rec.get("context"))
                except ValueError:
                    # Keep the id occupied. Dropping a malformed user row could replay its work.
                    turn.binding_valid = False
                by_id[tid] = turn
                log.turns.append(turn)
        elif tid in by_id:
            turn = by_id[tid]
            if t == "status" and rec.get("status") in STATUSES:
                turn.status = rec["status"]
                reason = rec.get("reason")
                turn.reason = reason[:500] if isinstance(reason, str) else None
                code = rec.get("code")
                turn.code = code if code in ("interrupted", "uncertain") else None
                if turn.status == "pending" and rec.get("resume") is not True:
                    turn.reply, turn.usage, turn.truncated, turn.dropped = None, None, False, 0
                    turn.tools = {}
            elif t == "tool":
                call = _tool(rec)
                if call is not None:
                    turn.tools[call["call_id"]] = call
            elif t == "assistant" and isinstance(rec.get("text"), str):
                turn.reply = rec["text"][:REPLY_MAX]
                turn.reply_ts = _num(rec.get("ts"))
                turn.usage = _usage(rec.get("usage"))
                turn.truncated = rec.get("truncated") is True
                d = rec.get("dropped")
                turn.dropped = d if isinstance(d, int) and not isinstance(d, bool) and d >= 0 else 0
    return log


def _tool(rec: dict) -> dict | None:
    """A tool record's summary, validated field by field; None when it is not one."""
    cid, name, outcome = rec.get("call_id"), rec.get("name"), rec.get("outcome")
    if not (isinstance(cid, str) and 0 < len(cid) <= 128):
        return None
    if not isinstance(name, str) or outcome not in TOOL_OUTCOMES:
        return None
    path = rec.get("path")
    out: dict = {
        "call_id": cid,
        "name": name[:64],
        "path": path[:_TOOL_TEXT_MAX] if isinstance(path, str) else "",
        "outcome": outcome,
    }
    for k in _TOOL_INT_FIELDS:
        v = rec.get(k)
        out[k] = v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None
    reason = rec.get("reason")
    out["reason"] = reason[:_TOOL_TEXT_MAX] if isinstance(reason, str) else None
    return out


def session_ids(root: Path) -> list[str]:
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [
        n[: -len(".jsonl")] for n in names if n.endswith(".jsonl") and _UUID_RE.fullmatch(n[:-6])
    ]


def usage_since(root: Path, since: float) -> dict[str, int]:
    """Token totals over replies settled since ``since``, across every conversation in ``root``.
    Counted once per turn: the fold keeps one assistant record per turn."""
    totals = {"in": 0, "out": 0, "cache_read": 0, "cache_write": 0}
    for sid in session_ids(root):
        log = read(root, sid)
        if log is None:
            continue
        for t in log.turns:
            if t.status == "done" and t.usage and (t.reply_ts or 0) >= since:
                totals["in"] += t.usage.get("prompt_tokens", 0)
                totals["out"] += t.usage.get("completion_tokens", 0)
    return totals


class ChatStoreKind:
    """Store kind for layout ``battlelab-chat``. Engine-agnostic: it serves whichever `chat` engine
    selects the layout (`owner` is the provider the registry attached it to)."""

    owner = None  # set by PluginProvider.attach_kind

    def _root(self) -> Path | None:
        return self.owner.store_root() if self.owner is not None else None

    def store_present(self) -> bool:
        root = self._root()
        return root is not None and root.is_dir() and bool(session_ids(root))

    def is_present(self) -> bool:
        return self.store_present()

    def _row(self, root: Path, sid: str) -> Session | None:
        log = read(root, sid)
        if log is None:
            return None
        try:
            st = (root / f"{sid}.jsonl").stat()
        except OSError:
            return None
        first = next((t.text for t in log.turns), "")
        return Session(
            engine=self.owner.engine_id,
            uuid=sid,
            cwd=log.cwd,
            last_mtime=st.st_mtime,
            first_user_message=first,
            archived=False,
            created_at=log.created_at or fs_created_at(st),
        )

    def scan(self) -> list[Session]:
        root = self._root()
        if root is None:
            return []
        return [r for sid in session_ids(root) if (r := self._row(root, sid)) is not None]

    def lookup(self, native_id: str) -> Session | None:
        root = self._root()
        if root is None or not _UUID_RE.fullmatch(native_id or ""):
            return None
        return self._row(root, native_id)

    def archive(self, native_id: str) -> None:
        _metadata.patch(f"{self.owner.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id: str) -> None:
        _metadata.patch(f"{self.owner.engine_id}:{native_id}", archived=False)
