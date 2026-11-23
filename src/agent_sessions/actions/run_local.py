"""Run locally (#2248 P3): what "Run a command" and "Run a script" share.

Both run ONE operator-authored program on this machine, in a project's folder, as the user the app
runs as. This module is the reviewed code that decides what runs and how; `run_command` and
`run_script` only say which program and which pins. Read ``docs/invariants/actions.md`` (Run a
command / Run a script) before changing anything here.

**What goes into the run.**

* **Never a shell.** The run is ONE ``subprocess.Popen`` of a literal argv list: the containment
  wrapper (`scopedspawn.wrap`, ``systemd-run --user --scope``), then ``env -i`` with the clean
  environment, then the program's own argv. No string is ever parsed as a command line.
* **Operator-authored.** The argv is a list of elements, each a FIXED string or ``{from: field}``
  (a typed value from a webhook delivery that fills that WHOLE element). The program (argv[0]),
  any element the operator marks as an ``option``, and the element right after an option (its
  value) are always fixed. A delivered value is never concatenated into another string, never
  chooses the program or the script, and one that starts with ``-`` is refused at the run unless a
  fixed ``--`` comes before it (a program would read it as an option).
* **The environment is clean**: ``PATH`` (fixed), ``HOME`` (the run-as user's own), ``LANG`` and
  ``TERM=dumb``; then only the variables the action declares, fixed or typed from the delivery.
  Nothing of the app's own environment is passed on. A delivered value never fills a variable a
  loader or interpreter reads as code (`_COMMAND_ENV`).
* **No secret as a value.** A fixed argument or variable that looks like a credential or holds a
  stored secret is refused at save (`ask_store.guard_text`); output is redacted the same way.

**Where it runs.** The project's folder, resolved again at every run (`resolve_mission_cwd`), must
equal the folder pinned at consent and be inside the roots and outside ``folder_exclusions``
(`automation_runner.scope_ok` / `folder_ok`, fail closed). A script must be a regular file inside
that folder (no symbolic link, no hidden segment, its real path inside the folder).

**Containment fails closed.** The run needs a transient systemd scope: `scopedspawn.wrap` returns
the bare argv when scopes are switched off or unavailable, and then the run is REFUSED and nothing
starts. The program is its own session leader inside the scope. When it ends (or at its time limit,
or when the run is cancelled), every process it left is killed: ``killpg`` on its own validated
process group (never 0 or 1, never ours), and ``systemctl --user kill`` on its own scope (which
catches a descendant that left the group). This is lifecycle containment, NOT a sandbox: the
program runs as the user, and can read and write what the user can and reach the network.

**Pinned at consent, checked at the launch.** The program's real path and sha256 (and its
interpreter's, when it is a ``#!`` script), a script's sha256 and its interpreter chain, the
project's folder and the run-as user are pins; a change is a drift, so the automation pauses and
asks again. At the run they are hashed AGAIN through an open file descriptor:

* a SCRIPT is read once through its descriptor, hashed, copied into a sealed in-memory file
  (``memfd``, sealed against every write), and THAT is executed (``/proc/self/fd/N``, passed to the
  child), so the bytes that run are exactly the bytes that were checked, whatever happens to the
  file on disk meanwhile;
* a PROGRAM is executed by its real path (so it sees its own name and finds its own files); its
  descriptor's identity (device, inode, size, times) must still be what the path names right before
  the launch. Replacing a program in the instant between that check and the exec needs write access
  to it as the run-as user, which could change what the command does anyway: the documented
  residual.

**Outcomes.** Exit 0 is ``ok``, anything else ``failed`` with the code, the time limit ``failed``
("stopped after N minutes"); a cancelled run kills the tree and is recorded, and a run whose outcome
is unknown after a crash is ``interrupted`` and never replayed (`automations_store`). The run
records ``exit_code``, up to `OUTPUT_CAP` of stdout and of stderr (redacted, truncation marked),
and the typed fields the action declares, read from a JSON object on the LAST line of stdout.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import math
import os
import pwd
import re
import selectors
import signal
import stat
import subprocess
import threading
import time
from dataclasses import dataclass, field

from .. import automations as model
from .. import scopedspawn, webhook_events
from . import plugins

log = logging.getLogger(__name__)

#: The fixed search path, for the program's lookup AND inside the run.
CLEAN_PATH = "/usr/local/bin:/usr/bin:/bin"
LANG = "C.UTF-8"
#: The fixed launcher that starts the program with ONLY the given environment.
ENV_BIN = "/usr/bin/env"
SYSTEMCTL_BIN = "/usr/bin/systemctl"

TIMEOUT_DEFAULT_MIN = 5
TIMEOUT_MAX_MIN = 30
#: Seconds per minute of the time limit (a test seam: tests run "minutes" of a fraction).
MINUTE_S = 60.0
#: What is kept of stdout and of stderr each; the rest is counted and marked.
OUTPUT_CAP = 64 * 1024
#: The tail of stdout kept apart, so the JSON last line is read even past the cap.
TAIL_CAP = 4096
#: How long the run waits for the output pipes to close once the tree is killed.
DRAIN_S = 2.0
POLL_S = 0.05

ARGV_MAX = 32
ELEMENT_MAX = 1024
ENV_MAX = 16
OUTPUTS_MAX = 8
OUTPUT_TEXT_MAX = 200
SCRIPT_MAX_BYTES = 1024 * 1024
PROGRAM_MAX_BYTES = 512 * 1024 * 1024
SHEBANG_MAX = 255
INTERPRETER_DEPTH = 4

OUTPUT_TYPES = ("number", "text", "yes_no")
_ENV_NAME = re.compile(r"\A[A-Z_][A-Z0-9_]{0,63}\Z")
_BARE_PROGRAM = re.compile(r"\A[A-Za-z0-9._+-]{1,128}\Z")
_UNIT = re.compile(r"\Aas-action-[A-Za-z0-9_.-]{1,8}-[0-9a-f]{8}\.scope\Z")

#: Set by the run itself; an action never declares these.
FIXED_ENV = ("HOME", "LANG", "PATH", "TERM")
#: Variables a loader, a shell or an interpreter reads as CODE, a command or a search path. A
#: delivered value never fills one (the operator may still set a fixed value they wrote).
_COMMAND_ENV = re.compile(
    r"\A(LD_\w*|BASH_ENV|ENV|BASH_FUNC_\w*|SHELLOPTS|BASHOPTS|IFS|PS4|PROMPT_COMMAND|"
    r"PYTHON\w*|NODE_\w*|NPM_CONFIG_\w*|PERL\w*|RUBY\w*|GEM_\w*|JAVA_TOOL_OPTIONS|_JAVA_OPTIONS|"
    r"JDK_JAVA_OPTIONS|CLASSPATH|GIT_\w*|SSH_\w*|\w*_COMMAND|PAGER|\w*_PAGER|EDITOR|VISUAL|SHELL|"
    r"GCONV_PATH|LOCPATH|NLSPATH|MALLOC_\w*|GLIBC_TUNABLES|XDG_\w*|TMPDIR|LUA_\w*|PHP\w*|"
    r"DOTNET_\w*|GOFLAGS|CARGO_\w*|RUSTC_\w*|R_\w*|MAKEFLAGS|MFLAGS|CC|CXX|CFLAGS|LDFLAGS)\Z"
)
#: Programs that run ANOTHER command taken from their arguments (or are a shell). Refused as the
#: program of "Run a command": a delivered argument would choose what runs. Write a script instead.
LAUNCHERS = frozenset(
    {
        "sh", "bash", "dash", "zsh", "ksh", "mksh", "pdksh", "ash", "fish", "csh", "tcsh",
        "busybox", "toybox", "env", "xargs", "parallel", "sudo", "doas", "su", "pkexec",
        "runuser", "nohup", "setsid", "timeout", "nice", "ionice", "stdbuf", "chroot", "unshare",
        "nsenter", "systemd-run", "flock", "watch", "script", "time", "ssh", "rsh", "sshpass",
        "expect", "strace", "ltrace", "gdb", "taskset", "chrt", "prlimit", "firejail", "bwrap",
        "proot", "faketime", "torsocks", "proxychains", "proxychains4", "tmux", "screen", "dtach",
        "at", "batch", "crontab", "eval", "exec", "command", "builtin",
        # Package runners: their first argument names a package to fetch and run.
        "npx", "pnpx", "bunx", "uvx",
    }
)  # fmt: skip


#: Programs that read CODE, or the program to run, from their arguments (#2279 review). For each:
#: the options whose value is the program text (`-c` for python), and how many fixed positional
#: arguments name the program before a delivered value may follow (2 for a program whose first
#: positional is a subcommand: ``deno run x.ts``). Matched on the basename, versions included
#: (``python3.12``, ``perl5.36``, ``lua5.4``).
_CODE_READERS: tuple[tuple[re.Pattern, frozenset[str], int], ...] = tuple(
    (re.compile(rf"\A(?:{names})(?:[0-9][0-9.]*)?\Z", re.IGNORECASE), frozenset(opts), need)
    for names, opts, need in (
        ("python|pypy|python[0-9]*-config", ("-c",), 1),
        ("perl", ("-e", "-E"), 1),
        ("ruby|irb|jruby|truffleruby", ("-e",), 1),
        ("node|nodejs", ("-e", "--eval", "-p", "--print"), 1),
        ("deno|bun", ("-e", "--eval", "-p", "--print"), 2),
        ("php|php-cgi|hhvm", ("-r", "-B", "-R", "-E"), 1),
        ("lua|luajit", ("-e",), 1),
        ("awk|gawk|mawk|nawk|busybox-awk", ("-f", "--file", "-e", "--source"), 1),
        ("sed|gsed", ("-e", "-f", "--expression", "--file"), 1),
        ("jq|gojq|jaq|yq", ("-f", "--from-file"), 1),
        ("rscript|r", ("-e",), 1),
        ("julia", ("-e", "-E", "--eval", "--print"), 1),
        ("tclsh|wish|jimsh", (), 1),
        ("guile|racket|sbcl|clisp|ecl|ocaml|swift|groovy|scala|kotlin|kotlinc|java|jshell"
         "|elixir|iex|erl|escript|pwsh|powershell|osascript|bc|dc|m4|gdb|lldb|vim|nvim|emacs"
         "|ghci|runghc|runhaskell|dart|crystal|nim|zig|v|raku|rakudo|pike|scheme|chicken|csi",
         ("-e", "-c", "--eval", "-x", "--exec", "--command"), 1),
        # Subcommand runners: `npm run build`, `go run main.go`, `uv run x.py`.
        ("npm|pnpm|yarn|uv|pipx|poetry|pdm|hatch|rye|cargo|go|dotnet|bundle|mix|gradle|mvn", (), 2),
    )
)  # fmt: skip
#: Programs that read code from ANY argument (``make X=$(shell …)``, a SQL statement): a delivered
#: value never fills any of their arguments.
_NEVER_DELIVERED = re.compile(
    r"\A(?:make|gmake|bmake|remake|sqlite3?|duckdb|psql|mysql|mariadb|redis-cli|mongo|mongosh"
    r"|cqlsh|clickhouse-client|sqlcmd)(?:[0-9][0-9.]*)?\Z",
    re.IGNORECASE,
)
#: Options that take TWO values (``jq --arg name value``): neither is the program text.
_TWO_VALUES = frozenset({"--arg", "--argjson", "--slurpfile", "--rawfile", "--jsonargs"})


class Refused(Exception):
    """The run is refused before anything started. ``drift``: the operator must approve again."""

    def __init__(self, reason: str, *, drift: bool = False) -> None:
        super().__init__(reason)
        self.reason = reason
        self.drift = drift


# ---- validation (pure: no filesystem) ------------------------------------------------------------


def _element(raw: object, where: str, mapped: dict) -> dict:
    if not isinstance(raw, dict):
        raise model.AutomationError(f"{where} must be an object")
    keys = set(raw)
    if keys == {"from"}:
        name = raw["from"]
        if not isinstance(name, str) or name not in mapped:
            raise model.AutomationError(f"{where} takes a value from the delivery it doesn't map")
        return {"from": name}
    if "from" in keys:
        raise model.AutomationError(
            f"{where}: a value from the delivery fills a whole argument, never an option"
        )
    if not keys <= {"value", "option"} or "value" not in keys:
        raise model.AutomationError(f"{where} is {{value, option?}} or {{from}}")
    value = raw["value"]
    if not isinstance(value, str) or len(value) > ELEMENT_MAX:
        raise model.AutomationError(f"{where} must be text of at most {ELEMENT_MAX} characters")
    if "\x00" in value or not (value.isprintable() or value == ""):
        raise model.AutomationError(f"{where} must be printable text")
    option = raw.get("option", False)
    if not isinstance(option, bool):
        raise model.AutomationError(f"{where}.option must be true or false")
    return {"value": value, "option": True} if option else {"value": value}


def is_option(e: dict) -> bool:
    """A fixed element a program reads as an option: one marked so, or ANY that starts with ``-``
    (exactly ``--`` ends the options and is not one). Flag or no flag (#2279 review)."""
    if "value" not in e:
        return False
    v = e["value"]
    return bool(e.get("option")) or (v.startswith("-") and v != "--")


def option_slot_problem(elements: list[dict], what: str, *, offset: int = 0) -> str | None:
    """A delivered value directly after an option: it would be that option's value (``-c``,
    ``-e``, ``--eval``, ``-m``, ``-exec`` …), so it is refused."""
    for i, e in enumerate(elements[:-1]):
        if is_option(e) and "from" in elements[i + 1]:
            return (
                f"{what}[{i + 1 + offset}] is the value of the option {e['value']!r}: "
                "it is always fixed"
            )
    return None


def _family(name: str) -> tuple[frozenset[str], int] | None:
    for rx, opts, need in _CODE_READERS:
        if rx.match(name):
            return opts, need
    return None


def _is_code_option(v: str, code_opts: frozenset[str]) -> bool:
    if v in code_opts:
        return True
    # A short-option cluster ending in a code letter: perl -ne, -lane; python -Ic.
    letters = {o[1] for o in code_opts if len(o) == 2 and o[0] == "-" and o[1] != "-"}
    return bool(re.fullmatch(r"-[A-Za-z]+", v)) and v[-1] in letters


def code_slot_problem(names: list[str], args: list[dict]) -> str | None:
    """For a program that reads code or a program from its arguments (`_CODE_READERS`), a
    delivered value may follow only the operator's own program text: the fixed positional
    arguments that name the script or the code (``python3 x.py {v}``, ``python3 -c "code" {v}``),
    never before them (``python3 {v}``, ``awk {v}``). An option's value is not program text unless
    the option carries code (``-c``); any other option is assumed to take the next element as its
    value (conservative: put interpreter flags in the script or an environment variable). For a
    program in `_NEVER_DELIVERED`, no argument comes from the delivery at all."""
    if not any("from" in a for a in args):
        return None
    for name in names:
        if _NEVER_DELIVERED.match(name):
            return (
                f"{name} reads commands from any argument: a value from the delivery can't fill one"
            )
    families = [f for f in (_family(n) for n in names) if f is not None]
    for code_opts, need in families:
        seen, skip, ended = 0, 0, False
        for i, a in enumerate(args):
            if "from" in a:
                if seen < need:
                    words = "its script or program text" if need == 1 else "its command and script"
                    return (
                        f"argument {i + 1} comes from the delivery before {words}: "
                        f"{names[-1]} could run it as code; a value from the delivery may only "
                        "follow the program text you wrote"
                    )
                continue
            if skip:
                skip -= 1
                continue
            v = a["value"]
            if v == "--" and not ended:
                ended = True
                continue
            if not ended and is_option(a):
                if "=" in v or _is_code_option(v, code_opts):
                    continue  # its value is attached, or the next element IS the program text
                skip = 2 if v in _TWO_VALUES else 1
                continue
            seen += 1
    return None


def program_names(word: str, real: str | None = None) -> list[str]:
    out = [os.path.basename(word)]
    if real and os.path.basename(real) not in out:
        out.append(os.path.basename(real))
    return out


def validate_elements(raw: object, what: str, mapped: dict, *, first_fixed: bool) -> list[dict]:
    if not isinstance(raw, list) or len(raw) > ARGV_MAX:
        raise model.AutomationError(f"{what} must be a list of at most {ARGV_MAX} arguments")
    out = [_element(e, f"{what}[{i}]", mapped) for i, e in enumerate(raw)]
    if first_fixed:
        if not out or "value" not in out[0]:
            raise model.AutomationError(f"{what}[0], the program, is always fixed")
        if out[0].get("option"):
            raise model.AutomationError(f"{what}[0] is the program, not an option")
    why = option_slot_problem(out, what)
    if why:
        raise model.AutomationError(why)
    return out


def validate_env(raw: object, mapped: dict) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > ENV_MAX:
        raise model.AutomationError(f"env must be a list of at most {ENV_MAX} variables")
    out: list[dict] = []
    for i, e in enumerate(raw):
        where = f"env[{i}]"
        if not isinstance(e, dict) or set(e) not in ({"name", "value"}, {"name", "from"}):
            raise model.AutomationError(f"{where} is {{name, value}} or {{name, from}}")
        name = e["name"]
        if not isinstance(name, str) or not _ENV_NAME.match(name):
            raise model.AutomationError(f"{where}: a variable name is A-Z, 0-9 and _")
        if name in FIXED_ENV:
            raise model.AutomationError(f"{where}: {name} is set by the run itself")
        if "from" in e:
            if not isinstance(e["from"], str) or e["from"] not in mapped:
                raise model.AutomationError(
                    f"{where} takes a value from the delivery it doesn't map"
                )
            if _COMMAND_ENV.match(name):
                raise model.AutomationError(
                    f"{where}: {name} is read as code or a command; a value from the delivery "
                    "never fills it"
                )
            out.append({"name": name, "from": e["from"]})
        else:
            v = e["value"]
            if not isinstance(v, str) or len(v) > ELEMENT_MAX or "\x00" in v:
                raise model.AutomationError(f"{where}: a value is text of at most {ELEMENT_MAX}")
            out.append({"name": name, "value": v})
    names = [e["name"] for e in out]
    if len(set(names)) != len(names):
        raise model.AutomationError("env names a variable twice")
    return sorted(out, key=lambda e: e["name"])


def validate_outputs(raw: object) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or len(raw) > OUTPUTS_MAX:
        raise model.AutomationError(f"outputs must be an object of at most {OUTPUTS_MAX} fields")
    out: dict[str, str] = {}
    for name, typ in raw.items():
        if not isinstance(name, str) or not webhook_events.FIELD_RE.match(name):
            raise model.AutomationError(f"output {name!r} is not a field name (a-z, 0-9, _)")
        if name == "exit_code":
            raise model.AutomationError("exit_code is always an output; declare other fields")
        if typ not in OUTPUT_TYPES:
            raise model.AutomationError(f"output {name}: type must be one of {list(OUTPUT_TYPES)}")
        out[name] = typ
    return dict(sorted(out.items()))


def validate_timeout(raw: object) -> int:
    if raw is None:
        return TIMEOUT_DEFAULT_MIN
    if not isinstance(raw, int) or isinstance(raw, bool) or not 1 <= raw <= TIMEOUT_MAX_MIN:
        raise model.AutomationError(f"timeout_minutes must be 1 to {TIMEOUT_MAX_MIN}")
    return raw


def check_mapping_used(mapped: dict, *element_lists: list[dict]) -> None:
    used = {e["from"] for lst in element_lists for e in lst if "from" in e}
    unused = sorted(set(mapped) - used)
    if unused:
        raise model.AutomationError(f"from_delivery: {unused} is mapped but fills no argument")


def validate_program_word(word: str) -> None:
    if word.startswith("/"):
        if "\x00" in word or len(word) > 4096:
            raise model.AutomationError("the program must be an absolute path or a name")
        return
    if not _BARE_PROGRAM.match(word):
        raise model.AutomationError(
            "the program is an absolute path or a name looked up in "
            f"{CLEAN_PATH}; to run a file of the project, use Run a script"
        )


# ---- the filesystem: programs, scripts, interpreters ---------------------------------------------


_O_READ = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NOCTTY
#: ``(dev, ino, size, mtime_ns, ctime_ns) → sha256`` for the consent pins (a write moves ctime,
#: which a user can't set back). The launch never uses it: it hashes through its own descriptor.
_HASHES: dict[tuple, str] = {}
_HASHES_LOCK = threading.Lock()


def _identity(st: os.stat_result) -> tuple:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _hash_fd(fd: int, cap: int) -> str:
    h = hashlib.sha256()
    total = 0
    offset = 0
    while True:
        chunk = os.pread(fd, 1 << 20, offset)
        if not chunk:
            return h.hexdigest()
        total += len(chunk)
        if total > cap:
            raise Refused(f"the file is larger than {cap // (1024 * 1024)} MiB")
        h.update(chunk)
        offset += len(chunk)


def _open_regular(real: str, what: str) -> tuple[int, os.stat_result]:
    """``real`` opened for reading without following a link at its last component; must be a
    regular file. Raises `Refused`."""
    try:
        fd = os.open(real, _O_READ)
    except FileNotFoundError:
        raise Refused(f"{what} {real} no longer exists", drift=True) from None
    except OSError as e:
        raise Refused(f"{what} {real} can't be read ({e.strerror})", drift=True) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f"{what} {real} is not a regular file", drift=True)
    except BaseException:
        os.close(fd)
        raise
    return fd, st


