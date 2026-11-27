"""Session search (#2373): one local index of every session's conversation, matched together with
its live details, so the sidebar and HQ find a session by any word in it — in milliseconds, with
no model.

**What is indexed.** The index holds CONVERSATION text only: the Commander's messages and the
agent's replies (tool calls and their output are left out — noisy, and they carry file contents
and command output). A session's DETAILS — its title, AI summary and recap, tag, project, the
agent's newest report and the title of the session a handoff continued — are matched against the
fresh rows the list builds on every request (`details`), so a rename or a new recap is never
stale. Both are normalised the same way (`normalize`).

**The store.** SQLite FTS5 with the trigram tokenizer (diacritics folded), at `index_path()`, mode
0600, ``journal_mode=DELETE`` (no WAL sidecars). It is a cache: a missing or corrupt file is
rebuilt. One row per message (`msg`), one row per session (`sess`: its fingerprint, counts,
failures and retry time).

**Freshness.** `refresh_pass` re-reads a session only when its fingerprint (last activity and the
transcript's size) changed, newest first, bounded per pass (`PASS_SESSIONS_MAX`,
`PASS_SECONDS_MAX`). A session's rows are replaced in ONE transaction. A read that fails — or
reads nothing from a non-empty source — keeps the old rows and is retried with backoff. A session
gone from the scan is dropped. The whole history is read (`transcript.full_turns`), never the
tail every other reader keeps.

**Scope.** The index never decides what is visible. `search` is given the scoped rows — main
sessions, and the in-scope children of each — and only their keys can match. An out-of-scope
child contributes nothing: no hit, no snippet, no name.

**Matching.** Every word must be found somewhere in the session (session-wide AND): in its title,
a detail, or any message of the session or of an in-scope child. Each word is looked up on its
own and the sets are intersected — a single FTS5 AND only matches within one row. Words shorter
than three letters (under the trigram floor) are matched in titles and details only. A word found
in more than `SEARCH_TERM_KEYS_MAX` sessions is too common to list: it still constrains the
result, checked against the sessions the other words found; when every word is that common the
conversations are not searched and the result says so (`degraded`).
"""

from __future__ import annotations

import bisect
import contextlib
import logging
import os
import re
import sqlite3
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: A word found in more sessions than this is too common to list (it only narrows the others).
SEARCH_TERM_KEYS_MAX = 1500
#: A word in more MESSAGES than this is too common without listing it at all (a fast row count).
ROWS_COMMON_MAX = 20_000
#: Checking a too-common word within the other words' sessions: at most this many sessions, and
#: this long; past either the result is degraded rather than incomplete.
WITHIN_KEYS_MAX = 400
WITHIN_SECONDS_MAX = 0.5
#: Words a query is cut to; longer queries keep their first words.
QUERY_TERMS_MAX = 8
#: Under the trigram floor: matched in titles and details only.
SHORT_TERM = 3
#: Per refresh pass: at most this many sessions re-read, for at most this long.
PASS_SESSIONS_MAX = 40
PASS_SECONDS_MAX = 20.0
#: How often the background loop runs a pass, and how long it waits after a failed read.
PASS_INTERVAL_S = 30.0
PASS_FIRST_DELAY_S = 20.0
RETRY_BASE_S = 60.0
RETRY_MAX_S = 3600.0
#: A long message is indexed WHOLE, as parts of about this size cut at whitespace (a word is
#: never split, unless a run longer than this has none): no text of it is ever dropped.
CHUNK_CHARS = 16_000
#: A matched message longer than this never becomes a snippet (withheld): a snippet is cut only
#: after the WHOLE message went through the boundary, and a bigger one costs too much to guard.
SNIPPET_MESSAGE_CHARS_MAX = 1_000_000
#: A snippet's window around the match.
SNIPPET_CHARS = 160
#: The fallback candidates HQ's model may rerank (`candidates`).
CANDIDATES_MAX = 30
SCHEMA_VERSION = "3"

TIERS = ("title", "details", "conversation")
_TIER_RANK = {t: i for i, t in enumerate(TIERS)}
_WORD = re.compile(r"[\w'’-]+", re.UNICODE)
#: A handoff seed names its source as "- session: <engine>:<id>" (newlines may be collapsed).
_HANDOFF_SOURCE = re.compile(r"session:\s*([\w.-]+:[\w.-]+)")

_WRITE_LOCK = threading.Lock()

#: Every read on a search's path that may return many rows returns ONE aggregated row instead,
#: parsed here. Python's sqlite3 releases and retakes the GIL for every row it steps, and the app
#: always has other threads running: with one busy thread a 16k-row listing measured 30 ms idle
#: and 2.3-3.8 s, while the same rows as one `group_concat` row stayed at 35-47 ms.
_US, _RS = "\x1f", "\x1e"  # unit / record separators: never in a key, a number or `who`


def _records(con: sqlite3.Connection, cols: str, sql: str, params=()) -> list[list[str]]:
    """``sql``'s rows as fields (text), read as ONE row: ``cols`` is the SQL expression list
    ``sql`` selects, each field of which is joined with `_US`, each row with `_RS`."""
    fields = " || char(31) || ".join(f"ifnull({c}, '')" for c in cols.split("|"))
    if " LIMIT " not in sql:
        sql += " LIMIT -1"  # never flattened into the aggregate: bm25() needs its own query
    agg = con.execute(
        f"SELECT group_concat({fields}, char(30)) FROM ({sql})",  # noqa: S608 — fixed text
        params,
    ).fetchone()[0]
    return [r.split(_US) for r in agg.split(_RS)] if agg else []


def _float(text: str) -> float:
    return float(text) if text else 0.0


_FTS_OK: bool | None = None


def index_path() -> Path:
    return Path(
        os.environ.get(
            "AGENT_SESSIONS_SESSION_INDEX",
            str(Path.home() / ".config" / "agent-sessions" / "session-index.db"),
        )
    )


