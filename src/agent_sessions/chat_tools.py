"""Read-only tools for `chat`-runtime agents (#853 P9b, #1222). **A security boundary.**

With ``tools = "read"`` the API agent may call two functions, and nothing else:

* ``list_files(path)`` — one directory's entries;
* ``read_file(path, start_line?, max_lines?)`` — text lines of one regular file.

No write, no delete, no command, no network, no search. What they return goes to the endpoint the
operator configured for the agent — the same destination as the conversation itself, through the
same transport (`review._post_chat`), so template-secret redaction applies to it.

**The root is the conversation's folder** (``ChatLog.cwd``, realpath-resolved per call), never
``$HOME`` and never another root. Access reuses :mod:`agent_sessions.files`' open-then-verify
proof re-rooted there: ``O_NOFOLLOW|O_NONBLOCK`` acquisition, ``fstat`` for the type, and a
``/proc/self/fd`` re-check that the descriptor is still inside the root. A symlinked final
component is refused; an intermediate one collapses and is judged on the descriptor.

**The boundary is re-read at every call** (:func:`admit`): the terminal's roots +
``folder_exclusions`` rule (``project_dirs.in_scope(curated=False)``) must admit the folder AND the
file, from the live prefs. An exclusion added mid-conversation stops the next call.

**Credential-shaped paths are refused before any open**, and again on the verified path (a
``docs -> .git`` symlink would otherwise launder a hidden target through a plain name). Hidden
components (``.env``, ``.git/``, ``.ssh/`` …) and key/credential file names never reach a read.

A refusal is a RESULT, not an exception: the model is told what was refused and why, and the turn
goes on. File contents are returned to the caller only — :class:`ToolResult.summary` is what the
transcript keeps, and it never carries them.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field

from . import files, prefs, project_dirs
from .fsbrowse import FsError

LIST_MAX_ENTRIES = 500
READ_MAX_LINES = 2000
READ_MAX_BYTES = 64 * 1024
#: How much of a file is read to find the requested lines. Lines past it are reported unreachable.
READ_WINDOW_BYTES = files.FILES_MAX_READ
PATH_MAX = 1024

#: Names refused anywhere under the root (matched case-insensitively against each component).
CREDENTIAL_NAMES = (
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.kdbx",
    "*.keystore",
    "*.jks",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials*",
    "secrets*",
)

TOOL_NAMES = ("list_files", "read_file")

#: The OpenAI-compatible tool declarations sent with a request when tools are on.
SPECS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List one directory in the conversation's folder. Paths are relative to that "
                "folder; '.' is the folder itself. Hidden and credential files are not listed."
            ),
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "a relative path"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                f"Read lines of one text file in the conversation's folder, at most "
                f"{READ_MAX_LINES} lines or {READ_MAX_BYTES // 1024} KiB per call. Lines are "
                "1-based; call again with a later start_line to continue."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "a relative path"},
                    "start_line": {"type": "integer", "minimum": 1},
                    "max_lines": {"type": "integer", "minimum": 1, "maximum": READ_MAX_LINES},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
]


@dataclass
class ToolResult:
    """One call's outcome. ``content`` goes to the model; ``summary`` goes to the transcript."""

    content: str
    summary: dict = field(default_factory=dict)
    #: The descriptor-verified absolute path this result carries content from (None for a
    #: refusal). The turn loop re-admits it before the result is sent (#1222).
    target: str | None = None


def printable(text: str) -> str:
    """``text`` with every code point UTF-8 cannot encode (a lone surrogate — valid JSON, e.g.
    ``"\\ud800"``) replaced by U+FFFD. Anything the model sent that is echoed into a summary, a
    record or the transcript goes through here, so no model string can make a write raise
    (Hermes on #1228)."""
    if not _unencodable(text):
        return text
    return "".join("\ufffd" if 0xD800 <= ord(c) <= 0xDFFF else c for c in text)


def _unencodable(text: str) -> bool:
    return any(0xD800 <= ord(c) <= 0xDFFF for c in text)