def file_pin(real: str, what: str, cap: int = PROGRAM_MAX_BYTES) -> dict:
    """``{real, sha256}`` for the consent pins (cached by identity). Raises `Refused`."""
    fd, st = _open_regular(real, what)
    try:
        key = (real, *_identity(st))
        with _HASHES_LOCK:
            known = _HASHES.get(key)
        if known is None:
            known = _hash_fd(fd, cap)
            with _HASHES_LOCK:
                if len(_HASHES) > 256:
                    _HASHES.clear()
                _HASHES[key] = known
        return {"real": real, "sha256": known}
    finally:
        os.close(fd)


def _executable(real: str) -> bool:
    return os.access(real, os.X_OK)


def resolve_program(word: str) -> str:
    """The program's real, absolute path: an absolute path, or a name looked up in `CLEAN_PATH`
    (the PATH the run has). Raises `Refused` (a drift: what was approved can't run)."""
    if word.startswith("/"):
        candidate = word
    else:
        candidate = next(
            (
                os.path.join(d, word)
                for d in CLEAN_PATH.split(":")
                if os.path.isfile(os.path.join(d, word)) and _executable(os.path.join(d, word))
            ),
            "",
        )
        if not candidate:
            raise Refused(f"the program {word} is not found in {CLEAN_PATH}", drift=True)
    real = os.path.realpath(candidate)
    try:
        st = os.stat(real)
    except OSError:
        raise Refused(f"the program {word} does not exist", drift=True) from None
    if not stat.S_ISREG(st.st_mode) or not _executable(real):
        raise Refused(f"the program {word} is not an executable file", drift=True)
    if "=" in real:
        raise Refused(f"the program's path {real} contains '='", drift=True)
    for name in (os.path.basename(word), os.path.basename(real)):
        if name in LAUNCHERS:
            raise Refused(
                f"{name} runs other commands from its arguments (or is a shell); write a script "
                "and use Run a script",
                drift=True,
            )
    return real