_NORM_CACHE: dict[str, str] = {}
_NORM_CACHE_MAX = 50_000


def normalize(text: str) -> str:
    """Casefolded, diacritics removed (NFKD, combining marks dropped): the form both a query and
    the text it is matched against are compared in. ASCII needs only the casefold, and the live
    details are normalised once each (bounded cache), not on every keystroke."""
    text = text or ""
    if text.isascii():
        return text.casefold()
    hit = _NORM_CACHE.get(text)
    if hit is None:
        decomposed = unicodedata.normalize("NFKD", text)
        hit = "".join(c for c in decomposed if not unicodedata.combining(c)).casefold()
        if len(_NORM_CACHE) >= _NORM_CACHE_MAX:
            _NORM_CACHE.clear()
        _NORM_CACHE[text] = hit
    return hit


def terms(query: str) -> list[str]:
    """The query's words, normalised, de-duplicated, in order, at most `QUERY_TERMS_MAX`."""
    out: list[str] = []
    for w in _WORD.findall(normalize(query)):
        w = w.strip("'’-")
        if w and w not in out:
            out.append(w)
    return out[:QUERY_TERMS_MAX]


def fts_available() -> bool:
    """FTS5 with the trigram tokenizer, probed once. Without it search stays on substring."""
    global _FTS_OK
    if _FTS_OK is None:
        try:
            con = sqlite3.connect(":memory:")
            con.execute("CREATE VIRTUAL TABLE t USING fts5(x, tokenize='trigram')")
            con.close()
            _FTS_OK = True
        except sqlite3.Error:
            _FTS_OK = False
    return _FTS_OK


# ---- the store -----------------------------------------------------------------------------------

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)",
    "CREATE TABLE IF NOT EXISTS sess(key TEXT PRIMARY KEY, fp TEXT, ok INTEGER, indexed_at REAL,"
    " messages INTEGER DEFAULT 0, bytes INTEGER DEFAULT 0, truncated INTEGER DEFAULT 0,"
    " fails INTEGER DEFAULT 0, next_try REAL DEFAULT 0)",
    # A message is one row per part (`CHUNK_CHARS`); its parts hold consecutive rowids, so the
    # whole message is `rowid BETWEEN first AND first + parts - 1` (first = rowid - part).
    "CREATE VIRTUAL TABLE IF NOT EXISTS msg USING fts5(key UNINDEXED, who UNINDEXED,"
    " seq UNINDEXED, part UNINDEXED, parts UNINDEXED, text,"
    " tokenize='trigram remove_diacritics 1')",
    # The session a handoff continued, read from its first message at index time (the row keeps
    # only that message's first line): a key lookup, never a scan of the message table.
    "CREATE TABLE IF NOT EXISTS handoff(key TEXT PRIMARY KEY, source TEXT NOT NULL)",
    # Each session's messages hold one contiguous rowid range: a word's sessions are found from
    # the index's own rowids (no content lookup per match), and a session's rows are replaced or
    # read by `rowid BETWEEN`, which FTS5 seeks, never by scanning the table for its key.
    "CREATE TABLE IF NOT EXISTS span(key TEXT PRIMARY KEY, lo INTEGER NOT NULL,"
    " hi INTEGER NOT NULL)",
)


def _open(path: Path | None = None) -> sqlite3.Connection:
    """The index, created (0600) or rebuilt when it is missing, of another schema, or corrupt."""
    path = path or index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in (1, 2):
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        try:
            con = sqlite3.connect(path, timeout=30, check_same_thread=False)
            # WAL: a search reads while a pass writes a session's rows (the rollback journal
            # blocked readers for a whole session's transaction: seconds at p95 during refresh).
            con.execute("PRAGMA journal_mode=WAL")
            for stmt in _SCHEMA:
                con.execute(stmt)
            ver = con.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
            if ver is None:
                con.execute("INSERT INTO meta(k, v) VALUES('schema', ?)", (SCHEMA_VERSION,))
                con.commit()
            elif ver[0] != SCHEMA_VERSION:
                raise sqlite3.DatabaseError("index schema changed")
            for p in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
                with contextlib.suppress(FileNotFoundError):
                    os.chmod(p, 0o600)
            return con
        except sqlite3.DatabaseError:
            if attempt == 2:
                raise
            # A cache: a corrupt or outdated index is dropped and built again.
            log.warning("session index unreadable; rebuilding it", exc_info=True)
            with contextlib.suppress(Exception):
                con.close()
            for suffix in ("", "-journal", "-wal", "-shm"):
                with contextlib.suppress(FileNotFoundError):
                    path.with_name(path.name + suffix).unlink()
    raise AssertionError("unreachable")


@dataclass
class Progress:
    """``indexed``: sessions whose conversation is in the index. ``tried``: those plus the ones
    whose read failed (retried later). ``failed``: tried, never read; ``truncated``: indexed, but
    only up to the full-read bound (`transcript.FULL_READ_MAX_BYTES`).

    ``settled``: every session was tried — "still indexing" ends, so an unreadable session never
    keeps it up for ever. ``complete``: settled AND every conversation is in the index whole —
    only then may an answer say no session holds a word (`incomplete` counts the others)."""

    indexed: int
    total: int
    tried: int = 0
    failed: int = 0
    truncated: int = 0

    @property
    def settled(self) -> bool:
        return self.total > 0 and max(self.tried, self.indexed) >= self.total

    @property
    def incomplete(self) -> int:
        return self.failed + self.truncated

    @property
    def complete(self) -> bool:
        return self.settled and self.incomplete == 0


