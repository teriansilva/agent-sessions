"""Engine-agnostic conversation-transcript renderer for scroll-up history (issue #242).

Raw PTY-byte scrollback is width-fragile: it stores the literal screen-drawing escapes (absolute
cursor moves baked to the width they were authored at), so a reattach at a different width
garbles / duplicates / loses the history — and there is no faithful way to reflow an
absolute-positioned grid to a narrower screen (proved by the reverted pyte attempt, PR #248/#249).

Instead, render scroll-up from the engine's OWN saved conversation — the real messages it persists
for ``resume``/``continue`` (Claude's ``*.jsonl``, codex rollout JSONL, opencode's SQLite
``message``/``part`` tables, gemini's ``tmp/<hash>/chats/session-*.jsonl``). That's *semantic
text*: it wraps cleanly at any width, is fast (no escape parsing), and can't misfire — there are no
cursor escapes in it. The live terminal then owns only the current frame.

Two layers, so adding an engine is cheap:

1. A per-engine **adapter** reads that engine's store → a common list of :class:`Turn`. This is the
   ONLY engine-specific code; adapters register in `_ADAPTERS` keyed by the same engine id as
   ``engines.py``. Claude is implemented here; codex / opencode / gemini / future engines register
   their own. An engine with no usable transcript store simply has no adapter and the caller falls
   back to the existing raw-byte path.
2. ONE shared **renderer** (:func:`render`) turns ``Turn``s into flat, wrapped ANSI at the
   requested width — written once, used by every engine.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# --- common conversation model -------------------------------------------------------------

# How many bytes of a tool-call argument / tool-result to show before eliding.
_ARG_MAX = 60
_RESULT_MAX = 240


@dataclass
class Turn:
    """One renderable unit of a conversation, engine-decoded into plain text.

    ``role``: "user" | "assistant" | "system" | "tool".
    ``kind``: "text" (a message) | "tool" (a one-line tool-call summary) | "result"
    (a truncated tool result). The renderer styles by ``kind``/``role``; everything else is text.
    ``ts``: when the engine recorded the message (epoch seconds), or ``None`` where it does not say.
    Set on text turns; it plays no part in equality, so a Turn compares by what it renders.
    """

    role: str
    text: str
    kind: str = "text"
    ts: float | None = field(default=None, compare=False, repr=False)


def _when(value: object) -> float | None:
    """An engine's record time as epoch seconds — an ISO-8601 string, or a number in seconds or
    milliseconds — or ``None`` when it is absent or unreadable."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.timestamp() if dt.tzinfo is not None else None
    return None