def parse_shebang(head: bytes) -> tuple[str, str | None] | None:
    """``(interpreter, optional argument)`` from a ``#!`` line, or None (not a script)."""
    if not head.startswith(b"#!"):
        return None
    line = head[2:].split(b"\n", 1)[0]
    if len(line) > SHEBANG_MAX:
        raise Refused("the #! line is longer than the kernel reads", drift=True)
    try:
        text = line.decode("utf-8").strip()
    except UnicodeDecodeError:
        raise Refused("the #! line is not text", drift=True) from None
    if not text:
        raise Refused("the #! line names no interpreter", drift=True)
    parts = text.split(None, 1)
    return parts[0], (parts[1].strip() if len(parts) > 1 else None)


def interpreter_chain(head: bytes, *, fresh: bool = False, depth: int = 0) -> list[dict]:
    """The interpreters the kernel will run for a file starting with ``head``: each
    ``{path, real, sha256}``, in order. ``#!/usr/bin/env NAME`` pins env AND what NAME resolves to
    in `CLEAN_PATH`. ``fresh``: hash through a descriptor now (the launch), never the cache."""
    parsed = parse_shebang(head)
    if parsed is None:
        return []
    if depth >= INTERPRETER_DEPTH:
        raise Refused("the interpreter chain is too deep", drift=True)
    interp, arg = parsed
    if not interp.startswith("/"):
        raise Refused(f"the #! interpreter {interp} is not an absolute path", drift=True)
    real = os.path.realpath(interp)
    pin = _pin_now(real, "the interpreter", fresh)
    chain = [{"path": interp, **pin}]
    nested_head = _head(real)
    if os.path.basename(real) == "env":
        if not arg or len(arg.split()) != 1 or arg.startswith("-"):
            raise Refused(
                "#!/usr/bin/env takes exactly one interpreter name here (no options)", drift=True
            )
        target = _lookup(arg)
        tpin = _pin_now(target, "the interpreter", fresh)
        chain.append({"path": arg, **tpin})
        nested_head = _head(target)
    return chain + interpreter_chain(nested_head, fresh=fresh, depth=depth + 1)