def progress(total_keys: set[str] | None = None) -> Progress:
    """How many sessions have their conversation indexed (of ``total_keys``, when given)."""
    if not fts_available() or not index_path().exists():
        return Progress(0, len(total_keys or ()))
    con = _open()
    try:
        rows = [
            (k, int(ok or 0), int(t or 0))
            for k, ok, t in _records(con, "key|ok|truncated", "SELECT key, ok, truncated FROM sess")
        ]
    finally:
        con.close()
    done = {k for k, ok, _t in rows if ok == 1}
    tried = {k for k, _ok, _t in rows}
    cut = {k for k, ok, t in rows if ok == 1 and t}
    if total_keys is None:
        return Progress(len(done), len(done), len(tried), len(tried - done), len(cut))
    return Progress(
        len(done & total_keys),
        len(total_keys),
        len(tried & total_keys),
        len((tried - done) & total_keys),
        len(cut & total_keys),
    )


# ---- building -----------------------------------------------------------------------------------


def _chunks(text: str) -> list[str]:
    """``text`` as parts of at most `CHUNK_CHARS`, each cut after whitespace where there is any
    in its last quarter; ``"".join`` of the parts is ``text`` exactly."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    out, at = [], 0
    while len(text) - at > CHUNK_CHARS:
        end = at + CHUNK_CHARS
        floor = end - CHUNK_CHARS // 4
        cut = max(text.rfind(" ", floor, end), text.rfind("\n", floor, end))
        cut = cut + 1 if cut > at else end
        out.append(text[at:cut])
        at = cut
    out.append(text[at:])
    return out


def _docs(turns: list) -> list[tuple[str, int, str]]:
    """The messages a session's turns hold, as ``(who, seq, text)``: the Commander's words and
    the agent's replies; tool calls, tool output and injected context are left out. Whole: a
    long message is split into parts at insert (`_chunks`), never cut."""
    out: list[tuple[str, int, str]] = []
    for i, t in enumerate(turns):
        role, kind = getattr(t, "role", ""), getattr(t, "kind", None)
        text = (getattr(t, "text", "") or "").strip()
        if not text:
            continue
        if role == "user" and kind != "context":
            who = "you"
        elif role == "assistant" and kind in (None, "text"):
            who = "agent"  # replies only: tool calls ("tool") and results are left out
        else:
            continue
        out.append((who, i, text))
    return out


def _fingerprint(s, growth: int | None) -> str:
    return f"{float(s.last_mtime or 0):.3f}:{growth if growth is not None else '-'}"


def refresh_pass(
    sessions: list | None = None,
    *,
    home: Path | None = None,
    now: float | None = None,
    max_sessions: int = PASS_SESSIONS_MAX,
    max_seconds: float = PASS_SECONDS_MAX,
) -> dict:
    """One bounded pass: re-read the sessions whose fingerprint changed, newest first; drop the
    ones gone from the scan. Returns what it did. Blocking — run it in a thread."""
    from . import engines, transcript

    if not fts_available():
        return {"skipped": "fts5 unavailable"}
    home = home or Path.home()
    now = time.time() if now is None else now
    scanned = list(engines.scan_all_cached()) if sessions is None else list(sessions)
    started = time.monotonic()
    stats = {"scanned": len(scanned), "indexed": 0, "failed": 0, "removed": 0, "pending": 0}
    with _WRITE_LOCK:
        con = _open()
        try:
            known = {
                r[0]: (r[1], r[2], r[3])
                for r in con.execute("SELECT key, fp, ok, next_try FROM sess").fetchall()
            }
            todo: list[tuple[object, str, str]] = []
            present: set[str] = set()
            for s in sorted(scanned, key=lambda x: -float(x.last_mtime or 0)):
                key = engines.session_key(s)
                present.add(key)
                try:
                    growth = transcript.growth_mark(s.engine, s.uuid, home)
                except Exception:  # noqa: BLE001 — no growth signal: the activity time decides
                    growth = None
                fp = _fingerprint(s, growth)
                old = known.get(key)
                if old is not None and old[0] == fp and old[1] == 1:
                    continue
                if old is not None and float(old[2] or 0) > now:
                    continue  # a failed read waits for its retry time
                todo.append((s, key, fp))
            stats["pending"] = len(todo)
            for s, key, fp in todo:
                if stats["indexed"] + stats["failed"] >= max_sessions:
                    break
                if time.monotonic() - started >= max_seconds:
                    break
                if _index_one(con, s, key, fp, home=home, now=now):
                    stats["indexed"] += 1
                else:
                    stats["failed"] += 1
            stats["pending"] -= stats["indexed"]
            if scanned:
                gone = [k for k in known if k not in present]
                for k in gone:
                    with con:
                        _drop_rows(con, k)
                        con.execute("DELETE FROM handoff WHERE key=?", (k,))
                        con.execute("DELETE FROM sess WHERE key=?", (k,))
                stats["removed"] = len(gone)
        finally:
            con.close()
    return stats


def _index_one(con: sqlite3.Connection, s, key: str, fp: str, *, home: Path, now: float) -> bool:
    """Replace one session's rows in one transaction. False (old rows kept, retry scheduled) when
    the read failed or read nothing from a source that is not empty."""
    from . import transcript

    try:
        turns = transcript.full_turns(s.engine, s.uuid, home)
    except Exception:  # noqa: BLE001 — unreadable is not empty: keep the old rows, try later
        log.info("session index: %s could not be read", key, exc_info=True)
        _failed(con, key, now)
        return False
    if turns is None:
        turns = []  # no reader for this engine: nothing to index, and nothing failed
    elif not turns:
        had_rows = con.execute("SELECT 1 FROM span WHERE key=?", (key,)).fetchone() is not None
        try:
            growth = transcript.growth_mark(s.engine, s.uuid, home)
        except Exception:  # noqa: BLE001
            growth = None
        if growth and had_rows:
            # Nothing read from a non-empty source that HAD messages: unreadable, never "now
            # empty" — the old rows stay and the read is retried. A session that never had any
            # (a resume stub, an API session never used) is simply indexed as empty.
            _failed(con, key, now)
            return False
    docs = _docs(turns)
    first = next((t for who, seq, t in docs if who == "you"), "")
    src = _HANDOFF_SOURCE.search(first) if first.startswith("# Handoff") else None
    with con:
        _drop_rows(con, key)
        con.execute("DELETE FROM handoff WHERE key=?", (key,))
        if src:
            con.execute("INSERT INTO handoff(key, source) VALUES(?, ?)", (key, src.group(1)))
        if docs:
            row = con.execute("SELECT v FROM meta WHERE k='next_rowid'").fetchone()
            lo = int(row[0]) if row else 1
            parts = [
                (who, seq, i, len(chunks), chunk)
                for who, seq, text in docs
                for chunks in (_chunks(text),)
                for i, chunk in enumerate(chunks)
            ]
            con.executemany(
                "INSERT INTO msg(rowid, key, who, seq, part, parts, text)"
                " VALUES(?, ?, ?, ?, ?, ?, ?)",
                [(lo + n, key, *row) for n, row in enumerate(parts)],
            )
            hi = lo + len(parts) - 1
            con.execute("INSERT INTO span(key, lo, hi) VALUES(?, ?, ?)", (key, lo, hi))
            con.execute(
                "INSERT INTO meta(k, v) VALUES('next_rowid', ?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(hi + 1),),
            )
        con.execute(
            "INSERT INTO sess(key, fp, ok, indexed_at, messages, bytes, truncated, fails, next_try)"
            " VALUES(?, ?, 1, ?, ?, ?, ?, 0, 0) ON CONFLICT(key) DO UPDATE SET fp=excluded.fp,"
            " ok=1, indexed_at=excluded.indexed_at, messages=excluded.messages,"
            " bytes=excluded.bytes, truncated=excluded.truncated, fails=0, next_try=0",
            (
                key,
                fp,
                now,
                len(docs),
                sum(len(t.encode("utf-8", "replace")) for _w, _s, t in docs),
                1 if transcript.was_truncated(turns) else 0,
            ),
        )
    return True


def _drop_rows(con: sqlite3.Connection, key: str) -> None:
    """Delete a session's messages by its rowid span (an FTS5 seek), and the span."""
    span = con.execute("SELECT lo, hi FROM span WHERE key=?", (key,)).fetchone()
    if span is not None:
        con.execute("DELETE FROM msg WHERE rowid BETWEEN ? AND ?", span)
        con.execute("DELETE FROM span WHERE key=?", (key,))