def _refused(name: str, path: str, reason: str) -> ToolResult:
    name, path = printable(name), printable(path)
    return ToolResult(
        content=json.dumps({"error": f"refused: {reason}"}),
        summary={"name": name, "path": path, "outcome": "refused", "reason": reason},
    )


def refused_name(parts: list[str]) -> str | None:
    """Why a relative path is refused by NAME, or None. Checked lexically and on the verified
    path."""
    for p in parts:
        if p.startswith("."):
            return "hidden path — never readable by the agent"
        low = p.lower()
        if any(fnmatch.fnmatchcase(low, pat) for pat in CREDENTIAL_NAMES):
            return "credential-shaped file name"
    return None


def _parts(rel: str) -> list[str]:
    return [p for p in rel.split(os.sep) if p and p != "."]


def admit(cwd: str) -> str | None:
    """The folder's realpath if it may be read now, else None. Live prefs, every call."""
    try:
        root = os.path.realpath(cwd)
    except (OSError, ValueError):
        return None
    if not os.path.isdir(root):
        return None
    home = files.home_root()
    if root != home and not root.startswith(home + os.sep):
        return None
    if not _in_boundary(root):
        return None
    return root


def still_admitted(cwd: str, target: str | None) -> bool:
    """May a result read from ``target`` still be SENT? The same live rule as a new call: the
    folder admitted now, and the target inside the boundary now (#1222). Blocking (realpath)."""
    root = admit(cwd)
    if root is None:
        return False
    return target is None or _in_boundary(target)


def _in_boundary(path: str) -> bool:
    return project_dirs.in_scope(
        path,
        roots=project_dirs.effective_roots(),
        exclusions=prefs.get_folder_exclusions(),
        curated=False,
    )


def _resolve(root: str, raw: object) -> tuple[str, str] | str:
    """``(absolute candidate, relative display path)`` or a refusal reason."""
    if not isinstance(raw, str):
        return "path must be a string"
    # A lone surrogate cannot reach a syscall (`os.lstat` raises on it): refuse it as the
    # invalid path it is, before anything touches the filesystem (Hermes on #1228).
    if len(raw) > PATH_MAX or any(ord(c) < 0x20 for c in raw) or _unencodable(raw):
        return "invalid path"
    raw = raw.strip() or "."
    cand = os.path.normpath(raw if os.path.isabs(raw) else os.path.join(root, raw))
    rel = os.path.relpath(cand, root)
    if rel == ".." or rel.startswith(".." + os.sep):
        return "outside the conversation's folder"
    return cand, ("." if rel == "." else rel)


def _verified_rel(root: str, verified: str) -> str | None:
    rel = os.path.relpath(verified, root)
    if rel == ".." or rel.startswith(".." + os.sep):
        return None
    return rel


def _fs_reason(e: FsError) -> str:
    msg = str(e)
    if "symlink" in msg:
        return "a symbolic link — links are not followed"
    if e.status == 403 and "escapes" in msg:
        return "outside the conversation's folder"
    return msg


def _list(root: str, cand: str, rel: str) -> ToolResult:
    try:
        listing = files.list_dir(cand, root)
    except FsError as e:
        return _refused("list_files", rel, _fs_reason(e))
    vrel = _verified_rel(root, listing["path"])
    if vrel is None:
        return _refused("list_files", rel, "outside the conversation's folder")
    why = refused_name(_parts(vrel))
    if why:
        return _refused("list_files", rel, why)
    if not _in_boundary(listing["path"]):
        return _refused("list_files", rel, "an excluded folder")
    rows, omitted, capped = [], 0, False
    for e in listing["entries"]:
        if refused_name([e["name"]]) or not _in_boundary(e["path"]):
            omitted += 1
            continue
        if len(rows) >= LIST_MAX_ENTRIES:
            omitted += 1
            capped = True
            continue
        rows.append({"name": e["name"], "kind": e["kind"], "size": e["size"]})
    # Complete = every entry was SEEN (not every entry returned): refused names are withheld by
    # design, a cap or an unfinished scan means there are entries the model was not told about.
    complete = bool(listing["complete"]) and not capped
    body = {"path": rel, "entries": rows, "complete": complete, "omitted": omitted}
    return ToolResult(
        content=json.dumps(body),
        summary={"name": "list_files", "path": rel, "outcome": "ok", "entries": len(rows)},
        target=listing["path"],
    )