def _lookup(name: str) -> str:
    if not _BARE_PROGRAM.match(name):
        raise Refused(f"the interpreter {name} is not a plain name", drift=True)
    for d in CLEAN_PATH.split(":"):
        p = os.path.join(d, name)
        if os.path.isfile(p) and _executable(p):
            return os.path.realpath(p)
    raise Refused(f"the interpreter {name} is not found in {CLEAN_PATH}", drift=True)


def _head(real: str) -> bytes:
    fd, _st = _open_regular(real, "the interpreter")
    try:
        return os.pread(fd, SHEBANG_MAX + 3, 0)
    finally:
        os.close(fd)


def _pin_now(real: str, what: str, fresh: bool) -> dict:
    if not fresh:
        return file_pin(real, what)
    fd, _st = _open_regular(real, what)
    try:
        return {"real": real, "sha256": _hash_fd(fd, PROGRAM_MAX_BYTES)}
    finally:
        os.close(fd)


def run_as() -> dict:
    uid = os.getuid()
    try:
        ent = pwd.getpwuid(uid)
    except KeyError:
        return {"uid": uid, "user": str(uid), "home": "/"}
    return {"uid": uid, "user": ent.pw_name, "home": ent.pw_dir}


def folder_pin(project_id: str) -> dict:
    """The project's folder identity as `resolve_mission_cwd` gives it (``{missing}`` when none)."""
    return model.resolve_mission_cwd(project_id)