def _spans(con: sqlite3.Connection) -> tuple[list[int], list[int], list[str]]:
    """Every session's rowid span, sorted: ``(los, his, keys)`` for `bisect` lookups."""
    rows = sorted(
        (int(lo), int(hi), key)
        for lo, hi, key in _records(con, "lo|hi|key", "SELECT lo, hi, key FROM span")
    )
    return [r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows]


def _key_of(rowid: int, spans: tuple[list[int], list[int], list[str]]) -> str | None:
    los, his, keys = spans
    i = bisect.bisect_right(los, rowid) - 1
    if i >= 0 and rowid <= his[i]:
        return keys[i]
    return None


def _failed(con: sqlite3.Connection, key: str, now: float) -> None:
    row = con.execute("SELECT fails FROM sess WHERE key=?", (key,)).fetchone()
    fails = (row[0] if row else 0) + 1
    wait = min(RETRY_MAX_S, RETRY_BASE_S * (2 ** (fails - 1)))
    with con:
        if row is None:
            con.execute(
                "INSERT INTO sess(key, fp, ok, indexed_at, fails, next_try)"
                " VALUES(?, '', 0, ?, ?, ?)",
                (key, now, fails, now + wait),
            )
        else:
            # ok=0 even when old rows are kept: they are searchable, but the session's NEWEST
            # conversation is not in them, so it counts as failed (`progress`), never complete.
            con.execute(
                "UPDATE sess SET ok=0, fails=?, next_try=? WHERE key=?", (fails, now + wait, key)
            )


#: One pass's own process gets this long before it is stopped (its rows are per-session
#: transactions, so a stopped pass leaves every session either old or new, never half).
PASS_PROCESS_TIMEOUT_S = PASS_SECONDS_MAX * 6


async def run() -> None:
    """The background loop: one bounded pass every `PASS_INTERVAL_S`, never on a request path.

    Each pass runs in its OWN short-lived process (`python -I -m agent_sessions.session_index`):
    reading and parsing whole transcripts is CPU and memory heavy, and in the app's process it held
    the interpreter lock against the list requests (p95 doubled during a pass) and grew the app's
    memory by the largest transcript read. The child parses on its own interpreter, at a lower
    priority, writes through WAL while the app reads, and gives its memory back when it exits.
    A literal argv, no shell."""
    import asyncio
    import sys

    await asyncio.sleep(PASS_FIRST_DELAY_S)  # not during the app's own startup work
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-m",
                "agent_sessions.session_index",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), PASS_PROCESS_TIMEOUT_S)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                log.warning("session index pass stopped after %ss", PASS_PROCESS_TIMEOUT_S)
                out = b""
            text = out.decode("utf-8", "replace").strip()
            if text and any(f'"{k}": 0' not in text for k in ("indexed", "failed", "removed")):
                log.info("session index pass: %s", text[-500:])
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop never dies on one bad pass
            log.warning("session index pass failed", exc_info=True)
        await asyncio.sleep(PASS_INTERVAL_S)


def main() -> int:
    """One pass, in its own process (`run` starts it): lower priority, stats on stdout."""
    import json

    with contextlib.suppress(OSError):
        os.nice(10)
    print(json.dumps(refresh_pass()))
    return 0


# ---- matching -----------------------------------------------------------------------------------


@dataclass
class Match:
    """Why a main session matched: its tier, the field or message the snippet is from, and the
    child it came from (in-scope children only)."""

    tier: str
    field: str
    snippet: str = ""
    marks: list[tuple[int, int]] = field(default_factory=list)
    child: str | None = None
    more: int = 0
    withheld: bool = False

    def as_dict(self) -> dict:
        out: dict = {"tier": self.tier, "field": self.field}
        if self.snippet:
            out["snippet"] = self.snippet
            out["marks"] = [list(m) for m in self.marks]
        if self.child:
            out["child"] = self.child
        if self.more:
            out["more"] = self.more
        if self.withheld:
            out["withheld"] = True
        return out


