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
import textwrap
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


# Strip the common Markdown *syntax* so the log reads clean (no literal ``**`` / backticks / ``#``)
# while keeping the words. Applied to message text BEFORE wrapping, so width stays exact.
_MD_FENCE = re.compile(r"^[ \t]*```[^\n]*$", re.M)  # ```code-fence``` lines → removed
_MD_HEAD = re.compile(r"^[ \t]*#{1,6}[ \t]+", re.M)  # "### Heading" → "Heading"
_MD_BULLET = re.compile(r"^([ \t]*)[-*][ \t]+", re.M)  # "- item" / "* item" → "• item"
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.S)  # **bold** → bold
_MD_STRIKE = re.compile(r"~~(.+?)~~", re.S)  # ~~strike~~ → strike
_MD_CODE = re.compile(r"`([^`]+)`")  # `code` → code


def _clean_md(text: str) -> str:
    text = _MD_FENCE.sub("", text)
    text = _MD_HEAD.sub("", text)
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_STRIKE.sub(r"\1", text)
    text = _MD_CODE.sub(r"\1", text)
    text = _MD_BULLET.sub(r"\1• ", text)
    return text


# --- shared renderer -----------------------------------------------------------------------

_SGR_USER = "\x1b[1;36m"  # bright cyan  "› You"
_SGR_ASSISTANT = "\x1b[1;32m"  # bright green "⏺ <engine>"
_SGR_DIM = "\x1b[90m"  # grey         tool calls / results
_RESET = "\x1b[0m"

# Default bounds (Hermes #242: bound history rows + input messages independently of raw caps).
DEFAULT_MAX_MESSAGES = 400
DEFAULT_MAX_LINES = 4000


def _wrap(text: str, width: int, indent: str = "") -> list[str]:
    """Wrap ``text`` to ``width`` columns (per paragraph), preserving blank lines. Plain text only
    — no terminal escapes — so the result is width-correct at any width (the whole point).

    Note: ``textwrap`` counts code points, so a line dense with double-width glyphs (CJK / some
    emoji) can be slightly wider than ``width`` visually — bounded and rare in code transcripts,
    and a vast improvement over the raw-byte garble. A wcwidth-aware wrap is a later refinement.
    """
    out: list[str] = []
    avail = max(1, width - len(indent))
    for para in text.split("\n"):
        if not para.strip():
            out.append("")
            continue
        for line in textwrap.wrap(para, avail, break_long_words=True, break_on_hyphens=False) or [
            ""
        ]:
            out.append(indent + line)
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
            lines.append("")
            lines.append(_SGR_USER + "› You" + _RESET)
            lines.extend(_wrap(_clean_md(text), cols))
        else:  # assistant / system
            lines.append("")
            lines.append(_SGR_ASSISTANT + "⏺ " + assistant_label + _RESET)
            lines.extend(_wrap(_clean_md(text), cols))
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