def _short(value: object, limit: int = _ARG_MAX) -> str:
    s = value if isinstance(value, str) else json.dumps(value, default=str)
    s = " ".join(s.split())  # collapse whitespace/newlines to one line
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _result_text(content: object) -> str:
    """Pull the readable text out of a tool_result ``content`` instead of dumping its JSON wrapper
    (#260). Claude stores results as a bare string, or a list of ``{"type":"text","text":…}`` blocks
    — the latter was being ``json.dumps``'d, so the scroll-up showed ``[{"type":"text",…}]`` noise.
    str → itself; list/dict of text blocks → their joined text; anything else → compact JSON so a
    result never renders empty."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                t = p.get("text") or p.get("content")
                if isinstance(t, str):
                    parts.append(t)
                elif p.get("type"):  # image / tool_reference / … → tag, never dump the blob
                    parts.append(f"[{p['type']}]")
        if parts:
            return "\n".join(parts)
    if isinstance(content, dict):
        t = content.get("text") or content.get("content")
        if isinstance(t, str):
            return t
        if content.get("type"):
            return f"[{content['type']}]"
    return json.dumps(content, default=str) if content else ""


# Render the common Markdown to ANSI (#301) so the scroll-up reads like the real console: bold,
# inline code, and headings are STYLED (not stripped). Wrapping is ANSI-aware (see _wrap), so the
# inline escapes don't break width.
_MD_FENCE = re.compile(r"^[ \t]*```[^\n]*$", re.M)  # ```code-fence``` lines → removed (code kept)
_MD_HEAD = re.compile(r"^([ \t]*)#{1,6}[ \t]+(.+?)[ \t]*$", re.M)  # "### Heading" → bold heading
_MD_BULLET = re.compile(r"^([ \t]*)[-*][ \t]+", re.M)  # "- item" / "* item" → "• item"
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)  # **bold** → ANSI bold
_MD_STRIKE = re.compile(r"~~(.+?)~~", re.S)  # ~~strike~~ → text
_MD_CODE = re.compile(r"`([^`]+)`")  # `code` → ANSI cyan
# *italic* / _italic_ — single delimiter, matched (backref) pair, applied AFTER bold so ** is gone;
# not adjacent to a word char (so it won't fire on a*b math) and no inner edge whitespace.
_MD_ITALIC = re.compile(r"(?<![\w*])([*_])(?!\s)([^*_\n]+?)(?<!\s)\1(?![\w*])")

_SGR_BOLD = "\x1b[1m"
_SGR_BOLD_OFF = "\x1b[22m"
_SGR_ITALIC = "\x1b[3m"
_SGR_ITALIC_OFF = "\x1b[23m"
_SGR_CODE = "\x1b[36m"
# Bold amber (256-color 214 ≈ the app's #ffb000 accent) for the user-turn gutter marker.
_SGR_USER_MARK = "\x1b[1;38;5;214m"
_SGR_MARK_OFF = "\x1b[22;39m"
_SGR_CODE_OFF = "\x1b[39m"


def _render_md(text: str) -> str:
    text = _MD_FENCE.sub("", text)
    text = _MD_HEAD.sub(r"\1" + _SGR_BOLD + r"\2" + _SGR_BOLD_OFF, text)
    text = _MD_BOLD.sub(_SGR_BOLD + r"\1" + _SGR_BOLD_OFF, text)
    text = _MD_ITALIC.sub(_SGR_ITALIC + r"\2" + _SGR_ITALIC_OFF, text)
    text = _MD_STRIKE.sub(r"\1", text)
    text = _MD_CODE.sub(_SGR_CODE + r"\1" + _SGR_CODE_OFF, text)
    text = _MD_BULLET.sub(r"\1• ", text)
    return text


# --- shared renderer -----------------------------------------------------------------------

_SGR_ASSISTANT = "\x1b[1;32m"  # bright green ● dot for the assistant turn
_SGR_DIM = "\x1b[90m"  # grey         tool calls / results
_RESET = "\x1b[0m"
# User messages render as a grey-background block (like the real console), filled to the terminal
# width so the band spans the whole line.
_SGR_USER_BG = "\x1b[48;5;238m"
_BG_OFF = "\x1b[49m"


def _bg_block(lines: list[str], width: int) -> list[str]:
    """User turn: grey background band with a bold amber ``❯`` gutter on the first line.

    The band alone read ambiguously in long sessions ("not clear what I wrote and what
    the agent wrote") — the marker mirrors the assistant's green ● so the two voices are
    distinguishable at a glance even when a band spans many wrapped lines. Continuations
    indent 2 under the marker; every line keeps the full-width band."""
    out: list[str] = []
    for i, ln in enumerate(lines):
        gutter = (_SGR_USER_MARK + "❯" + _SGR_MARK_OFF + " ") if i == 0 else "  "
        body = gutter + ln
        pad = " " * max(0, width - _vis_len(body))
        out.append(_SGR_USER_BG + body + pad + _BG_OFF)
    return out


def _dot_block(text: str, width: int) -> list[str]:
    """Assistant turn: first line prefixed with a green ● dot, continuations hanging-indented 2."""
    wrapped = _wrap(text, width, indent="  ") or [""]
    wrapped[0] = _SGR_ASSISTANT + "●" + _RESET + " " + wrapped[0][2:]
    return wrapped


# Default bounds (Hermes #242: bound history rows + input messages independently of raw caps).
# Env-overridable and raised (#348 Phase 2): the old 400/4000 caps made days-old sessions
# render a thin slice — the operator-visible "tiny scrollback". Render runs in the thread
# pool and the output is bounded by these, so deeper defaults are paid only on attach.


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


DEFAULT_MAX_MESSAGES = _env_int("AGENT_SESSIONS_TRANSCRIPT_MAX_MESSAGES", 2000)
DEFAULT_MAX_LINES = _env_int("AGENT_SESSIONS_TRANSCRIPT_MAX_LINES", 20000)


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _vis_len(s: str) -> int:
    """Visible width of ``s`` (ANSI SGR escapes are zero-width)."""
    return len(_ANSI_RE.sub("", s))


def _split_long(word: str, width: int) -> list[str]:
    """Hard-break a word wider than ``width`` VISIBLE columns, keeping ANSI escapes attached."""
    out: list[str] = []
    cur, vis, i = "", 0, 0
    while i < len(word):
        m = _ANSI_RE.match(word, i)
        if m:
            cur += m.group()
            i = m.end()
            continue
        if vis >= width:
            out.append(cur)
            cur, vis = "", 0
        cur += word[i]
        vis += 1
        i += 1
    if cur:
        out.append(cur)
    return out


def _wrap(text: str, width: int, indent: str = "") -> list[str]:
    """Word-wrap ``text`` to ``width`` VISIBLE columns (ANSI SGR escapes count as zero-width), per
    paragraph, preserving blank lines. ANSI-aware so rendered-markdown bold/code escapes don't break
    the wrap.

    Note: counts code points for visible width, so a run of double-width glyphs (CJK) can be
    slightly wide — bounded, rare in code transcripts; a wcwidth-aware wrap is a later refinement.
    """
    out: list[str] = []
    avail = max(1, width - len(indent))
    for para in text.split("\n"):
        if not _ANSI_RE.sub("", para).strip():
            out.append("")
            continue
        cur, vis = "", 0
        for word in para.split(" "):
            for piece in _split_long(word, avail) if _vis_len(word) > avail else [word]:
                pv = _vis_len(piece)
                if cur and vis + 1 + pv > avail:
                    out.append(indent + cur)
                    cur, vis = "", 0
                if cur:
                    cur += " " + piece
                    vis += 1 + pv
                else:
                    cur, vis = piece, pv
        out.append(indent + cur)
    return out


def render(
    turns: list[Turn],
    cols: int,
    *,
    assistant_label: str = "Agent",
    max_lines: int | None = None,
) -> bytes:
    """Render ``turns`` as flat, wrapped ANSI for injection as xterm scrollback at ``cols`` wide.

    Width-correct at any width (it wraps plain text — no cursor escapes). Bounded to the last
    ``max_lines`` rendered lines (``None`` → the live :data:`DEFAULT_MAX_LINES`). Returns UTF-8
    bytes; the caller decides framing (e.g. a leading clear + a trailing separator before the
    live frame). Empty input → ``b""``.
    """
    return render_with_boundary(turns, cols, assistant_label=assistant_label, max_lines=max_lines)[
        0
    ]


def render_with_boundary(
    turns: list[Turn],
    cols: int,
    *,
    assistant_label: str = "Agent",
    max_lines: int | None = None,
) -> tuple[bytes, int]:
    """:func:`render`, plus the EXACT turn boundary the output covers.

    The second element is the smallest index ``N`` such that every turn from ``N`` on is fully
    present in the rendered text — i.e. ``turns[:N]`` were truncated away by ``max_lines``. When
    the cap sliced INTO a turn, that turn counts as NOT covered (``N`` is one past it), so a
    history pager requesting ``before=N`` re-serves it whole instead of losing its head; a
    one-turn overlap on screen beats a hole. ``N == 0`` ⇔ nothing was truncated.

    This is the attach-side source of the ``{"t":"hist","cursor":N}`` control frame (Hermes #365
    r2 finding 1): the renderer that BUILT the attach payload is the only place that knows the
    exact boundary, so it exports it instead of the history endpoint re-deriving it later from
    rendered line counts at whatever width that request happens to carry (a resize between
    attach and first lazy-load made the re-derived boundary skip turns).
    """
    if max_lines is None:
        max_lines = DEFAULT_MAX_LINES  # live attr so tests/operators can tune it
    cols = max(20, cols)
    lines: list[str] = []
    owner: list[int] = []  # lines[i] was emitted by turns[owner[i]] — the boundary's map
    for ti, t in enumerate(turns):
        emitted = len(lines)
        text = (t.text or "").rstrip()
        if not text:
            continue
        if t.kind == "tool":
            lines.append(_SGR_DIM + "  ⎿ " + _short(text.splitlines()[0], cols - 6) + _RESET)
        elif t.kind == "result":
            # One short dimmed line — the first non-blank line, truncated (#260). Results are
            # context, not the focus; the live frame has the full thing.
            first = next((ln for ln in text.splitlines() if ln.strip()), "")
            if first:
                lines.append(_SGR_DIM + "    ⎿ " + _short(first, cols - 7) + _RESET)
        elif t.role == "user":
            # User turn: a grey-background block (like the real console), no "You" label (#301).
            lines.append("")
            lines.extend(_bg_block(_wrap(_render_md(text), max(10, cols - 2)), cols))
        else:  # assistant / system
            # Assistant turn: a green ● dot + rendered markdown, no "Claude" label (#301).
            lines.append("")
            lines.extend(_dot_block(_render_md(text), cols))
        owner.extend([ti] * (len(lines) - emitted))
    if not lines:
        return b"", 0
    boundary = 0
    if len(lines) > max_lines:
        cut = len(lines) - max_lines  # index of the first SURVIVING line
        first_kept = owner[cut]
        # Cut mid-turn → that turn's head is gone: it is not covered, boundary is one past it.
        boundary = first_kept if owner[cut - 1] != first_kept else first_kept + 1
        lines = lines[-max_lines:]
    return "\r\n".join(lines).encode("utf-8", "replace"), boundary


# --- per-engine adapters -------------------------------------------------------------------

# An adapter resolves + reads one engine's store for a native session id and returns its Turns.
# Signature: (native_id, home) -> list[Turn]. `home` is injectable for testing. Bounded by
# `max_messages` inside each adapter so a huge transcript never balloons.
TranscriptAdapter = Callable[[str, Path], list[Turn]]
#: Every registry below is keyed by the manifest's `transcript.kind` (#853 P3), never an engine id:
#: an adapter is a KIND — reviewed code shaped by one store format — and which engine reads through
#: it is that engine's manifest's business. Callers still ask by engine id.
_ADAPTERS: dict[str, TranscriptAdapter] = {}


def _kind(engine_id: str) -> str | None:
    from . import engines

    m = engines.manifest_of(engine_id)
    return m.transcript_kind if m is not None and m.transcript_kind != "none" else None


def register_adapter(kind: str, adapter: TranscriptAdapter) -> None:
    """Register the adapter for a `transcript.kind`."""
    _ADAPTERS[kind] = adapter


def _scoped(fn, engine_id: str, empty):
    """Run a kind's reader AGAINST ``engine_id``'s OWN store (#853 P3): the kind is shared, the
    store is not. A store the engine does not declare reads as nothing, never as another's."""
    from .engines import base

    if fn is None:
        return None

    def call(native_id: str, home: Path):
        with base.store_scope(engine_id):
            try:
                return fn(native_id, home)
            except base.EngineError:
                return empty

    call.__wrapped__ = fn  # type: ignore[attr-defined]
    return call


def adapter_for(engine_id: str) -> TranscriptAdapter | None:
    """The adapter ``engine_id``'s manifest selects, reading ``engine_id``'s store — or ``None``
    (→ the raw-byte path)."""
    k = _kind(engine_id)
    return _scoped(_ADAPTERS.get(k), engine_id, []) if k else None


# A STRICT reader is the same read with one difference: it RAISES where the adapter above degrades
# to ``[]``. The adapters are fail-soft on purpose — a locked or corrupt store must never take down
# the sidebar, a review or a history view — but a caller deciding "which session did this launch
# become" (`launch_binding.bind_by_nonce`) must be able to tell "this session has no turn yet" from
# "this session could not be read", because #989's rule is that unreadable is not absent (review
# comment 72377 finding 3). An engine whose adapter already raises on a failed read needs none.
_STRICT_ADAPTERS: dict[str, TranscriptAdapter] = {}


def register_strict_adapter(kind: str, adapter: TranscriptAdapter) -> None:
    """Register the read for a `transcript.kind` that raises instead of returning ``[]``."""
    _STRICT_ADAPTERS[kind] = adapter


def strict_adapter_for(engine_id: str) -> TranscriptAdapter | None:
    """The strict reader when ``engine_id``'s manifest declares `transcript.strict`, else its
    ordinary adapter."""
    from . import engines

    m = engines.manifest_of(engine_id)
    k = _kind(engine_id)
    strict = _STRICT_ADAPTERS.get(k) if k and m is not None and m.transcript_strict else None
    if strict is None:
        return adapter_for(engine_id)
    from .engines import base

    def call(native_id: str, home: Path):
        # No EngineError → [] here: a STRICT read must raise rather than read as empty.
        with base.store_scope(engine_id):
            return strict(native_id, home)

    call.__wrapped__ = strict  # type: ignore[attr-defined]
    return call


# Where an engine keeps a session's FULL transcript, as an agent-readable location string (#716).
# Deliberately separate from the adapters: an adapter *parses* a transcript, a locator only
# *names where it lives*, so a handoff seed can point the receiving agent at the history the
# capped seed had to drop.
#
# Signature ``(native_id, home) -> str | None``. ``None`` means **the session did not resolve** —
# not merely that the store is missing. A store shared by many sessions (opencode's SQLite DB)
# must confirm rows for *this* id, and glob-backed engines must match the id EXACTLY: a
# same-prefix neighbour is a different session, and naming it would point the target agent at
# someone else's history. Callers omit the reference entirely on ``None`` — never a guess.
SourceLocator = Callable[[str, Path], str | None]
_LOCATORS: dict[str, SourceLocator] = {}


def register_locator(kind: str, locator: SourceLocator) -> None:
    """Register the locator for a `transcript.kind`."""
    _LOCATORS[kind] = locator


def locator_for(engine_id: str) -> SourceLocator | None:
    """The locator ``engine_id``'s manifest selects, or ``None`` (engine can't be located)."""
    k = _kind(engine_id)
    return _scoped(_LOCATORS.get(k), engine_id, None) if k else None


def source_location(engine_id: str, native_id: str, home: Path) -> str | None:
    """Where ``engine_id``'s session ``native_id`` keeps its full transcript, or ``None`` when the
    session doesn't resolve / the engine has no locator. Fail-soft by construction: a locator that
    raises yields ``None``, so an unreadable or corrupt store can never break a handoff."""
    fn = locator_for(engine_id)
    if fn is None:
        return None
    try:
        return fn(native_id, home)
    except Exception:
        return None


#: A monotonic, session-scoped measure of how much an engine has written. Used ONLY as a growth
#: signal — its absolute value is meaningless and must never be compared across engines.
GrowthMark = Callable[[str, Path], "int | None"]
_GROWTH: dict[str, GrowthMark] = {}


def register_growth(kind: str, fn: GrowthMark) -> None:
    """Register the growth signal for a `transcript.kind`."""
    _GROWTH[kind] = fn


def growth_for(engine_id: str) -> GrowthMark | None:
    k = _kind(engine_id)
    return _scoped(_GROWTH.get(k), engine_id, None) if k else None


def growth_mark(engine_id: str, native_id: str, home: Path) -> int | None:
    """How much ``engine_id``'s session ``native_id`` has written, or ``None``.

    **Why this exists rather than counting rendered turns.** The renderers cap at
    :data:`DEFAULT_MAX_MESSAGES`, so a busy session pinned at the cap has a count that stops moving
    while the session is perfectly healthy — a stall detector built on it reports the opposite of
    the truth. A growth signal has to be monotonic and uncapped.

    **And why not the locator.** `source_location` is prose for the operator: opencode's returns
    the shared database plus the query to run, deliberately, because there is no per-session file.
    Sizing `Path(that_string)` therefore always fails for opencode and silently falls back to the
    capped count — which looked like it worked, because every file-backed engine took the other
    branch. A per-engine signal makes each provider say what its own monotonic measure is.

    Fail-soft by construction: any error yields ``None`` and the caller treats the session as
    unmeasurable rather than stalled.
    """
    fn = growth_for(engine_id)
    if fn is None:
        return None
    try:
        return fn(native_id, home)
    except Exception:
        return None


def _path_growth(resolve: Callable[[str, Path], Path | None]) -> GrowthMark:
    """Size on disk, for the file-backed engines. A transcript file only ever grows."""

    def mark(native_id: str, home: Path) -> int | None:
        path = resolve(native_id, home)
        if path is None:
            return None
        try:
            return int(Path(path).stat().st_size)
        except OSError:
            return None

    return mark


def _path_locator(resolve: Callable[[str, Path], Path | None]) -> SourceLocator:
    """Adapt a ``(native_id, home) -> Path | None`` resolver into a locator. Used for the
    file-backed engines, whose resolvers already enforce exact-id matching."""

    def locate(native_id: str, home: Path) -> str | None:
        path = resolve(native_id, home)
        return str(path) if path is not None else None

    return locate


# Read at most this many bytes from the END of a transcript. We only need the last
# `max_messages`, and even a few hundred KB of JSONL holds far more than that — so a multi-MB
# transcript parses in ~the same time as a small one (keeps the parse well under budget, #242).
_TAIL_BYTES = _env_int("AGENT_SESSIONS_TRANSCRIPT_TAIL_BYTES", 8 * 1024 * 1024)


def claude_turns_from_jsonl(path: Path, *, max_messages: int = DEFAULT_MAX_MESSAGES) -> list[Turn]:
    """Parse a Claude Code session JSONL into Turns. ``message.content`` is a str or a list of
    ``text`` / ``thinking`` / ``tool_use`` / ``tool_result`` blocks; ``thinking`` is hidden, tool
    calls become one-line summaries, tool results are truncated. Only the last ``_TAIL_BYTES`` are
    read (from the next line boundary), so a huge transcript stays fast. Best-effort: unreadable
    file / bad lines are skipped (→ ``[]`` / partial)."""
    try:
        with path.open("rb") as fh:
            size = path.stat().st_size
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # discard the (likely partial) first line after the seek
            data = fh.read()
    except OSError:
        return []
    recs: list[dict] = []
    for raw in data.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        if isinstance(o, dict) and o.get("type") in ("user", "assistant"):
            recs.append(o)
    turns: list[Turn] = []
    for o in recs[-max_messages:]:
        msg = o.get("message") or {}
        role = msg.get("role") or o.get("type") or "assistant"
        content = msg.get("content")
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content or ""}]
        for b in blocks:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text" and (b.get("text") or "").strip():
                turns.append(Turn(role, b["text"].strip(), "text", _when(o.get("timestamp"))))
            elif bt == "tool_use":
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                arg = (
                    inp.get("command")
                    or inp.get("file_path")
                    or inp.get("pattern")
                    or inp.get("description")
                    or inp.get("path")
                    or ""
                )
                turns.append(Turn(role, f"{b.get('name', 'tool')}({_short(arg)})", "tool"))
            elif bt == "tool_result":
                txt = _result_text(b.get("content"))
                if txt.strip():
                    turns.append(Turn("tool", txt, "result"))
            # "thinking" blocks are intentionally omitted from scroll-up.
    return turns


