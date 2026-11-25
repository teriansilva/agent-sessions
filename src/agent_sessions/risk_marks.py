"""Advisory risk marks for the commands an API agent asks to run or ran (#1339 Phase 2b).

A FIXED, short list of risky shapes (the operator's "dangerous commands can be marked"). It is
advisory, never a guarantee: an unmarked command is not "safe", and the list is deliberately not
grown to chase evasions (quoting, aliases, scripts, variables). What it buys is a visible reason
on the shapes an operator most often regrets. It removes no choice (operator direction, PR #1342
comment 89972): an "approve always" option on such a request carries the same mark.

Pure functions only: the server's snapshot projection and the native worker's approve gate call
the same ``classify`` on the same command, so a card and its tool row can never disagree.
"""

from __future__ import annotations

import os
import shlex

RISKY = "risky"
NONE = "none"
UNKNOWN = "unknown"

#: The shells whose ``-c``/``-lc`` script is unwrapped and classified as the command itself.
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "fish"})
_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "\n"})
_REDIRECTS = frozenset({">", ">>", "&>", "&>>", "1>", "2>", "1>>", "2>>"})
_DOWNLOADERS = frozenset({"curl", "wget"})
MAX_COMMAND = 8192


def _result(reasons: list[str]) -> dict:
    unique = list(dict.fromkeys(reasons))
    return {"level": RISKY if unique else NONE, "reasons": unique}


def unknown() -> dict:
    return {"level": UNKNOWN, "reasons": []}


def _tokens(command) -> list[str] | None:
    """The command as words. A list is taken as argv; a string is split like a POSIX shell.
    Anything else, too long, or unparseable is None (→ ``unknown``)."""
    if isinstance(command, list):
        if not command or not all(isinstance(t, str) for t in command):
            return None
        if sum(len(t) for t in command) > MAX_COMMAND:
            return None
        return list(command)
    if not isinstance(command, str) or not command.strip() or len(command) > MAX_COMMAND:
        return None
    # An unquoted newline separates commands for a shell; shlex would read it as a space and
    # merge two commands into one (Hermes 5911). For this ADVISORY mark, every newline ends a
    # command (a quoted one only makes the mark stricter, never laxer).
    command = command.replace("\r", "\n").replace("\n", " ; ")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars="|&;<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return None


#: Words that run ANOTHER command line this classifier cannot see whole: any shell left in the
#: classified words, plus eval-style launchers. Their presence makes the command `unknown`
#: (fail closed: no standing grant), never `none` (Hermes on #1342).
_OPAQUE = _SHELLS | frozenset(
    {
        "eval",
        "exec",
        "source",
        ".",
        "xargs",
        "parallel",
        "watch",
        # launchers that run a command line given in their own arguments (Hermes on #1342)
        "timeout",
        "ionice",
        "chrt",
        "taskset",
        "setsid",
        "stdbuf",
        "flock",
        "unbuffer",
        "script",
        "busybox",
        "runuser",
        "systemd-run",
        "nsenter",
        "unshare",
        "chroot",
        "firejail",
        "bwrap",
        "sg",
        "<launcher-with-options>",  # see _program
    }
)
#: Prefixes looked through to the command they run — but ONLY bare: with any option of their own
#: (`env -i`, `nice -n 10`, `command -p`) the effective command is not resolved here, so the
#: segment is opaque (`unknown`), never classified on the wrong word (Hermes on #1342).
_LOOK_THROUGH = frozenset({"env", "nice", "nohup", "time", "command"})


def unwrap(tokens: list[str]) -> list[str] | None:
    """EXACTLY ``<shell> -<flags with c> 'script'`` → the script's own words (one level), else the
    tokens unchanged. None — `unknown` — when the script cannot be parsed or anything follows it
    (`sh -c 'printf ok' && rm -rf data` runs more than the script: Hermes on #1342)."""
    if len(tokens) >= 3 and os.path.basename(tokens[0]) in _SHELLS:
        flags = tokens[1]
        if flags.startswith("-") and "c" in flags[1:]:
            return _tokens(tokens[2]) if len(tokens) == 3 else None
    return tokens


def command_words(command) -> list[str] | None:
    tokens = _tokens(command)
    return None if tokens is None else unwrap(tokens)


def _segments(tokens: list[str]) -> list[list[str]]:
    out, cur = [], []
    for tok in tokens:
        if tok in _SEPARATORS:
            if cur:
                out.append(cur)
            cur = []
            if tok == "|":
                out.append(["|"])
        else:
            cur.append(tok)
    if cur:
        out.append(cur)
    return out


def _flags(args: list[str]) -> set[str]:
    out: set[str] = set()
    for a in args:
        if a.startswith("--"):
            out.add(a)
        elif a.startswith("-") and len(a) > 1:
            out.update(f"-{c}" for c in a[1:])
    return out