@dataclass
class Result:
    """``mode``: "index" (conversations searched) or "titles" (titles and details only: no
    index yet, FTS5 missing, or `degraded`). ``matches``: main id → `Match`, unordered."""

    mode: str
    matches: dict[str, Match]
    degraded: str | None = None
    progress: Progress | None = None
    short: bool = False
    #: row id → the session key its handoff continued (`handoff_sources`), for snippets.
    handoffs: dict[str, str] = field(default_factory=dict)
    #: main id → the lowest (best) bm25 of its conversation rows across the words, when any.
    scores: dict[str, float] = field(default_factory=dict)

    def order_key(self, row: dict) -> tuple:
        m = self.matches[row["id"]]
        return (_TIER_RANK[m.tier], not row.get("sticky"), -float(row.get("last_mtime") or 0))


def details(
    row: dict,
    *,
    reports: dict | None = None,
    titles: dict | None = None,
    handoffs: dict | None = None,
) -> list:
    """A row's matchable details, ``[(field, text)]``: the fresh values the list built.
    ``handoffs``: row id → the key of the session its handoff continued (`handoff_sources`)."""
    out: list[tuple[str, str]] = []
    for fld, value in (
        ("summary", row.get("ai_summary")),
        ("recap", row.get("ai_recap")),
        ("tag", row.get("tag")),
        (
            "project",
            (row.get("project") or {}).get("name") if isinstance(row.get("project"), dict) else "",
        ),
    ):
        if isinstance(value, str) and value.strip():
            out.append((fld, value))
    rep = (reports or {}).get(row.get("id"))
    if isinstance(rep, dict) and isinstance(rep.get("summary"), str) and rep["summary"].strip():
        out.append(("report", rep["summary"]))
    first = row.get("first_user_message") or ""
    if first.startswith("# Handoff"):
        # A handoff's first message is boilerplate: the session it continued is what names it.
        # The row keeps only its first line, so the source comes from the indexed message.
        src = (handoffs or {}).get(row.get("id"))
        title = (titles or {}).get(src) if src else None
        if title:
            out.append(("handoff", title))
    elif first.strip():
        # The raw first message, as the list always matched it (#284): it keeps a session whose
        # title normalised to "" findable even before its conversation is indexed.
        out.append(("first", first))
    return out


def handoff_sources(rows: list[dict]) -> dict[str, str]:
    """Row id → the session key its handoff seed names (recorded at index time from the full
    first message). Only rows that open with a handoff are looked up, by key."""
    ids = [r["id"] for r in rows if (r.get("first_user_message") or "").startswith("# Handoff")]
    if not ids or not fts_available() or not index_path().exists():
        return {}
    con = _open()
    out: dict[str, str] = {}
    try:
        for i in range(0, len(ids), 900):
            chunk = ids[i : i + 900]
            marks = ",".join("?" * len(chunk))
            for key, source in con.execute(
                f"SELECT key, source FROM handoff WHERE key IN ({marks})",  # noqa: S608 — placeholders only
                chunk,
            ):
                out[key] = source
    finally:
        con.close()
    return out


_DETAILS_CACHE: dict[str, tuple[tuple, list]] = {}


def _norm_details(row: dict, reports, titles, handoffs) -> list:
    """A row's normalised details, cached by the row's own current values: nothing is rebuilt
    on a keystroke unless something it is built from changed."""
    rid = row["id"]
    rep = (reports or {}).get(rid)
    src = (handoffs or {}).get(rid)
    sig = (
        row.get("ai_summary"),
        row.get("ai_recap"),
        row.get("tag"),
        (row.get("project") or {}).get("name") if isinstance(row.get("project"), dict) else None,
        row.get("first_user_message"),
        rep.get("summary") if isinstance(rep, dict) else None,
        (titles or {}).get(src) if src else None,
    )
    hit = _DETAILS_CACHE.get(rid)
    if hit is not None and hit[0] == sig:
        return hit[1]
    value = [
        (f, normalize(t))
        for f, t in details(row, reports=reports, titles=titles, handoffs=handoffs)
    ]
    if len(_DETAILS_CACHE) > 20_000:
        _DETAILS_CACHE.clear()
    _DETAILS_CACHE[rid] = (sig, value)
    return value


def _quote(term: str) -> str:
    """One literal FTS5 phrase: never query syntax."""
    return '"' + term.replace('"', '""') + '"'