def claude_jsonl_path(native_id: str, home: Path) -> Path | None:
    """Resolve a Claude session id to its JSONL under ``home/.claude/projects/*/`` (or
    ``projects-archive`` for archived sessions), or ``None``. The cwd-encoded project dir isn't
    known from the id alone, so glob — but for the EXACT ``<id>.jsonl`` filename, so the match is
    always that one session and never a same-prefix neighbour."""
    from .engines import base

    for name in ("sessions", "archive"):
        # The `claude-projects` store's named paths — the requesting engine's own when scoped.
        try:
            root = base._store("claude-projects", name, home)
        except base.EngineError:
            continue
        try:
            match = next(root.glob(f"*/{native_id}.jsonl"), None)
        except OSError:
            match = None
        if match is not None:
            return match
    return None


def _claude_adapter(native_id: str, home: Path) -> list[Turn]:
    """Resolve a Claude session id to its JSONL and parse it (``[]`` when it doesn't resolve)."""
    path = claude_jsonl_path(native_id, home)
    return claude_turns_from_jsonl(path) if path is not None else []


register_adapter("claude-jsonl", _claude_adapter)
register_locator("claude-jsonl", _path_locator(claude_jsonl_path))
register_growth("claude-jsonl", _path_growth(claude_jsonl_path))