def _int_arg(v: object, default: int, lo: int, hi: int) -> int | None:
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        return None
    return v


def _read(root: str, cand: str, rel: str, args: dict) -> ToolResult:
    start = _int_arg(args.get("start_line"), 1, 1, 10_000_000)
    count = _int_arg(args.get("max_lines"), READ_MAX_LINES, 1, READ_MAX_LINES)
    if start is None or count is None:
        return _refused("read_file", rel, f"start_line ≥ 1 and max_lines in [1, {READ_MAX_LINES}]")
    try:
        verified, data, _size, window_cut = files.read_file_bytes(
            cand, limit=READ_WINDOW_BYTES, root=root
        )
    except FsError as e:
        return _refused("read_file", rel, _fs_reason(e))
    vrel = _verified_rel(root, verified)
    if vrel is None:
        return _refused("read_file", rel, "outside the conversation's folder")
    why = refused_name(_parts(vrel))
    if why:
        return _refused("read_file", rel, why)
    if not _in_boundary(verified):
        return _refused("read_file", rel, "an excluded folder")
    if b"\x00" in data[:8192]:
        return _refused("read_file", rel, "not a text file")
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if window_cut:
        lines = lines[:-1]  # the last line of a cut window is partial
    total = None if window_cut else len(lines)
    if start > len(lines):
        return _refused(
            "read_file",
            rel,
            "start_line is past the end of the file"
            if not window_cut
            else f"start_line is beyond the first {READ_WINDOW_BYTES // (1024 * 1024)} MiB, "
            "which is all a tool call can reach",
        )
    out, used = [], 0
    for line in lines[start - 1 : start - 1 + count]:
        b = len(line.encode("utf-8")) + 1
        if used + b > READ_MAX_BYTES and out:
            break
        # One line longer than the whole cap is cut at the cap, in bytes, on a character boundary.
        out.append(line.encode("utf-8")[:READ_MAX_BYTES].decode("utf-8", errors="ignore"))
        used += b
    end = start + len(out) - 1
    body = {
        "path": rel,
        "start_line": start,
        "end_line": end,
        "total_lines": total,
        "more": total is None or end < total,
        "text": "\n".join(out),
    }
    return ToolResult(
        content=json.dumps(body),
        target=verified,
        summary={
            "name": "read_file",
            "path": rel,
            "outcome": "ok",
            "start_line": start,
            "end_line": end,
            "total_lines": total,
        },
    )


def run(cwd: str, name: object, raw_args: object) -> ToolResult:
    """Execute ONE tool call. Blocking (filesystem): the caller runs it off the event loop, on
    the file panel's bounded pool. Never raises for anything the model sent."""
    tool = name if isinstance(name, str) and name in TOOL_NAMES else "unknown"
    try:
        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
    except ValueError:
        return _refused(tool, "", "arguments are not valid JSON")
    if not isinstance(args, dict):
        return _refused(tool, "", "arguments must be an object")
    path_arg = args.get("path", ".")
    shown = path_arg[:200] if isinstance(path_arg, str) else ""
    if tool == "unknown":
        return _refused(
            str(name)[:64] if isinstance(name, str) else "unknown", shown, "no such tool"
        )
    allowed = {"path"} if tool == "list_files" else {"path", "start_line", "max_lines"}
    extra = sorted(set(args) - allowed)
    if extra:
        return _refused(tool, shown, f"unknown arguments: {extra}")
    root = admit(cwd)
    if root is None:
        return _refused(tool, shown, "this conversation's folder is not readable now")
    resolved = _resolve(root, path_arg)
    if isinstance(resolved, str):
        return _refused(tool, shown, resolved)
    cand, rel = resolved
    why = refused_name(_parts(rel))
    if why:
        return _refused(tool, rel, why)
    if tool == "list_files":
        return _list(root, cand, rel)
    return _read(root, cand, rel, args)