def folder_problem(real: str | None) -> str | None:
    """Why the folder is not one a run may use now (roots, exclusions, browsable), or None."""
    from .. import automation_runner as runner

    if not real:
        return "the project has no folder to run in"
    ok, why = runner.folder_ok(real)
    return None if ok else why


def script_real(folder: str, rel: str) -> str:
    """The script's real path, checked to be a regular file inside ``folder`` (no link at any
    level that leaves it, no link as the file itself). Raises `Refused`."""
    from .. import automation_runner as runner
    from .. import project_dirs

    item = os.path.normpath(os.path.join(folder, rel))
    try:
        lst = os.lstat(item)
    except FileNotFoundError:
        raise Refused(f"{rel} does not exist in the project's folder", drift=True) from None
    except OSError as e:
        raise Refused(f"{rel} can't be read ({e.strerror})", drift=True) from None
    if stat.S_ISLNK(lst.st_mode):
        raise Refused(f"{rel} is a symbolic link; pick the file itself", drift=True)
    real = os.path.realpath(item)
    if not project_dirs.path_within(real, folder) or real == folder:
        raise Refused(f"{rel} is outside the project's folder", drift=True)
    if not runner.scope_ok(real):
        raise Refused(f"{rel} is outside your project folders", drift=True)
    if not stat.S_ISREG(lst.st_mode):
        raise Refused(f"{rel} is not a regular file", drift=True)
    return real


# ---- containment ---------------------------------------------------------------------------------


def containment_problem() -> str | None:
    """Why a contained launch is impossible right now, or None. Never a fallback."""
    if not scopedspawn.enabled():
        return (
            "process containment is switched off (AGENT_SESSIONS_SESSION_SCOPES=0), and a command "
            "never runs uncontained"
        )
    if not scopedspawn.available():
        return (
            "process containment is unavailable (systemd-run --user can't make a scope), and a "
            "command never runs uncontained"
        )
    return None


def _launcher_env() -> dict[str, str]:
    """What the containment wrapper itself needs to reach the user manager. ``env -i`` strips it
    before the program starts."""
    out = {"PATH": CLEAN_PATH}
    for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
        if os.environ.get(k):
            out[k] = os.environ[k]
    return out


def _valid_pgid(pgid: object) -> bool:
    """A process group we may signal: our own child's, never 0/1, never our own group."""
    return type(pgid) is int and pgid > 1 and pgid != os.getpgrp() and pgid != os.getpid()


def _kill_tree(pid: int, unit: str | None) -> None:
    """SIGKILL the program's process group (validated) and its whole scope. Best effort; the
    caller has NOT reaped the leader yet, so its pid still names its group."""
    if _valid_pgid(pid):
        try:
            if os.getpgid(pid) == pid:
                os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    else:  # pragma: no cover — a defect, never a signal to the wrong group
        log.error("run locally: refusing to signal process group %r", pid)
    if unit and _UNIT.match(unit):
        _scope_kill(unit)


def _scope_kill(unit: str) -> None:
    """SIGKILL every process in the run's own scope (one that left the process group too)."""
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(  # noqa: S603 — fixed systemctl argv and a server-minted unit name
            [SYSTEMCTL_BIN, "--user", "kill", "--signal=SIGKILL", unit],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=_launcher_env(),
            timeout=5,
            check=False,
        )


# ---- the launch ----------------------------------------------------------------------------------


@dataclass
class Launch:
    """What one run executes. ``payload`` is the program's argv as exec'd (a script's argv[0] is
    its sealed copy, ``/proc/self/fd/N``); ``shown`` is what the record and consent show."""

    payload: list[str]
    env: dict[str, str]
    cwd: str
    shown: list[str]
    pass_fds: tuple[int, ...] = ()
    owned_fds: list[int] = field(default_factory=list)
    #: Right before the spawn: raises `Refused` if what was checked is no longer what runs.
    recheck: object = None

    def close(self) -> None:
        for fd in self.owned_fds:
            with contextlib.suppress(OSError):
                os.close(fd)
        self.owned_fds.clear()


# Linux ABI constants (``linux/memfd.h``, ``linux/fcntl.h``): a Python built without them (some
# standalone builds) still gets the same calls.
_MFD_CLOEXEC = getattr(os, "MFD_CLOEXEC", 0x0001)
_MFD_ALLOW_SEALING = getattr(os, "MFD_ALLOW_SEALING", 0x0002)
_F_ADD_SEALS = getattr(fcntl, "F_ADD_SEALS", 1033)
_F_GET_SEALS = getattr(fcntl, "F_GET_SEALS", 1034)
_SEALS = (
    getattr(fcntl, "F_SEAL_SEAL", 0x0001)
    | getattr(fcntl, "F_SEAL_SHRINK", 0x0002)
    | getattr(fcntl, "F_SEAL_GROW", 0x0004)
    | getattr(fcntl, "F_SEAL_WRITE", 0x0008)
)


def _memfd(name: str) -> int:
    if hasattr(os, "memfd_create"):
        return os.memfd_create(name, _MFD_CLOEXEC | _MFD_ALLOW_SEALING)
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
    fn = getattr(libc, "memfd_create", None)
    if fn is None:
        raise OSError(38, "memfd_create is unavailable")
    fn.argtypes = (ctypes.c_char_p, ctypes.c_uint)
    fn.restype = ctypes.c_int
    fd = fn(name.encode(), _MFD_CLOEXEC | _MFD_ALLOW_SEALING)
    if fd < 0:
        err = ctypes.get_errno()
        raise OSError(err, os.strerror(err))
    return fd