def _read_tail(path: Path) -> bytes:
    """Read the last ``_TAIL_BYTES`` of ``path`` from the next line boundary (so a huge JSONL
    transcript parses in ~constant time — we only need the last ``max_messages``). Fail-soft: an
    unreadable file yields ``b""``."""
    try:
        with path.open("rb") as fh:
            size = path.stat().st_size
            if size > _TAIL_BYTES:
                fh.seek(size - _TAIL_BYTES)
                fh.readline()  # discard the (likely partial) first line after the seek
            return fh.read()
    except OSError:
        return b""


def _jsonl_dicts(data: bytes) -> list[dict]:
    """Every JSON-object line in ``data`` (blank / unparseable lines skipped)."""
    out: list[dict] = []
    for raw in data.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            o = json.loads(raw)
        except ValueError:
            continue
        if isinstance(o, dict):
            out.append(o)
    return out


# --- codex --------------------------------------------------------------------------------


def codex_rollout_path(native_id: str, home: Path) -> Path | None:
    """Resolve a codex session id to its rollout JSONL under ``<codex-sessions>/<date>/`` (the date
    dir isn't known from the id → glob ``rollout-*<id>.jsonl``). The sessions dir is the same
    env-overridable location the provider discovers from (``base._codex_sessions_dir``), so a
    configured store renders in scroll-up too — not just in the sidebar."""
    from .engines import base

    try:
        return next(base._codex_sessions_dir(home).glob(f"**/rollout-*{native_id}.jsonl"), None)
    except OSError:
        return None


