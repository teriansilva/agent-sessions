"""Engine-agnostic conversation-transcript renderer for scroll-up history (issue #242).

Raw PTY-byte scrollback is width-fragile: it stores the literal screen-drawing escapes (absolute
cursor moves baked to the width they were authored at), so a reattach at a different width
garbles / duplicates / loses the history — and there is no faithful way to reflow an
absolute-positioned grid to a narrower screen (proved by the reverted pyte attempt, PR #248/#249).

Instead, render scroll-up from the engine's OWN saved conversation — the real messages it persists
for ``resume``/``continue`` (Claude's ``*.jsonl``, codex rollout JSONL, opencode's SQLite,
gemini's ``logs.json``). That's *semantic text*: it wraps cleanly at any width, is fast (no escape
parsing), and can't misfire — there are no cursor escapes in it. The live terminal then owns only
the current frame.

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
import re
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
    """Wrap each line in the user-message grey background, padded to ``width`` so the band fills."""
    out: list[str] = []
    for ln in lines:
        pad = " " * max(0, width - _vis_len(ln))
        out.append(_SGR_USER_BG + ln + pad + _BG_OFF)
    return out


def _dot_block(text: str, width: int) -> list[str]:
    """Assistant turn: first line prefixed with a green ● dot, continuations hanging-indented 2."""
    wrapped = _wrap(text, width, indent="  ") or [""]
    wrapped[0] = _SGR_ASSISTANT + "●" + _RESET + " " + wrapped[0][2:]
    return wrapped


# Default bounds (Hermes #242: bound history rows + input messages independently of raw caps).
DEFAULT_MAX_MESSAGES = 400
DEFAULT_MAX_LINES = 4000


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
    max_lines: int = DEFAULT_MAX_LINES,
) -> bytes:
    """Render ``turns`` as flat, wrapped ANSI for injection as xterm scrollback at ``cols`` wide.

    Width-correct at any width (it wraps plain text — no cursor escapes). Bounded to the last
    ``max_lines`` rendered lines. Returns UTF-8 bytes; the caller decides framing (e.g. a leading
    clear + a trailing separator before the live frame). Empty input → ``b""``.
    """
    cols = max(20, cols)
    lines: list[str] = []
    for t in turns:
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
            lines.extend(_bg_block(_wrap(_render_md(text), cols), cols))
        else:  # assistant / system
            # Assistant turn: a green ● dot + rendered markdown, no "Claude" label (#301).
            lines.append("")
            lines.extend(_dot_block(_render_md(text), cols))
    if not lines:
        return b""
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
    return "\r\n".join(lines).encode("utf-8", "replace")


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
_TAIL_BYTES = 2 * 1024 * 1024


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