def sealed_copy(data: bytes) -> int:
    """``data`` in an anonymous in-memory file sealed against every change (write, grow, shrink,
    further seals), the seals read back before it is used. The descriptor is close-on-exec; the
    launch passes it on explicitly. Raises `Refused` when that can't be had: never a fallback to
    running the file on disk."""
    try:
        fd = _memfd("battlelab-script")
    except OSError as e:
        raise Refused(f"a sealed copy of the script could not be made ({e.strerror})") from None
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        fcntl.fcntl(fd, _F_ADD_SEALS, _SEALS)
        if fcntl.fcntl(fd, _F_GET_SEALS) & _SEALS != _SEALS:
            raise Refused("the script's copy could not be sealed")
    except OSError as e:
        os.close(fd)
        raise Refused(f"the script's copy could not be sealed ({e.strerror})") from None
    except BaseException:
        os.close(fd)
        raise
    return fd


def read_checked(real: str, what: str, cap: int) -> tuple[bytes, str, os.stat_result]:
    """Read a file ONCE through its descriptor: ``(bytes, sha256, stat)``. Raises `Refused`."""
    fd, st = _open_regular(real, what)
    try:
        if st.st_size > cap:
            raise Refused(f"{what} is larger than {cap // 1024} KiB", drift=True)
        chunks, total = [], 0
        while True:
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                break
            total += len(chunk)
            if total > cap:
                raise Refused(f"{what} is larger than {cap // 1024} KiB", drift=True)
            chunks.append(chunk)
        data = b"".join(chunks)
        return data, hashlib.sha256(data).hexdigest(), st
    finally:
        os.close(fd)


def check_interpreters(head: bytes, pinned: list[dict]) -> None:
    """The interpreter chain NOW, hashed fresh, must be the pinned one."""
    now = interpreter_chain(head, fresh=True)
    if now != pinned:
        raise Refused(_interp_words(pinned, now), drift=True)


def _interp_words(old: list[dict], new: list[dict]) -> str:
    if [p.get("real") for p in old] != [p.get("real") for p in new]:
        return "the script's interpreter is a different program now"
    for a, b in zip(old, new, strict=False):
        if a.get("sha256") != b.get("sha256"):
            return f"the interpreter {a.get('real')} changed (sha256 {_short(a)} → {_short(b)})"
    return "the interpreter changed"


def _short(pin: dict | None) -> str:
    h = str((pin or {}).get("sha256") or "")
    return f"{h[:4]}…{h[-4:]}" if len(h) > 8 else h or "?"


def changed_words(what: str, old: dict | None, new: dict | None) -> str:
    return f"{what} changed since you approved it (sha256 {_short(old)} → {_short(new)})"


def build_env(env_spec: list[dict], delivered: dict[str, str]) -> dict[str, str]:
    who = run_as()
    out = {"PATH": CLEAN_PATH, "HOME": who["home"], "LANG": LANG, "TERM": "dumb"}
    for e in env_spec:
        value = e["value"] if "value" in e else delivered.get(e["from"])
        if not isinstance(value, str) or "\x00" in value:
            raise Refused(f"{e['name']} has no usable value from the delivery")
        out[e["name"]] = value
    return out


def build_args(elements: list[dict], delivered: dict[str, str]) -> list[str]:
    """The argv elements, each delivered value EXACTLY one element. A delivered value that would
    read as an option (a leading ``-``) is refused unless a fixed ``--`` came before it."""
    why = option_slot_problem(elements, "argument")
    if why:
        raise Refused(why)
    out: list[str] = []
    ended = False
    for i, e in enumerate(elements):
        if "value" in e:
            out.append(e["value"])
            if e["value"] == "--":
                ended = True
            continue
        value = delivered.get(e["from"])
        if not isinstance(value, str) or "\x00" in value:
            raise Refused(f"argument {i} has no usable value from the delivery")
        if value.startswith("-") and not ended:
            raise Refused(
                f"the delivery's {e['from']} starts with '-', which the program would read as an "
                "option; put a fixed -- before it to allow that"
            )
        out.append(value)
    return out


# ---- running it ----------------------------------------------------------------------------------


@dataclass
class Result:
    exit_code: int | None
    ended: str  # "exited" | "timeout" | "cancelled"
    stdout: bytes
    stderr: bytes
    stdout_dropped: int
    stderr_dropped: int
    tail: bytes
    duration_s: float


class _Sink:
    def __init__(self, cap: int, tail: int = 0) -> None:
        self.cap, self.tail_cap = cap, tail
        self.head = bytearray()
        self.tail = bytearray()
        self.dropped = 0

    def add(self, data: bytes) -> None:
        room = self.cap - len(self.head)
        if room > 0:
            self.head += data[:room]
        self.dropped += max(0, len(data) - max(room, 0))
        if self.tail_cap:
            self.tail += data
            if len(self.tail) > self.tail_cap:
                del self.tail[: len(self.tail) - self.tail_cap]


#: Test seam: called with the `Launch` right before the spawn, after every check.
_before_spawn = None


def spawn_and_wait(
    launch: Launch, *, run_id: str, timeout_s: float, cancel: threading.Event
) -> Result:
    """Run ``launch`` contained, to its end, its time limit or a cancellation; kill what it left.
    Raises `Refused` when it could not start contained (nothing ran)."""
    inner = [ENV_BIN, "-i", *(f"{k}={v}" for k, v in sorted(launch.env.items())), *launch.payload]
    wrapped, unit = scopedspawn.wrap(inner, engine="action", session_id=run_id)
    if unit is None:
        # The ladder fell back to the bare argv: a command never runs uncontained.
        raise Refused(
            containment_problem()
            or "process containment is unavailable, and a command never runs uncontained"
        )
    if callable(launch.recheck):
        launch.recheck()
    if callable(_before_spawn):
        _before_spawn(launch)
    started = time.monotonic()
    try:
        proc = subprocess.Popen(  # noqa: S603 — a literal argv list, never a shell
            wrapped,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=launch.cwd,
            env=_launcher_env(),
            close_fds=True,
            pass_fds=launch.pass_fds,
            start_new_session=True,
        )
    except OSError as e:
        raise Refused(f"it could not be started ({e.strerror})") from None
    finally:
        launch.close()
    out, err = _Sink(OUTPUT_CAP, TAIL_CAP), _Sink(OUTPUT_CAP)
    sel = selectors.DefaultSelector()
    for pipe, sink in ((proc.stdout, out), (proc.stderr, err)):
        os.set_blocking(pipe.fileno(), False)
        sel.register(pipe, selectors.EVENT_READ, sink)
    deadline = started + timeout_s
    ended = "exited"
    try:
        while True:
            _pump(sel, POLL_S)
            if _exited(proc.pid):
                break
            if cancel.is_set():
                ended = "cancelled"
                break
            if time.monotonic() >= deadline:
                ended = "timeout"
                break
        # Whatever it left behind (or the whole tree, at the limit or on cancel) goes now, while
        # the unreaped leader still holds its process group's id.
        _kill_tree(proc.pid, unit)
        code = proc.wait()
        drain_until = time.monotonic() + DRAIN_S
        while sel.get_map() and time.monotonic() < drain_until:
            _pump(sel, POLL_S)
    finally:
        for key in list(sel.get_map().values()):
            with contextlib.suppress(Exception):
                sel.unregister(key.fileobj)
            with contextlib.suppress(Exception):
                key.fileobj.close()
        sel.close()
        if proc.returncode is None:  # pragma: no cover — only on an unexpected error above
            _kill_tree(proc.pid, unit)
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)
    return Result(
        exit_code=code if ended == "exited" else None,
        ended=ended,
        stdout=bytes(out.head),
        stderr=bytes(err.head),
        stdout_dropped=out.dropped,
        stderr_dropped=err.dropped,
        tail=bytes(out.tail),
        duration_s=round(time.monotonic() - started, 3),
    )