def _codex_turns_from_records(recs: list[dict]) -> list[Turn]:
    """Flatten codex rollout ``response_item`` records into Turns (user/assistant messages, function
    calls + their output). ``reasoning`` (hidden thinking) and developer/system messages are
    skipped."""
    from .engines.codex import is_injected_context

    turns: list[Turn] = []
    for o in recs:
        if o.get("type") != "response_item":
            continue
        p = o.get("payload") or {}
        pt = p.get("type")
        if pt == "message":
            role = p.get("role")
            if role not in ("user", "assistant"):
                continue
            text = " ".join(
                b.get("text", "")
                for b in (p.get("content") or [])
                if isinstance(b, dict) and b.get("text")
            ).strip()
            # Codex injects machine context as plain "user" messages — the XML preamble
            # (`<environment_context>` / `<user_instructions>`) and, since 0.142.5, the
            # `# AGENTS.md instructions` block (#670). Shared predicate with the provider's
            # title fallback so the two surfaces can't drift.
            if text and not is_injected_context(text):
                turns.append(Turn(role, text, "text", _when(o.get("timestamp"))))
        elif pt == "function_call":
            arg = p.get("arguments") or p.get("name", "")
            turns.append(Turn("assistant", f"{p.get('name', 'tool')}({_short(arg)})", "tool"))
        elif pt == "function_call_output":
            txt = _result_text(p.get("output"))
            if txt.strip():
                turns.append(Turn("tool", txt, "result"))
    return turns


def _codex_adapter(native_id: str, home: Path) -> list[Turn]:
    path = codex_rollout_path(native_id, home)
    if path is None:
        return []
    return _codex_turns_from_records(_jsonl_dicts(_read_tail(path)))[-DEFAULT_MAX_MESSAGES:]


register_adapter("codex-rollout", _codex_adapter)
register_locator("codex-rollout", _path_locator(codex_rollout_path))
register_growth("codex-rollout", _path_growth(codex_rollout_path))


# --- kimi ---------------------------------------------------------------------------------


def kimi_wire_path(native_id: str, home: Path) -> Path | None:
    """Resolve a Kimi session id to its ``agents/main/wire.jsonl`` transcript, or ``None`` (#720).

    Goes through the provider's single exact-session seam (``engines.kimi.session_dir_for``) — the
    SAME resolver ``scan``/reconcile use — so the adapter, the locator, and the sidebar can never
    disagree about which dir a session lives in. ``session_dir_for`` already validates the
    ``session_<uuid>`` shape and rejects same-prefix neighbours, so a bare/junk id yields ``None``
    rather than a bogus path. v1 reads the ``main`` agent only (Swarm sub-agents are out of scope).
    """
    from .engines.kimi import session_dir_for

    sdir = session_dir_for(native_id, home)
    if sdir is None:
        return None
    wire = sdir / "agents" / "main" / "wire.jsonl"
    try:
        return wire if wire.is_file() else None
    except OSError:
        return None


def _kimi_input_text(parts: object) -> str:
    """Join the ``text`` chunks of a Kimi ``turn.prompt`` ``input`` (``[{type:"text", text}]``)."""
    if not isinstance(parts, list):
        return ""
    return "".join(
        p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)
    ).strip()


