"""The BattleLab skills tool (#1693 S2): a minimal stdio MCP server over ONE launch's skill bundle.

A session started from a specialist carries this beside the memory tool (`agent_tools`). Its only
input is its own environment: `BATTLELAB_SKILLS_BUNDLE`, the absolute path of the bundle BattleLab
built for this launch (`skill_bundles`) — each of the specialist's skills at its pinned revision.
No network, no listening socket, no dependency, and **nothing writable**: two read tools.

**Confinement, for every call** (the memory tool's own primitives, `memory_mcp`): the bundle is
opened as a directory descriptor (``O_DIRECTORY | O_NOFOLLOW``) and must still be the very
directory found at start (device + inode); every path component below it is opened relative to its
parent descriptor with ``O_NOFOLLOW``; a file is served only as a regular, single-link file of this
user, read to its cap. A symlink, a replaced directory, a FIFO, ``..``, an absolute path or a name
outside the bound skills is refused. So the tool can read the bundle and nothing else.

**Untrusted output.** Skill files are instructions the operator chose for this specialist, but they
are text from a library that may have come from a gallery: returned length-bounded with control
characters stripped, framed as "skill material for this task".
"""

from __future__ import annotations

import os
import stat
import sys

from .memory_mcp import (
    _DIR_FLAGS,
    PROTOCOL_VERSIONS,
    ToolError,
    _error,
    _frontmatter,
    _open_pinned,
    _pin,
    _result,
    _text,
    clean,
    one_line,
    read_file,
    serve,
)
from .playbooks.schema import SKILL_REF_RE

ENV_BUNDLE = "BATTLELAB_SKILLS_BUNDLE"
SERVER_NAME = "battlelab-skills"
SERVER_VERSION = "1"
FILE_MAX_BYTES = 256 * 1024
#: Files listed per skill, at most (a skill holds at most 64).
LIST_FILES_MAX = 64
#: One path component: any name the skills library accepts (spaces, non-ASCII, a leading dot) —
#: the confinement is the O_NOFOLLOW walk, not the spelling. Only what can't be a single name
#: inside the bundle is refused.
_COMPONENT_MAX_BYTES = 255
_PATH_MAX = 512
_DEPTH_MAX = 9

FRAME = "[BattleLab skill material for this specialist, read-only.]"
INSTRUCTIONS = (
    "BattleLab skills: the know-how this specialist was given, read-only. Call skills_list to "
    "see each skill and its files, then skill_read the files a task needs. SKILL.md is each "
    "skill's entry point."
)
TOOLS = [
    {
        "name": "skills_list",
        "description": (
            "List the skills this specialist carries: each skill's id, name, one-line summary "
            "and files. Call it before a task."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "skill_read",
        "description": (
            "Read one file of a skill this specialist carries, by skill id and a relative path "
            "as listed by skills_list (default SKILL.md)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "id": {"type": "string", "maxLength": 64},
                "path": {"type": "string", "maxLength": _PATH_MAX},
            },
            "required": ["id"],
            "additionalProperties": False,
        },
    },
]


def _skill_id(raw: object) -> str:
    if not isinstance(raw, str) or not SKILL_REF_RE.fullmatch(raw):
        raise ToolError("id must be a skill id as listed by skills_list")
    return raw


def _component_ok(name: str) -> bool:
    try:
        size = len(name.encode("utf-8"))  # strict: a lone surrogate is no file name
    except UnicodeEncodeError:
        return False
    return (
        name not in ("", ".", "..")
        and "/" not in name
        and size <= _COMPONENT_MAX_BYTES
        and not any(ord(c) < 32 or ord(c) == 127 for c in name)
    )


def _components(raw: object) -> list[str]:
    if not isinstance(raw, str) or not raw or len(raw) > _PATH_MAX or raw.startswith("/"):
        raise ToolError("path must be a relative file path as listed by skills_list")
    parts = raw.split("/")
    if len(parts) > _DEPTH_MAX or not all(_component_ok(p) for p in parts):
        raise ToolError("path must be a relative file path as listed by skills_list")
    return parts


def _is_dir_at(dfd: int, name: str) -> bool:
    try:
        return stat.S_ISDIR(os.stat(name, dir_fd=dfd, follow_symlinks=False).st_mode)
    except OSError:
        return False


def _open_dir_at(dfd: int, name: str) -> int:
    try:
        return os.open(name, _DIR_FLAGS, dir_fd=dfd)
    except OSError:
        raise ToolError("no such file in this skill") from None