def _outside(path: str, cwd: str | None) -> bool:
    if not path or path.startswith("-") or cwd is None:
        return False
    if path == "/dev/null" or path.startswith(("/dev/fd/", "/dev/std")):
        return False
    expanded = os.path.expanduser(path)
    full = os.path.normpath(expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded))
    root = os.path.normpath(cwd)
    return not (full == root or full.startswith(root.rstrip("/") + "/"))


def _program(seg: list[str]) -> tuple[str, list[str]]:
    words = list(seg)
    # `sudo` is reported itself; bare `env`/`nice`/… (by basename: `/usr/bin/env` too) and
    # `VAR=value` assignments are looked through. A look-through launcher WITH options is opaque.
    while words:
        head = os.path.basename(words[0])
        if head in _LOOK_THROUGH:
            if len(words) > 1 and words[1].startswith("-"):
                return "<launcher-with-options>", []
            words = words[1:]
        elif "=" in words[0] and not words[0].startswith("-"):
            words = words[1:]
        else:
            break
    if not words:
        return "", []
    return os.path.basename(words[0]), words[1:]


def classify(command, cwd: str | None = None) -> dict:
    """``{level: risky|none|unknown, reasons: [...]}`` for one command (string or argv)."""
    tokens = command_words(command)
    if tokens is None:
        return unknown()
    reasons: list[str] = []
    segments = _segments(tokens)
    # A shell/eval inside the words runs a command line this cannot see whole: unless a listed
    # shape already marks it risky (with its reason), it is `unknown`, never `none`.
    opaque = any(seg != ["|"] and _program(seg)[0] in _OPAQUE for seg in segments)
    for i, seg in enumerate(segments):
        if seg == ["|"]:
            continue
        prog, args = _program(seg)
        flags = _flags(args)
        if prog in {"sudo", "su", "doas"}:
            reasons.append("runs as another user (sudo)")
        if prog == "rm" and ({"-r", "-R", "--recursive"} & flags or {"-f", "--force"} & flags):
            reasons.append("deletes files recursively or forcibly")
        if prog == "find" and "-delete" in args:
            reasons.append("deletes files recursively or forcibly")
        if prog == "git" and args:
            sub, rest = args[0], args[1:]
            rf = _flags(rest)
            if sub == "push" and (
                {"-f", "--force", "--force-with-lease"} & rf or any(a.startswith("+") for a in rest)
            ):
                reasons.append("force-pushes over remote history")
            if sub == "reset" and "--hard" in rest:
                reasons.append("discards local changes (git reset --hard)")
            if sub == "clean" and ({"-f", "--force"} & rf):
                reasons.append("deletes untracked files (git clean)")
            if sub == "branch" and "-D" in rest:
                reasons.append("force-deletes a branch")
            if sub == "checkout" and "--" in rest and "." in rest[rest.index("--") + 1 :]:
                reasons.append("discards local changes (git checkout -- .)")
        if prog in {"chmod", "chown", "chgrp"} and ({"-R", "--recursive"} & flags):
            reasons.append("changes permissions or ownership recursively")
        if prog in {"dd", "shred"} or prog.startswith("mkfs"):
            reasons.append("overwrites a disk or file at a low level")
        if prog in {"kill", "pkill", "killall"}:
            reasons.append("kills processes")
        if prog in _SHELLS and i > 0 and segments[i - 1] == ["|"]:
            prev = next((s for s in reversed(segments[: i - 1]) if s != ["|"]), None)
            if prev is not None and _program(prev)[0] in _DOWNLOADERS:
                reasons.append("pipes a download into a shell")
        for j, tok in enumerate(seg):
            if tok in _REDIRECTS and j + 1 < len(seg) and _outside(seg[j + 1], cwd):
                reasons.append("writes outside the session folder")
        if prog in {"tee", "cp", "mv", "install", "ln"} and args:
            target = [a for a in args if not a.startswith("-")]
            if target and _outside(target[-1], cwd):
                reasons.append("writes outside the session folder")
        if prog == "rm":
            if any(_outside(a, cwd) for a in args if not a.startswith("-")):
                reasons.append("writes outside the session folder")
    if opaque and not reasons:
        return unknown()
    return _result(reasons)


#: Claude file tools whose ``file_path``/``notebook_path`` is a write.
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
#: Tools that run a command (their ``command`` input is classified).
COMMAND_TOOLS = frozenset({"Bash", "commandExecution", "command"})


def classify_tool(tool: str | None, tool_input, cwd: str | None) -> dict | None:
    """Risk for one tool call by name and input, or None when the tool is neither a command nor
    a file write (nothing on the fixed list applies to it)."""
    if tool in COMMAND_TOOLS:
        if isinstance(tool_input, dict):
            return classify(tool_input.get("command"), cwd)
        return classify(tool_input, cwd)
    if tool in _WRITE_TOOLS:
        if not isinstance(tool_input, dict):
            return unknown()
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not isinstance(path, str) or not path:
            return unknown()
        return _result(["writes outside the session folder"] if _outside(path, cwd) else [])
    return None