def _kimi_turns_from_wire(recs: list[dict]) -> list[Turn]:
    """Flatten Kimi's ``wire.jsonl`` loop-event stream into Turns (#720).

    Kimi's transcript is NOT a flat message list — it's a stream of records keyed by turn/step:

    * ``turn.prompt`` — the **only** source of user Turns. ``context.append_message`` re-appends the
      same prompt (and the injected permission-mode reminders), so parsing it too would double-count
      the user's message; we take ``turn.prompt`` and ignore ``append_message`` entirely. Records
      whose ``origin.kind == "injection"`` are machine context (codex #670 class) and are skipped.
    * ``context.append_loop_event`` carries the assistant + tools: inner ``content.part`` of
      ``part.type == "text"`` is the visible answer (``"think"`` is hidden reasoning — excluded),
      ``tool.call`` is a one-line call summary, ``tool.result`` is the (truncated) output.

    Assistant ``text`` parts are buffered and flushed as ONE assistant Turn at each boundary (a tool
    call/result or the next user prompt), so a multi-step answer renders as a single message rather
    than fragments — and the ``DEFAULT_MAX_MESSAGES`` tail can never split one mid-way. Fail-soft:
    an unparseable line was already dropped by ``_jsonl_dicts``; a malformed record is ignored here.
    """
    turns: list[Turn] = []
    pending: list[str] = []  # assistant text chunks awaiting a flush

    def flush() -> None:
        if pending:
            text = "".join(pending).strip()
            pending.clear()
            if text:
                turns.append(Turn("assistant", text, "text"))

    for o in recs:
        # Per-record fail-soft (#720): a schema-drift record with a wrong-shaped nested value
        # (``origin``/``event``/``part``/``result`` a string/list instead of an object) must skip
        # only THAT record, never blank the whole transcript. isinstance-guard each nested access,
        # with a belt-and-suspenders ``except`` so no unforeseen shape can propagate out.
        try:
            rtype = o.get("type")
            if rtype == "turn.prompt":
                origin = o.get("origin")
                # A wrong-shaped ``origin`` (present but not a mapping) can't be checked against the
                # injection filter, so we can't prove the prompt ISN'T machine context — skip it
                # rather than promote malformed context to a user turn (per-record fail-soft). A
                # missing ``origin`` is fine (defaults to a real user prompt).
                if origin is not None and not isinstance(origin, dict):
                    continue
                if isinstance(origin, dict) and origin.get("kind") == "injection":
                    continue
                text = _kimi_input_text(o.get("input"))
                if text:
                    flush()  # close the previous assistant turn before the new user turn
                    turns.append(Turn("user", text, "text", _when(o.get("time"))))
            elif rtype == "context.append_loop_event":
                event = o.get("event")
                if not isinstance(event, dict):
                    continue
                etype = event.get("type")
                if etype == "content.part":
                    part = event.get("part")
                    if (
                        isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                    ):
                        pending.append(part["text"])
                    # part.type == "think" → hidden reasoning, excluded
                elif etype == "tool.call":
                    flush()
                    name = event.get("name") or "tool"
                    args = event.get("args")
                    turns.append(
                        Turn(
                            "assistant",
                            f"{name}({_short(args if args is not None else name)})",
                            "tool",
                        )
                    )
                elif etype == "tool.result":
                    flush()
                    result = event.get("result")
                    output = result.get("output") if isinstance(result, dict) else None
                    txt = _result_text(output)
                    if txt.strip():
                        turns.append(Turn("tool", txt, "result"))
            # every other record type (metadata, config.update, llm.*, usage.*, permission.*, …)
            # is framing/telemetry, not transcript content — ignored.
        except (AttributeError, TypeError, KeyError, ValueError, IndexError):
            # Any residual data-shape error past the isinstance guards → skip this record only.
            continue
    flush()
    return turns


def _kimi_cap(turns: list[Turn]) -> list[Turn]:
    """Cap to the last ``DEFAULT_MAX_MESSAGES`` **reconstructed Turns** (not raw records — Kimi
    emits many records per turn), then drop any leading ``result`` Turns whose ``tool.call`` was
    truncated off the top, so a tail read never opens on an orphaned tool result (#720)."""
    tail = turns[-DEFAULT_MAX_MESSAGES:]
    start = 0
    while start < len(tail) and tail[start].kind == "result":
        start += 1
    return tail[start:]


def _kimi_drop_partial_head(recs: list[dict]) -> list[dict]:
    """After a **truncated** tail read, drop every record before the first ``turn.prompt`` (#720).

    ``_read_tail`` only discards the partial first *line*; the surviving records can still open in
    the MIDDLE of a logical turn — e.g. the ``turn.prompt`` was cut off but its later
    ``content.part`` chunks remain. Reconstructing those would emit a headless, partial assistant
    message. A ``turn.prompt`` is the one safe boundary, so we resume from the first one; if the
    window contains none, there is no clean turn in it → drop the lot rather than show a fragment
    (the caller then falls back to raw-byte scrollback)."""
    for i, o in enumerate(recs):
        if o.get("type") == "turn.prompt":
            return recs[i:]
    return []


def _kimi_adapter(native_id: str, home: Path) -> list[Turn]:
    path = kimi_wire_path(native_id, home)
    if path is None:
        return []
    recs = _jsonl_dicts(_read_tail(path))
    try:
        truncated = path.stat().st_size > _TAIL_BYTES
    except OSError:
        truncated = False
    if truncated:
        recs = _kimi_drop_partial_head(recs)
    return _kimi_cap(_kimi_turns_from_wire(recs))


register_adapter("kimi-wire", _kimi_adapter)
register_locator("kimi-wire", _path_locator(kimi_wire_path))
register_growth("kimi-wire", _path_growth(kimi_wire_path))


# --- opencode -----------------------------------------------------------------------------


def _opencode_message_turns(
    role: str, part_rows: list[tuple], ts: float | None = None
) -> list[Turn]:
    """One opencode message's parts → Turns. text → message; tool → one-line summary;
    step-start/step-finish/reasoning are omitted (chrome / hidden thinking)."""
    r = "user" if role == "user" else "assistant"
    turns: list[Turn] = []
    for (pdata,) in part_rows:
        try:
            p = json.loads(pdata)
        except (ValueError, TypeError):
            continue
        pt = p.get("type")
        if pt == "text" and (p.get("text") or "").strip():
            turns.append(Turn(r, p["text"].strip(), "text", ts))
        elif pt == "tool":
            st = p.get("state") if isinstance(p.get("state"), dict) else {}
            arg = st.get("input") if isinstance(st, dict) else ""
            turns.append(Turn("assistant", f"{p.get('tool', 'tool')}({_short(arg)})", "tool"))
    return turns