def _exited(pid: int) -> bool:
    """Has the leader exited? Checked WITHOUT reaping it (``WNOWAIT``): its pid stays ours."""
    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def _pump(sel: selectors.BaseSelector, timeout: float) -> None:
    if not sel.get_map():
        time.sleep(timeout)
        return
    for key, _ in sel.select(timeout):
        try:
            data = os.read(key.fileobj.fileno(), 65536)
        except BlockingIOError:
            continue
        except OSError:
            data = b""
        if data:
            key.data.add(data)
        else:
            sel.unregister(key.fileobj)
            key.fileobj.close()


# ---- what is recorded ----------------------------------------------------------------------------


def redact(raw: bytes, dropped: int) -> str:
    """Output as stored: decoded, each line through the persisted-text boundary
    (`ask_store.guard_text`: stored secrets redacted, a credential-shaped line withheld), the
    truncation marked. Fails closed: unredactable output is not stored."""
    from .. import ask_store

    text = raw.decode("utf-8", errors="replace")
    try:
        lines = [ask_store.guard_text(line, len(line)) for line in text.split("\n")]
    except ask_store.StoreRefused:
        return "[output withheld: it could not be checked for secrets]"
    out = "\n".join(lines)
    if dropped:
        out += f"\n[… {dropped} more bytes were not kept]"
    return out


def typed_fields(tail: bytes, declared: dict[str, str]) -> tuple[dict, list[str]]:
    """The declared fields from a JSON object on the LAST line of stdout: ``(fields, notes)``. A
    missing field or one of another type is left out and noted; undeclared keys are ignored."""
    from .. import ask_store

    if not declared:
        return {}, []
    text = tail.decode("utf-8", errors="replace").rstrip("\n")
    last = text.rsplit("\n", 1)[-1].strip()
    if not last.startswith("{"):
        return {}, ["the last line of stdout is not a JSON object"]
    try:
        obj = json.loads(last)
    except (ValueError, RecursionError):
        return {}, ["the last line of stdout is not valid JSON"]
    if not isinstance(obj, dict):
        return {}, ["the last line of stdout is not a JSON object"]
    fields: dict = {}
    notes: list[str] = []
    for name, typ in declared.items():
        if name not in obj:
            notes.append(f"{name} is missing")
            continue
        v = obj[name]
        if typ == "number":
            # An int past 2**53 is not exactly a JSON number anywhere else, and `isfinite` of a
            # huge int raises (#2279 review): bounded here, floats must be finite.
            if isinstance(v, bool):
                ok = False
            elif isinstance(v, int):
                ok = abs(v) <= 2**53
            elif isinstance(v, float):
                ok = math.isfinite(v)
            else:
                ok = False
        elif typ == "yes_no":
            ok = isinstance(v, bool)
        else:
            ok = isinstance(v, str) and len(v) <= OUTPUT_TEXT_MAX
            if ok:
                try:
                    ok = ask_store.guard_text(v, len(v)) == v
                except ask_store.StoreRefused:
                    ok = False
                if not ok:
                    notes.append(f"{name} was withheld (it looks like a secret)")
                    continue
        if ok:
            fields[name] = v
        else:
            notes.append(f"{name} is not a {typ.replace('_', '/')}")
    return fields, notes


def withheld(value: str) -> bool | None:
    """Would `ask_store.guard_text` withhold or redact this fixed value? None: can't be judged."""
    from .. import ask_store

    try:
        return ask_store.guard_text(value, len(value)) != value
    except ask_store.StoreRefused:
        return None


def check_fixed_values(elements: list[dict], env: list[dict]) -> None:
    """No secret as a value: a fixed argument or variable holding a credential or a stored secret
    is refused at save (it would sit in the automation and in every process listing)."""
    for what, value in [
        (f"argument {e['value'][:24]!r}", e["value"]) for e in elements if "value" in e
    ] + [(f"variable {e['name']}", e["value"]) for e in env if "value" in e]:
        if not value:
            continue
        w = withheld(value)
        if w is None:
            raise model.AutomationError(
                "the command can't be checked for secrets right now; try again", status=503
            )
        if w:
            raise model.AutomationError(
                f"{what} looks like a credential or holds a stored secret: a secret never goes "
                "into a command as a value"
            )


# ---- the effect, shared -------------------------------------------------------------------------


def outcome_of(result: Result, minutes: int) -> tuple[str, str]:
    took = f"after {result.duration_s:g} s"
    if result.ended == "timeout":
        return "failed", (
            f"stopped after {minutes} minute{'s' if minutes != 1 else ''} (its time limit); "
            "everything it started was killed"
        )
    if result.ended == "cancelled":
        return "failed", (
            "stopped: the run was cancelled (BattleLab is stopping); everything it started was "
            "killed, and it is not run again"
        )
    code = result.exit_code
    if code == 0:
        return "ok", f"exited 0 {took}"
    if code is not None and code < 0:
        try:
            name = signal.Signals(-code).name
        except ValueError:
            name = str(-code)
        return "failed", f"ended by signal {name} {took}"
    return "failed", f"exited with code {code} {took}"