class Server:
    """One session's skills tool, over the bundle at ``bundle``."""

    def __init__(self, bundle: str) -> None:
        self.bundle = bundle
        self.bundle_id = _pin(bundle)

    def _root(self) -> int:
        return _open_pinned(self.bundle, self.bundle_id)

    def _skills(self, root_fd: int) -> list[str]:
        try:
            names = os.listdir(root_fd)
        except OSError:
            raise ToolError("unavailable") from None
        return sorted(n for n in names if SKILL_REF_RE.fullmatch(n) and _is_dir_at(root_fd, n))

    def _files(self, skill_fd: int, prefix: str = "", depth: int = 0) -> list[str]:
        out: list[str] = []
        try:
            names = sorted(os.listdir(skill_fd))
        except OSError:
            return out
        for name in names:
            if len(out) >= LIST_FILES_MAX or not _component_ok(name):
                continue
            try:
                st = os.stat(name, dir_fd=skill_fd, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(st.st_mode) and depth < _DEPTH_MAX - 1:
                try:
                    child = os.open(name, _DIR_FLAGS, dir_fd=skill_fd)
                except OSError:
                    continue
                try:
                    out += self._files(child, f"{prefix}{name}/", depth + 1)
                finally:
                    os.close(child)
            elif stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.geteuid():
                out.append(prefix + name)  # exactly what skill_read will serve
        return out[:LIST_FILES_MAX]

    # ---- the two tools ------------------------------------------------------------------------

    def skills_list(self, args: object) -> str:
        if not isinstance(args, dict) or args:
            raise ToolError("skills_list takes no arguments")
        root_fd = self._root()
        try:
            lines = []
            for sid in self._skills(root_fd):
                skill_fd = _open_dir_at(root_fd, sid)
                try:
                    raw = read_file(skill_fd, "SKILL.md", FILE_MAX_BYTES)
                    meta, _ = _frontmatter(raw.decode("utf-8", "replace")) if raw else ({}, "")
                    name = one_line(meta.get("name", sid), 120) or sid
                    summary = one_line(meta.get("description", ""), 200)
                    listed = ", ".join(self._files(skill_fd))
                finally:
                    os.close(skill_fd)
                lines.append(f"- `{sid}` — {name}: {summary}\n  files: {listed}")
            if not lines:
                return f"{FRAME}\n\nThis specialist carries no skills."
            return f"{FRAME}\n\n" + "\n".join(lines)
        finally:
            os.close(root_fd)

    def skill_read(self, args: object) -> str:
        if not isinstance(args, dict) or not set(args) <= {"id", "path"} or "id" not in args:
            raise ToolError("skill_read takes id and an optional path")
        sid = _skill_id(args["id"])
        parts = _components(args.get("path", "SKILL.md"))
        root_fd = self._root()
        opened = [root_fd]
        try:
            if sid not in self._skills(root_fd):
                raise ToolError("this specialist does not carry that skill")
            opened.append(_open_dir_at(root_fd, sid))
            for part in parts[:-1]:
                opened.append(_open_dir_at(opened[-1], part))
            raw = read_file(opened[-1], parts[-1], FILE_MAX_BYTES)
            if raw is None:
                raise ToolError("no such file in this skill, or it is too large")
            text = clean(raw.decode("utf-8", "replace"))
            return f"{FRAME} `{sid}/{'/'.join(parts)}`\n\n{text}"
        finally:
            for fd in reversed(opened):
                os.close(fd)

    # ---- JSON-RPC (the memory tool's shape) -----------------------------------------------------

    def handle(self, msg: object) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return _error(None, -32600, "invalid request")
        method, rid = msg.get("method"), msg.get("id")
        is_request = "id" in msg
        if is_request and (isinstance(rid, bool) or not isinstance(rid, str | int)):
            return _error(None, -32600, "invalid request id")
        if not isinstance(method, str):
            return None if not is_request else _error(rid, -32600, "invalid request")
        if not is_request:
            return None
        params = msg.get("params")
        params = {} if params is None else params
        if not isinstance(params, dict):
            return _error(rid, -32602, "params must be an object")
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0]
            return _result(
                rid,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": INSTRUCTIONS,
                },
            )
        if method == "ping":
            return _result(rid, {})
        if method == "tools/list":
            return _result(rid, {"tools": TOOLS})
        if method == "tools/call":
            return _result(rid, self.call(params.get("name"), params.get("arguments")))
        return _error(rid, -32601, "method not found")

    def call(self, name: object, args: object) -> dict:
        tool = {"skills_list": self.skills_list, "skill_read": self.skill_read}.get(
            name if isinstance(name, str) else ""
        )
        if tool is None:
            return _text("unknown tool", error=True)
        try:
            text = tool({} if args is None else args)
        except ToolError as exc:
            return _text(str(exc), error=True)
        except (UnicodeError, ValueError, OSError):
            # Never let one odd argument end the server: the session keeps its skills tool.
            return _text("that request could not be served", error=True)
        return _text(text[: FILE_MAX_BYTES + 256])


def main() -> int:
    bundle = os.environ.get(ENV_BUNDLE, "")
    if not bundle.startswith("/") or "\0" in bundle or "\n" in bundle:
        sys.stderr.write(
            f"battlelab skills tool: {ENV_BUNDLE} must be an absolute path; BattleLab sets it "
            "when it starts a session from a specialist\n"
        )
        return 2
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer, Server(bundle))
    except (BrokenPipeError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