def _opencode_adapter(native_id: str, home: Path) -> list[Turn]:
    """opencode keeps its conversation in SQLite (``message`` + ``part`` tables). Take the last
    ``DEFAULT_MAX_MESSAGES`` messages for the session (``id`` is a monotonic ULID), oldest-first,
    and expand each into its part Turns. Read-only + fail-soft: any sqlite error → ``[]``, so a
    locked or corrupt store never takes down the rows and views built on it. The DB is the same
    env-overridable path the provider reads (``base._opencode_db``)."""
    try:
        return _opencode_turns_strict(native_id, home)
    except (sqlite3.Error, FileNotFoundError):
        return []


def _opencode_turns_strict(native_id: str, home: Path) -> list[Turn]:
    """`_opencode_adapter`'s read, RAISING where it degrades (review comment 72377 finding 3).

    A missing DB is `FileNotFoundError` and any SQLite failure propagates: the binder only asks
    about ids it has just read out of this very store, so neither is "no turn yet"."""
    from .engines import base

    db = Path(base._opencode_db(home))
    if not db.exists():
        raise FileNotFoundError(f"opencode's store {db} does not exist")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
    try:
        rows = conn.execute(
            "SELECT id, data FROM message WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (native_id, DEFAULT_MAX_MESSAGES),
        ).fetchall()
        rows = rows[::-1]  # oldest-first
        turns: list[Turn] = []
        for mid, mdata in rows:
            try:
                meta = json.loads(mdata) or {}
                role = meta.get("role", "assistant")
                created = meta.get("time")
                ts = _when(created.get("created")) if isinstance(created, dict) else None
            except (ValueError, TypeError):
                role, ts = "assistant", None
            parts = conn.execute(
                "SELECT data FROM part WHERE message_id=? ORDER BY id", (mid,)
            ).fetchall()
            turns.extend(_opencode_message_turns(role, parts, ts))
        return turns
    finally:
        conn.close()


