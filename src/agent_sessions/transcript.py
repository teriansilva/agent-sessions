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
from dataclasses import dataclass
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
    """

    role: str
    text: str
    kind: str = "text"


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
_ADAPTERS: dict[str, TranscriptAdapter] = {}


def register_adapter(engine_id: str, adapter: TranscriptAdapter) -> None:
    """Register an engine's transcript adapter (keyed by the engines.py engine id)."""
    _ADAPTERS[engine_id] = adapter


def adapter_for(engine_id: str) -> TranscriptAdapter | None:
    """The registered adapter for ``engine_id``, or ``None`` (→ caller keeps the raw-byte path)."""
    return _ADAPTERS.get(engine_id)


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
                turns.append(Turn(role, b["text"].strip(), "text"))
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


def _claude_adapter(native_id: str, home: Path) -> list[Turn]:
    """Resolve a Claude session id to its JSONL under ``home/.claude/projects/*/`` and parse it.
    (The cwd-encoded project dir isn't known from the id alone, so glob for ``<id>.jsonl``;
    also check ``projects-archive`` for archived sessions.)"""
    roots = [home / ".claude" / "projects", home / ".claude" / "projects-archive"]
    for root in roots:
        try:
            match = next(root.glob(f"*/{native_id}.jsonl"), None)
        except OSError:
            match = None
        if match is not None:
            return claude_turns_from_jsonl(match)
    return []


register_adapter("claude", _claude_adapter)


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
            # Codex injects an `<environment_context>` / `<user_instructions>` XML preamble as the
            # first "user" message — machine context, not conversation. Skip it.
            if text and not text.startswith(("<environment_context", "<user_instructions")):
                turns.append(Turn(role, text, "text"))
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


register_adapter("codex", _codex_adapter)


# --- opencode -----------------------------------------------------------------------------


def _opencode_message_turns(role: str, part_rows: list[tuple]) -> list[Turn]:
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
            turns.append(Turn(r, p["text"].strip(), "text"))
        elif pt == "tool":
            st = p.get("state") if isinstance(p.get("state"), dict) else {}
            arg = st.get("input") if isinstance(st, dict) else ""
            turns.append(Turn("assistant", f"{p.get('tool', 'tool')}({_short(arg)})", "tool"))
    return turns


def _opencode_adapter(native_id: str, home: Path) -> list[Turn]:
    """opencode keeps its conversation in SQLite (``message`` + ``part`` tables). Take the last
    ``DEFAULT_MAX_MESSAGES`` messages for the session (``id`` is a monotonic ULID), oldest-first,
    and expand each into its part Turns. Read-only + fail-soft: any sqlite error → ``[]``. The DB
    is the same env-overridable path the provider reads (``base._opencode_db``)."""
    from .engines import base

    db = Path(base._opencode_db(home))
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT id, data FROM message WHERE session_id=? ORDER BY id DESC LIMIT ?",
            (native_id, DEFAULT_MAX_MESSAGES),
        ).fetchall()
        rows = rows[::-1]  # oldest-first
        turns: list[Turn] = []
        for mid, mdata in rows:
            try:
                role = (json.loads(mdata) or {}).get("role", "assistant")
            except (ValueError, TypeError):
                role = "assistant"
            parts = conn.execute(
                "SELECT data FROM part WHERE message_id=? ORDER BY id", (mid,)
            ).fetchall()
            turns.extend(_opencode_message_turns(role, parts))
        return turns
    except sqlite3.Error:
        return []
    finally:
        conn.close()


register_adapter("opencode", _opencode_adapter)


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
                turns.append(Turn("user", text, "text"))
        elif t == "gemini":
            text = _gemini_text(o.get("content"))
            if text:
                turns.append(Turn("assistant", text, "text"))
    return turns


def _gemini_adapter(native_id: str, home: Path) -> list[Turn]:
    path = gemini_chat_path(native_id, home)
    return _gemini_turns_from_jsonl(path) if path is not None else []


register_adapter("gemini", _gemini_adapter)