def run_effect(
    run: dict,
    *,
    launch_of,
    record: dict,
    minutes: int,
    outputs: dict[str, str],
) -> tuple[str, str]:
    """The shared effect: build the launch (``launch_of()``, which checks every pin through its
    descriptors), record the inputs, read the automation's authority once more, run it contained,
    record what came out. BLOCKING, under the effect lock (`plugins.run`)."""
    from .. import automation_runner as runner
    from .. import automations_store as store

    why = containment_problem()
    if why:
        return "refused", f"{why}; nothing ran"
    try:
        launch = launch_of()
    except Refused as r:
        if r.drift:
            runner._flag(run, r.reason)
            return "refused", f"{r.reason} — it needs your approval again; nothing ran"
        return "refused", f"{r.reason}; nothing ran"
    try:
        shown = [_guarded(a) for a in launch.shown]
        store.set_inputs(run["id"], {**record, "argv": shown, "folder": launch.cwd})
        stop = plugins.final(run)
        if stop:
            return stop
        cancel = plugins.cancel_event(run)
        try:
            result = spawn_and_wait(
                launch, run_id=run["id"], timeout_s=minutes * MINUTE_S, cancel=cancel
            )
        except Refused as r:
            if r.drift:
                runner._flag(run, r.reason)
                return "refused", f"{r.reason} — it needs your approval again; nothing ran"
            return "refused", f"{r.reason}; nothing ran"
    finally:
        launch.close()
    outcome, reason = outcome_of(result, minutes)
    # The effect happened: nothing in reading its output may lose that record (#2279 review).
    try:
        fields, notes = typed_fields(result.tail, outputs) if result.ended == "exited" else ({}, [])
    except Exception:  # noqa: BLE001 — a field that can't be read is a field that is missing
        log.warning("run locally: the output fields could not be read", exc_info=True)
        fields, notes = {}, ["the output fields could not be read"]
    done = plugins.delivered(run, outcome, reason + (f" ({'; '.join(notes)})" if notes else ""))
    store.set_inputs(
        run["id"],
        {
            **record,
            "argv": shown,
            "folder": launch.cwd,
            "output": {
                "exit_code": result.exit_code,
                "ended": result.ended,
                "duration_s": result.duration_s,
                "fields": fields,
                "stdout": _safe_redact(result.stdout, result.stdout_dropped),
                "stderr": _safe_redact(result.stderr, result.stderr_dropped),
            },
        },
    )
    return done


def _safe_redact(raw: bytes, dropped: int) -> str:
    try:
        return redact(raw, dropped)
    except Exception:  # noqa: BLE001 — fail closed: unredactable output is not stored
        log.warning("run locally: output could not be redacted", exc_info=True)
        return "[output withheld: it could not be checked for secrets]"


def _guarded(text: str) -> str:
    from .. import ask_store

    try:
        return ask_store.guard_text(text, len(text))
    except ask_store.StoreRefused:
        return "[withheld]"


# ---- consent lines, shared -----------------------------------------------------------------------


def _raw(value: object) -> str:
    """Anything the consent can't read as the expected shape is shown whole, never dropped."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def show_element(e: object, mapping: dict) -> str:
    if not isinstance(e, dict) or not ({"value"} <= set(e) or set(e) == {"from"}):
        return _raw(e)
    if "value" in e:
        return json.dumps(e["value"], ensure_ascii=False) + (" (option)" if e.get("option") else "")
    return _delivered_words(e["from"], mapping)


def _delivered_words(name: str, mapping: dict) -> str:
    m = mapping.get(name) or {}
    where = m.get("path") or name
    return f"<the delivery's {where}, as {_type_words(m.get('type'))}>"


def _type_words(t: object) -> str:
    words = {"number": "a number", "text": "short text", "yes_no": "yes/no", "choice": "a choice"}
    return words.get(str(t), str(t))


def _env_words(e: object, mapping: dict) -> str:
    if isinstance(e, dict) and set(e) == {"name", "value"}:
        return f"{e['name']}={_raw(e['value'])}"
    if isinstance(e, dict) and set(e) == {"name", "from"}:
        return f"{e['name']} ({_delivered_words(str(e['from']), mapping)})"
    return _raw(e)


def pin_words(pin: object) -> str:
    """A pinned file: its real path and FULL sha256 (a digest is never shortened in consent)."""
    if isinstance(pin, dict) and set(pin) <= {"path", "real", "sha256"}:
        return f"{pin.get('real')} (sha256 {pin.get('sha256')})"
    return _raw(pin)


def common_lines(t: dict, mapping: dict) -> list[str]:
    """The consent lines every run-local action shows (the mockup's "What you allow")."""
    who = t.get("run_as") if isinstance(t.get("run_as"), dict) else {"?": t.get("run_as")}
    env = t.get("env") if isinstance(t.get("env"), list) else [t.get("env")]
    outputs = t.get("outputs")
    minutes = t.get("timeout_minutes")
    out = [
        f"Runs in {t.get('folder')} (project {t.get('project_id')}) as your user "
        f"{who.get('user')} (uid {who.get('uid')})"
        + ("" if set(who) <= {"user", "uid"} else f" {_raw(who)}"),
        "Never through a shell: the program is started directly with exactly this argument list. "
        "A value from the delivery is one inert argument or variable by construction: never part "
        "of another string, never the program or the script, never right after an option",
        "What the program does with an argument is the program's own. BattleLab refuses the "
        "positions known to take code: an option's value, and anything before an interpreter's "
        "script or program text (python, node, perl, awk, sed, jq, …); it can't know every "
        "program",
    ]
    if env:
        out.append(
            "Environment: PATH, HOME, LANG, TERM=dumb, and "
            + ", ".join(_env_words(e, mapping) for e in env)
        )
    else:
        out.append("Environment: PATH, HOME, LANG and TERM=dumb only")
    out.append("Starts with a clean environment: nothing of BattleLab's own is passed on")
    out.append(
        f"Stops after {minutes} minute{'s' if minutes != 1 else ''}; keeps at most "
        f"{OUTPUT_CAP // 1024} KB of stdout and of stderr, with secrets redacted"
    )
    out.append(
        "Secrets never go in as values: a fixed argument or variable that looks like a credential "
        "or holds a stored secret is refused, and no stored secret is passed to it"
    )
    if isinstance(outputs, dict) and outputs:
        out.append(
            "Reports exit_code, and from a JSON object on the last line it prints: "
            + ", ".join(f"{k} ({_type_words(v)})" for k, v in outputs.items())
        )
    elif outputs:
        out.append(f"Reports exit_code and {_raw(outputs)}")
    else:
        out.append("Reports exit_code")
    out.append(
        "Exit 0 is ok, any other exit code is failed, the time limit is failed; "
        "a run cut off by a crash is never run again by itself"
    )
    out.append(
        "Runs only inside a separate process scope (systemd-run --user); without one it is "
        "refused. When it ends, everything it started is stopped"
    )
    out.append(
        "What it can reach: everything your user can — your files, your network, your "
        "credentials. The project folder is only where it starts and the scope only bounds how "
        "long it runs; neither is a filesystem or network sandbox"
    )
    return out