def _opencode_locator(native_id: str, home: Path) -> str | None:
    """opencode has **no per-session file** — a conversation is rows in one shared SQLite DB. So
    the location is the DB plus the id to query, and it resolves ONLY when rows for *this* session
    actually exist: the DB existing says nothing about this id, and naming a DB that doesn't hold
    the session would point the target agent at other people's sessions. Phrased as a query, never
    as a file to read. Read-only + fail-soft (any sqlite error → ``None``)."""
    from .engines import base

    db = Path(base._opencode_db(home))
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute(
            "SELECT 1 FROM message WHERE session_id=? LIMIT 1", (native_id,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None:
        return None
    return (
        f"{db} — SQLite database (no per-session file); "
        f"query rows where message.session_id = '{native_id}'"
    )


def _opencode_growth(native_id: str, home: Path) -> int | None:
    """A monotonic, session-scoped activity mark for opencode. Its conversation is rows, not a file.

    **The mark is the newest update TIMESTAMP across this session's messages and their parts**, not
    a size or a count.

    Counting rows misses the common case: opencode streams into the message it is already working
    on. Summing part sizes misses it too, and is not even monotonic — a replacement of equal length
    leaves the sum identical, and a shorter payload moves it BACKWARD, so a busy session reads as
    stalled and a shrinking one reads as going into reverse (#888 review). A timestamp has neither
    problem: it advances on an insert (a new row is created "now") and on an in-place update, and
    it never goes back.

    Session-scoped on purpose: the shared database is written by every session, so a whole-database
    measure would report all of them healthy forever.

    The time columns are read defensively. `time_updated` is what opencode maintains, but this is
    a store we do not own and must never assume the shape of — an older or drifted schema falls
    back to row counts, which is worse but still moves on an insert.
    """
    from .engines import base

    db = Path(base._opencode_db(home))
    if not db.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return None
    try:

        def _time_col(table: str) -> str | None:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}  # noqa: S608
            for name in ("time_updated", "time_created"):
                if name in cols:
                    return name
            return None

        mcol, pcol = _time_col("message"), _time_col("part")
        marks: list[int] = []
        if mcol:
            row = conn.execute(
                f"SELECT MAX(COALESCE({mcol}, 0)) FROM message WHERE session_id=?",  # noqa: S608
                (native_id,),
            ).fetchone()
            if row and row[0] is not None:
                marks.append(int(row[0]))
        if pcol:
            row = conn.execute(
                f"SELECT MAX(COALESCE(p.{pcol}, 0)) FROM part p "  # noqa: S608
                "JOIN message m ON m.id = p.message_id WHERE m.session_id=?",
                (native_id,),
            ).fetchone()
            if row and row[0] is not None:
                marks.append(int(row[0]))
        if marks:
            return max(marks)

        # No usable time column — fall back to row counts, which still move on an insert.
        row = conn.execute(
            "SELECT (SELECT COUNT(*) FROM message WHERE session_id=?) "
            "     + (SELECT COUNT(*) FROM part p JOIN message m ON m.id = p.message_id "
            "        WHERE m.session_id=?) AS n",
            (native_id, native_id),
        ).fetchone()
        return None if row is None else int(row[0])
    except sqlite3.Error:
        return None
    finally:
        conn.close()


register_adapter("opencode-sqlite", _opencode_adapter)
register_strict_adapter("opencode-sqlite", _opencode_turns_strict)
register_locator("opencode-sqlite", _opencode_locator)
register_growth("opencode-sqlite", _opencode_growth)


# --- gemini -------------------------------------------------------------------------------


def _gemini_text(content: object) -> str:
    """Visible text of a gemini message ``content`` — a bare string, or a list of ``{"text": …}``
    parts (joined). Non-text parts (e.g. function calls) contribute nothing here."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [str(i["text"]) for i in content if isinstance(i, dict) and i.get("text")]
        return " ".join(parts).strip()
    return ""


def gemini_chat_path(native_id: str, home: Path) -> Path | None:
    """Resolve a gemini session uuid to its chat JSONL under
    ``home/.gemini/tmp/<projectHash>/chats/session-<ts>-<short>.jsonl``. The project dir isn't known
    from the id, so glob by the filename's short suffix (first 8 chars of the uuid) and confirm via
    the file's ``sessionId`` header — the short suffix can collide, the header can't. Only an EXACT
    header match resolves; on no match we return ``None`` (→ clean raw-byte fallback) rather than a
    same-short-prefix neighbour, which would render a *different* session's conversation. The tmp
    dir is the same env-overridable location the provider scans (``base._gemini_tmp_dir``)."""
    from .engines import base

    root = base._gemini_tmp_dir(home)
    short = native_id[:8]
    try:
        candidates = list(root.glob(f"*/chats/session-*{short}*.jsonl"))
    except OSError:
        return None
    for path in candidates:
        try:
            with path.open("rb") as fh:
                header = json.loads(fh.readline() or b"{}")
        except (OSError, ValueError):
            continue
        if isinstance(header, dict) and header.get("sessionId") == native_id:
            return path
    return None


def _gemini_turns_from_jsonl(path: Path, *, max_messages: int = DEFAULT_MAX_MESSAGES) -> list[Turn]:
    """Parse a gemini chat JSONL into Turns. ``user`` records → user messages; ``gemini`` records →
    assistant messages (the ``thoughts`` field — hidden thinking — is omitted); the ``kind:"main"``
    header and ``info`` records are skipped. gemini stores no tool-call records in the chat log."""
    recs = _jsonl_dicts(_read_tail(path))
    turns: list[Turn] = []
    for o in recs[-max_messages:]:
        t = o.get("type")
        if t == "user":
            text = _gemini_text(o.get("content"))
            if text:
                turns.append(Turn("user", text, "text", _when(o.get("timestamp"))))
        elif t == "gemini":  # kind-data: gemini's own chat record type
            text = _gemini_text(o.get("content"))
            if text:
                turns.append(Turn("assistant", text, "text"))
    return turns


def _gemini_adapter(native_id: str, home: Path) -> list[Turn]:
    path = gemini_chat_path(native_id, home)
    return _gemini_turns_from_jsonl(path) if path is not None else []


register_adapter("gemini-chat", _gemini_adapter)
register_locator("gemini-chat", _path_locator(gemini_chat_path))
register_growth("gemini-chat", _path_growth(gemini_chat_path))


# --- antigravity (agy) --------------------------------------------------------------------
# agy's transcript is NOT gemini's format, so it gets its own parser (the conversation itself
# lives in a SQLite db of protobuf steps; this plaintext JSONL is agy's own rendered log). The
# id→path resolution and the ``<USER_REQUEST>`` unwrap are reused from the provider so the two
# never drift.


def _antigravity_turns_from_jsonl(
    path: Path, *, max_messages: int = DEFAULT_MAX_MESSAGES
) -> list[Turn]:
    """Parse an agy transcript JSONL into Turns. ``USER_INPUT`` steps → user messages (the
    ``<USER_REQUEST>`` body, dropping the metadata wrappers agy adds for the model); the model's
    ``PLANNER_RESPONSE`` steps → assistant messages. System bookkeeping (``CONVERSATION_HISTORY``)
    and tool steps render nothing — parity with the gemini adapter (user + assistant text only)."""
    from .engines import antigravity

    recs = _jsonl_dicts(_read_tail(path))
    turns: list[Turn] = []
    for o in recs[-max_messages:]:
        t = o.get("type")
        if t == "USER_INPUT":
            text = antigravity._user_request_text(o.get("content"))
            if text:
                turns.append(Turn("user", text, "text", _when(o.get("created_at"))))
        elif t == "PLANNER_RESPONSE":
            content = o.get("content")
            if isinstance(content, str) and content.strip():
                turns.append(Turn("assistant", content.strip(), "text"))
    return turns


def _antigravity_adapter(native_id: str, home: Path) -> list[Turn]:
    from .engines import antigravity, base

    path = antigravity._transcript_path(base._antigravity_dir(home), native_id)
    return _antigravity_turns_from_jsonl(path) if path is not None else []


def _antigravity_locator(native_id: str, home: Path) -> str | None:
    """antigravity's transcript path, resolved through the provider's own exact-id lookup."""
    from .engines import antigravity, base

    path = antigravity._transcript_path(base._antigravity_dir(home), native_id)
    return str(path) if path is not None else None


def _antigravity_growth(native_id: str, home: Path) -> int | None:
    """Size of antigravity's own transcript file. It is file-backed like the others; it was simply
    missed when the registry was introduced, and the fallback it landed on is the capped renderer
    that this whole mechanism exists to avoid (#888 review, finding 3)."""
    from .engines import antigravity, base

    path = antigravity._transcript_path(base._antigravity_dir(home), native_id)
    if path is None:
        return None
    try:
        return int(Path(path).stat().st_size)
    except OSError:
        return None


register_adapter("antigravity-brain", _antigravity_adapter)
register_locator("antigravity-brain", _antigravity_locator)
register_growth("antigravity-brain", _antigravity_growth)


# --- battlelab-chat (#1209): the API agent's own store ----------------------------------------


def _chat_adapter(native_id: str, home: Path) -> list[Turn]:
    """A `chat`-runtime conversation, from BattleLab's own store: each exchange's message and, once
    settled, its reply. The store root is the engine's (``store_scope``, set by ``_scoped``)."""
    from . import chat_store
    from .engines import base

    log = chat_store.read(base._store("battlelab-chat", home=home), native_id)
    if log is None:
        return []
    out: list[Turn] = []
    for t in log.turns:
        out.append(Turn("user", t.text, ts=t.ts or None))
        if t.reply is not None:
            out.append(Turn("assistant", t.reply, ts=t.reply_ts))
    return out


def chat_log_path(native_id: str, home: Path) -> Path | None:
    """The conversation file of a `chat` session, or None for a malformed id."""
    from . import chat_store
    from .engines import base

    if not chat_store.valid_turn_id(native_id):  # a session id has the same UUID shape
        return None
    return base._store("battlelab-chat", home=home) / f"{native_id}.jsonl"


register_adapter("battlelab-chat", _chat_adapter)
register_locator("battlelab-chat", _path_locator(chat_log_path))
# Append-only: the file's size is the monotonic, uncapped growth signal stall detection needs.
register_growth("battlelab-chat", _path_growth(chat_log_path))