def _term_keys(
    con: sqlite3.Connection,
    term: str,
    limit: int,
    within: list[str] | None = None,
    *,
    scores: bool = False,
    spans: tuple | None = None,
) -> dict[str, float] | None:
    """The sessions whose messages hold ``term`` (``key → best bm25`` when ``scores``, else 0.0).

    Listing (no ``within``): None when the term is in more than `ROWS_COMMON_MAX` messages or more
    than ``limit`` sessions — too common to list. ONE pass over the word's rowids, capped at
    `ROWS_COMMON_MAX` + 1: a common word stops early, and a listed one is never walked twice (a
    `count(*)` first read its whole doclist, then the listing read it again). bm25 is asked only of
    a listed word, by a second statement (``scores``). The index's own rowids are mapped to
    sessions through their spans, so no content row is read per match.

    ``within``: only those sessions, each checked by a `rowid BETWEEN` seek; None (degraded, never
    a silently partial answer) past `WITHIN_KEYS_MAX` sessions or `WITHIN_SECONDS_MAX`."""
    q = _quote(term)
    spans = spans if spans is not None else _spans(con)
    out: dict[str, float] = {}
    if within is not None:
        if len(within) > WITHIN_KEYS_MAX:
            return None
        ranges = dict(zip(spans[2], zip(spans[0], spans[1], strict=True), strict=True))
        started = time.monotonic()
        for key in within:
            rng = ranges.get(key)
            if rng is None:
                continue
            if time.monotonic() - started > WITHIN_SECONDS_MAX:
                return None
            if scores:  # one aggregated row, never one per message
                sql = (
                    "SELECT min(s) FROM (SELECT bm25(msg) AS s FROM msg"
                    " WHERE msg MATCH ? AND rowid BETWEEN ? AND ? LIMIT -1)"
                )
            else:
                sql = "SELECT 0.0 FROM msg WHERE msg MATCH ? AND rowid BETWEEN ? AND ? LIMIT 1"
            row = con.execute(sql, (q, *rng)).fetchone()
            if row is not None and row[0] is not None:
                out[key] = row[0]
        return out
    ids = _records(
        con, "r", "SELECT rowid AS r FROM msg WHERE msg MATCH ? LIMIT ?", (q, ROWS_COMMON_MAX + 1)
    )
    if len(ids) > ROWS_COMMON_MAX:
        return None
    if scores and ids:
        # %.17g: the score round-trips exactly, so ties rank as bm25 ranked them.
        recs = _records(
            con,
            "r|s",
            "SELECT rowid AS r, printf('%.17g', bm25(msg)) AS s FROM msg WHERE msg MATCH ?",
            (q,),
        )
        rows = sorted((int(r), _float(sc)) for r, sc in recs)
    else:
        rows = sorted((int(r[0]), 0.0) for r in ids)
    # Both lists ascend by rowid: one merge walk maps every match to its session's span.
    los, his, keys = spans
    j, n = 0, len(los)
    for rowid, score in rows:
        while j < n and his[j] < rowid:
            j += 1
        if j >= n:
            break
        if rowid < los[j]:
            continue  # between spans: a row of no listed session
        key = keys[j]
        prev = out.get(key)
        if prev is None:
            if len(out) >= limit:
                return None
            out[key] = score
        elif score < prev:
            out[key] = score
    return out


def search(
    query: str,
    rows: list[dict],
    children: dict[str, list[dict]] | None = None,
    *,
    reports: dict | None = None,
    titles: dict | None = None,
    use_index: bool = True,
    scores: bool = False,
) -> Result:
    """Match ``query`` against the scoped main ``rows`` and the in-scope ``children`` of each
    (main id → child rows). Only those keys can match; see the module docstring for the rules."""
    ts = terms(query)
    children = children or {}
    if not ts:
        return Result("titles", {})
    longs = [t for t in ts if len(t) >= SHORT_TERM]
    has_short = len(longs) < len(ts)
    owner = {r["id"]: r["id"] for r in rows}
    child_title = {}
    for main_id, kids in children.items():
        if main_id not in owner:
            continue
        for c in kids:
            owner[c["id"]] = main_id
            child_title[c["id"]] = c.get("title") or ""
    # Where each term is found, per main: tier names it was found in.
    found: dict[str, dict[str, set[str]]] = {r["id"]: {} for r in rows}
    norm_title = {r["id"]: normalize(r.get("title") or "") for r in rows}
    handoffs = handoff_sources(rows) if use_index else {}
    norm_details = {r["id"]: _norm_details(r, reports, titles, handoffs) for r in rows}
    for r in rows:
        rid = r["id"]
        for t in ts:
            if t in norm_title[rid]:
                found[rid].setdefault(t, set()).add("title")
            if any(t in nt for _f, nt in norm_details[rid]):
                found[rid].setdefault(t, set()).add("details")
    mode, degraded = "titles", None
    conv: dict[str, dict[str, float]] = {}  # term -> main id -> best bm25
    prog = None
    if use_index and longs and fts_available() and index_path().exists():
        prog = progress(set(owner))
        if prog.indexed:
            mode = "index"
            con = _open()
            try:
                # ONE read snapshot for the spans and every word's lookup (WAL): a pass that
                # replaces a session meanwhile is either wholly seen or not at all, so a rowid is
                # never mapped through a span that no longer holds it.
                con.execute("BEGIN")
                spans = _spans(con)
                listed: dict[str, dict[str, float] | None] = {
                    t: _term_keys(con, t, SEARCH_TERM_KEYS_MAX, scores=scores, spans=spans)
                    for t in longs
                }
                common = [t for t, ks in listed.items() if ks is None]
                if common and len(common) == len(longs):
                    mode, degraded = "titles", "common"
                else:
                    for t, ks in listed.items():
                        if ks is not None:
                            conv[t] = _by_main(ks, owner)
                    if common:
                        # Too common to list: checked only within the sessions every listed
                        # word already reaches (conversation or details), so it still constrains.
                        reach = None
                        for t in longs:
                            if t in common:
                                continue
                            mains = set(conv.get(t, {})) | {rid for rid in found if t in found[rid]}
                            reach = mains if reach is None else reach & mains
                        within = [k for k, m in owner.items() if m in (reach or set())]
                        for t in common:
                            ks = _term_keys(
                                con,
                                t,
                                SEARCH_TERM_KEYS_MAX,
                                within=within,
                                scores=scores,
                                spans=spans,
                            )
                            if ks is None:
                                # Too many sessions to check it in, or out of time: degraded, never
                                # an answer that quietly ignored the word.
                                mode, degraded = "titles", "common"
                                conv = {}
                                break
                            conv[t] = _by_main(ks, owner)
            finally:
                con.rollback()
                con.close()
    scores: dict[str, float] = {}
    for t, by_main in conv.items():
        for rid, score in by_main.items():
            found[rid].setdefault(t, set()).add("conversation")
            scores[rid] = min(score, scores.get(rid, score))
    matches: dict[str, Match] = {}
    for r in rows:
        rid = r["id"]
        where = found[rid]
        if not all(t in where for t in ts):
            continue
        if all("title" in where[t] for t in ts):
            matches[rid] = Match("title", "title")
        elif all(where[t] & {"title", "details"} for t in ts):
            fld = max(
                (f for f, _nt in norm_details[rid]),
                key=lambda f: sum(1 for t in ts if t in dict(norm_details[rid]).get(f, "")),
                default="summary",
            )
            matches[rid] = Match("details", fld)
        else:
            matches[rid] = Match("conversation", "you")
    return Result(mode, matches, degraded, prog, has_short, handoffs, scores)


def _by_main(keys: dict[str, float], owner: dict[str, str]) -> dict[str, float]:
    """Session keys (mains and in-scope children) folded onto their main, best bm25 kept. A key
    outside the scoped set has no owner and is dropped here."""
    out: dict[str, float] = {}
    for key, score in keys.items():
        main = owner.get(key)
        if main is not None and (main not in out or score < out[main]):
            out[main] = score
    return out


# ---- snippets ------------------------------------------------------------------------------------


def _window(text: str, ts: list[str]) -> tuple[str, list[tuple[int, int]]]:
    """A `SNIPPET_CHARS` window of ``text`` around its first matched term, with every term
    occurrence in it marked (offsets into the window). Located in the casefolded text, which
    keeps the original's length; a term found only through diacritic folding is not marked."""
    folded = text.casefold() if len(text.casefold()) == len(text) else None
    first = None
    if folded is not None:
        hits = [folded.find(t) for t in ts if folded.find(t) >= 0]
        first = min(hits) if hits else None
    start = max(0, (first or 0) - SNIPPET_CHARS // 3)
    end = min(len(text), start + SNIPPET_CHARS)
    cut = text[start:end]
    marks: list[tuple[int, int]] = []
    if folded is not None:
        fcut = folded[start:end]
        for t in ts:
            i = fcut.find(t)
            while i >= 0:
                marks.append((i, i + len(t)))
                i = fcut.find(t, i + len(t))
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    shift = len(prefix)
    return prefix + cut + suffix, sorted((a + shift, b + shift) for a, b in marks)


def snippet(
    query: str,
    row: dict,
    match: Match,
    children: list[dict] | None = None,
    *,
    reports: dict | None = None,
    titles: dict | None = None,
    handoffs: dict | None = None,
) -> Match:
    """`snippets` for one row."""
    return snippets(
        query, [(row, match, children)], reports=reports, titles=titles, handoffs=handoffs
    )[0]


def snippets(
    query: str,
    shown: list[tuple[dict, Match, list[dict] | None]],
    *,
    reports: dict | None = None,
    titles: dict | None = None,
    handoffs: dict | None = None,
) -> list[Match]:
    """Fill each ``(row, match, children)``'s snippet (only for rows actually shown). The
    COMPLETE message or field goes through the store's boundary first (known secrets redacted,
    credential-shaped text withheld whole), and only then is the window cut — a cut never splits a
    secret. Every conversation snippet of the page comes from ONE read of the index
    (`_best_messages`), not one per row."""
    from . import ask_store

    ts = terms(query)
    groups = {
        row["id"]: [row["id"], *[c["id"] for c in kids or []]]
        for row, match, kids in shown
        if match.tier == "conversation"
    }
    best = _best_messages(groups, ts) if groups else {}
    out: list[Match] = []
    for row, match, kids in shown:
        if match.tier == "title":
            text, marks = _window(row.get("title") or "", ts)
            match.marks = marks if len(text) == len(row.get("title") or "") else []
        elif match.tier == "details":
            text = dict(details(row, reports=reports, titles=titles, handoffs=handoffs)).get(
                match.field, ""
            )
            match = _guarded(match, text, ts, ask_store)
        elif row["id"] in best:
            key, who, text, held = best[row["id"]]
            names = {c["id"]: c.get("title") or "" for c in kids or []}
            match.field = "subagent" if key != row["id"] else who
            match.child = names.get(key) if key != row["id"] else None
            match.more = len([t for t in ts if t not in held])
            if text is None:  # too long to guard whole: withheld, never a cut-first window
                match.withheld = True
                match.snippet, match.marks = "", []
            else:
                match = _guarded(match, text, ts, ask_store)
        out.append(match)
    return out


def _guarded(match: Match, text: str, ts: list[str], ask_store) -> Match:
    try:
        safe = ask_store.guard_text(text, max(len(text), 1))
    except Exception:  # noqa: BLE001 — no boundary: the snippet is withheld, the hit stays
        safe = None
    if not safe or safe == ask_store.WITHHELD:
        match.withheld = True
        match.snippet, match.marks = "", []
        return match
    match.snippet, match.marks = _window(safe, ts)
    return match


#: Above this many sessions, a word's rows are listed once over the whole range the sessions span
#: rather than sought per session (`_held_rows`).
SNIPPET_SEEK_KEYS_MAX = 64


def _held_rows(con, spans: dict[str, tuple[int, int]], longs: list[str]) -> dict[int, set[str]]:
    """Rowid → the terms that row holds, for the rows of these ``spans`` only, from the index's own
    rowids (an FTS5 phrase per term: no content row and no bm25 is read). One seek per session and
    term; past `SNIPPET_SEEK_KEYS_MAX` sessions, one listing per term mapped through the spans."""
    out: dict[int, set[str]] = {}
    if len(spans) <= SNIPPET_SEEK_KEYS_MAX:
        for lo, hi in spans.values():
            for t in longs:
                for (rowid,) in _records(
                    con,
                    "r",
                    "SELECT rowid AS r FROM msg WHERE msg MATCH ? AND rowid BETWEEN ? AND ?",
                    (_quote(t), lo, hi),
                ):
                    out.setdefault(int(rowid), set()).add(t)
        return out
    ranges = sorted(spans.values())
    los = [r[0] for r in ranges]
    lo, hi = ranges[0][0], max(r[1] for r in ranges)
    for t in longs:
        for (text,) in _records(
            con,
            "r",
            "SELECT rowid AS r FROM msg WHERE msg MATCH ? AND rowid BETWEEN ? AND ?",
            (_quote(t), lo, hi),
        ):
            rowid = int(text)
            i = bisect.bisect_right(los, rowid) - 1
            if i >= 0 and rowid <= ranges[i][1]:
                out.setdefault(rowid, set()).add(t)
    return out


def _best_messages(
    groups: dict[str, list[str]], ts: list[str]
) -> dict[str, tuple[str, str, str | None, set[str]]]:
    """For each group of keys (a main row and its in-scope children), the message holding the
    most distinct terms across its parts; ties: the lowest bm25 (the best match), then the newest
    message. Its text is the WHOLE message, its parts joined (None when longer than
    `SNIPPET_MESSAGE_CHARS_MAX`: withheld, never cut first).

    Built for a whole page at once, in ONE read snapshot (the spans, the matches and the parts are
    of the same rows). Which rows hold which term comes from the index alone (`_held_rows`); the
    matched rows' small columns group their parts into messages; bm25 — whose every statement
    first counts each term over the WHOLE index — is asked once for the page, and only of the
    messages that tie; and only each chosen message's text is read."""
    longs = [t for t in ts if len(t) >= SHORT_TERM]
    if not longs or not fts_available() or not index_path().exists():
        return {}
    con = _open()
    out: dict[str, tuple[str, str, str | None, set[str]]] = {}
    try:
        con.execute("BEGIN")
        keys = sorted({k for ks in groups.values() for k in ks})
        spans: dict[str, tuple[int, int]] = {}
        for i in range(0, len(keys), 900):
            chunk = keys[i : i + 900]
            marks = ",".join("?" * len(chunk))
            for key, lo, hi in con.execute(
                f"SELECT key, lo, hi FROM span WHERE key IN ({marks})",  # noqa: S608 — placeholders only
                chunk,
            ):
                spans[key] = (int(lo), int(hi))
        held = _held_rows(con, spans, longs)
        if not held:
            return {}
        # The matched rows' messages: (key, seq) → who, the terms its parts hold, its rows.
        msgs: dict[tuple[str, int], dict] = {}
        ids = sorted(held)
        for i in range(0, len(ids), 900):
            chunk = ids[i : i + 900]
            marks = ",".join("?" * len(chunk))
            for rid, k, who, seq, part, parts in _records(
                con,
                "r|key|who|seq|part|parts",
                f"SELECT rowid AS r, key, who, seq, part, parts FROM msg WHERE rowid IN ({marks})",  # noqa: S608 — placeholders only
                chunk,
            ):
                rowid = int(rid)
                m = msgs.setdefault(
                    (k, int(seq)),
                    {"who": who, "held": set(), "rows": [], "first": rowid - int(part),
                     "parts": int(parts)},
                )  # fmt: skip
                m["held"] |= held[rowid]
                m["rows"].append(rowid)
        by_key: dict[str, list] = {}
        for (k, seq), m in msgs.items():
            by_key.setdefault(k, []).append(((k, seq), m))
        tops: dict[str, list] = {}
        tied: set[int] = set()
        for main, ks in groups.items():
            cand = [x for k in ks for x in by_key.get(k, ())]
            if not cand:
                continue
            most = max(len(m["held"]) for _id, m in cand)
            tops[main] = [x for x in cand if len(x[1]["held"]) == most]
            if len(tops[main]) > 1:
                tied.update(r for _id, m in tops[main] for r in m["rows"])
        score: dict[int, float] = {}
        if tied:
            expr = " OR ".join(_quote(t) for t in longs)
            for rid, s in _records(
                con,
                "r|s",
                "SELECT rowid AS r, printf('%.17g', bm25(msg)) AS s FROM msg"
                " WHERE msg MATCH ? AND rowid BETWEEN ? AND ?",
                (expr, min(tied), max(tied)),
            ):
                if int(rid) in tied:
                    score[int(rid)] = _float(s)
        for main, top in tops.items():
            (key, _seq), best = min(
                top,
                key=lambda kv: (min(score.get(r, 0.0) for r in kv[1]["rows"]), -kv[0][1]),
            )
            if best["parts"] * CHUNK_CHARS > SNIPPET_MESSAGE_CHARS_MAX * 2:
                text = None  # far too long to guard: withheld without reading it
            else:
                text = "".join(
                    r[0]
                    for r in con.execute(
                        "SELECT text FROM msg WHERE rowid BETWEEN ? AND ? ORDER BY rowid",
                        (best["first"], best["first"] + best["parts"] - 1),
                    ).fetchall()
                )
                if len(text) > SNIPPET_MESSAGE_CHARS_MAX:
                    text = None
            out[main] = (key, best["who"], text, best["held"])
    finally:
        con.rollback()
        con.close()
    return out


# ---- HQ's fallback candidates -----------------------------------------------------------------


def candidates(query: str, rows: list[dict], children=None, *, reports=None, titles=None) -> list:
    """For a query with NO literal hit: the main rows matching at least one word, ranked by how
    many distinct words they hold, then tier, then the lowest bm25, then the newest — at most
    `CANDIDATES_MAX`. Sessions matching no word are never added."""
    ts = terms(query)
    scored: list[tuple] = []
    per_term: dict[str, Result] = {
        t: search(t, rows, children, reports=reports, titles=titles, scores=True) for t in ts
    }
    by_id = {r["id"]: r for r in rows}
    for rid, r in by_id.items():
        hits = [res.matches[rid] for res in per_term.values() if rid in res.matches]
        if not hits:
            continue
        tier = min(_TIER_RANK[m.tier] for m in hits)
        best = min((res.scores[rid] for res in per_term.values() if rid in res.scores), default=0.0)
        scored.append((-len(hits), tier, best, -float(r.get("last_mtime") or 0), rid))
    scored.sort()
    return [by_id[s[-1]] for s in scored[:CANDIDATES_MAX]]


if __name__ == "__main__":
    raise SystemExit(main())
