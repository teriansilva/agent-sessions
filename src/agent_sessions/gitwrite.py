"""Operator-initiated git *writes* for the session file panel (#806).

A **second** git surface, deliberately separate from :mod:`agent_sessions.gitpanel`, and the
separation is the design rather than tidiness.

The read path is safe by *construction*: it assembles a sanitized gitdir holding only what reading
requires and never consults the repository's own config, so a repo-defined driver has nothing to
bind to. **That mechanism is unavailable here.** A fetch must write objects and refs into the real
store, a checkout must write the real index and worktree, a commit must move the real ``HEAD`` —
writing into a private copy and throwing it away is not the operation the user asked for. So these
commands run against the **real repository**, and its config is in play.

What this module therefore claims, stated plainly because the flattering version would be wrong:

    A git write executes code the repository configures — the same code the agent working in that
    worktree already executes every time it runs ``git`` itself. What is claimed is that (a) only
    the authenticated operator can trigger one, (b) the vectors that are *not* inherent to the
    operation are shut off, and (c) nothing here ever happens on a timer.

Measured on git 2.43.0, because three of the obvious hardening flags do not do what they look like:

=====================================  ==============================  ===========================
repo-config vector                     plain command                   what actually stops it
=====================================  ==============================  ===========================
``protocol.ext.allow=always`` + an      ``fetch`` runs ``<cmd>``        ``GIT_ALLOW_PROTOCOL`` — an
``ext::<cmd>`` remote                                                   **allowlist**. ``-c
                                                                        protocol.allow=never`` was
                                                                        measured INEFFECTIVE: the
                                                                        per-protocol key wins
``remote.<n>.uploadpack=<cmd>``         ``fetch`` runs ``<cmd>``        ``--upload-pack`` on the
                                        locally                         command line. ``-c`` was
                                                                        measured INEFFECTIVE
``core.hooksPath``                      ``switch`` runs the hook        ``-c core.hooksPath=<empty
                                                                        dir>`` (measured effective)
``filter.<drv>.smudge`` / ``.clean``    ``checkout`` / ``add`` run it   NOT mitigated, by decision —
                                                                        materialising the worktree
                                                                        and staging *are* the
                                                                        requested operations
=====================================  ==============================  ===========================

**Network transports only, and the destination is pinned.** ``file`` is not in
``GIT_ALLOW_PROTOCOL``, so a remote that is a path on this machine is refused by git itself before
any of this module's code has an opinion about it (operator decision, #806 — clone-to-clone fetch
and push on one box are terminal work now).

That alone was **not** enough, and the earlier claim here that it was is worth leaving corrected
rather than quietly rewritten: ``ssh://localhost/<path>`` is a network URL by every syntactic
test and reaches the local filesystem, which was reproduced against this module rather than
argued. So two further things hold. Every network operation **resolves the effective URL, refuses
it if it names this machine, and hands git that exact string** — resolving once and using what
was resolved is what makes the refusal sound, where inspecting ``remote.<n>.url`` and then running
``git fetch <name>`` would leave the config free to move in between. And the refspec is pinned
too, because ``remote.<n>.fetch`` is repo-controlled and ``+refs/heads/*:refs/heads/*`` would have
a fetch overwrite local branches.

What is still **not** closed: a repository can turn TLS verification off between the check and the
invocation (url-specific ``http.<url>.sslVerify`` beats the command line, measured). That needs
the network operation to stop reading the repository's config at all — tracked separately, not
fixed here.

The name-to-address race IS closed: a destination is pinned as an ADDRESS (``curloptResolve`` for
https, ``-o HostName`` for ssh) and a named host that resolves to nothing is refused rather than
admitted unpinned, so git never repeats the lookup that admission just made.

**A client-supplied name is not a pathspec, and ``--`` does not make it one.** ``--`` ends option
parsing; it does not disable pathspec magic. Measured: with ``a.txt`` and ``b.txt`` both modified,
``git restore --worktree -- ':(glob)*.txt'`` restored **both** (discard no longer shells out to
``restore``, but the rest take pathspecs the same way). So every command runs with
``--literal-pathspecs`` *and* ``--``, and — the part that actually bounds a destructive op — each
client path must appear in a **freshly re-read status**. A discard can only touch names the server
itself just reported.

No shell, ever. Literal argv lists only, exactly as on the read path.
"""

from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import os
import re
import select
import shutil
import signal
import socket
import stat as _stat
import subprocess
import tempfile
import threading
import time
from typing import NamedTuple
from urllib.parse import unquote

from .files import FsError, contained_path
from .fsbrowse import home_root
from .gitpanel import (
    GitError,
    Repo,
    bump_epoch,
    discover_repo,
    git_bin,
    git_branches,
    git_status,
    invalidate_status,
)

#: Network operations get a longer budget than the read path's 10s — a fetch talks to a remote.
#: Still bounded, and still killed *and reaped* on breach.
NET_TIMEOUT_S = 60.0
#: Everything local (switch, stage, commit, branch) — generous for a huge worktree, bounded anyway.
LOCAL_TIMEOUT_S = 30.0
_MAX_STDERR = 8192
#: Bound on captured stdout. `require_complete=True` turns hitting it into a refusal rather than a
#: silently shortened answer — see `run_git_write`.
_MAX_STDOUT = 1024 * 1024
_READ_CHUNK = 64 * 1024

#: Protocols an operator-initiated fetch/push may speak. An **allowlist**: a protocol nobody
#: thought to name is refused rather than inherited, which is the whole point (see the table).
#:
#: ``git`` is deliberately ABSENT. It is unauthenticated and unencrypted, essentially nobody uses
#: it for a real remote, and it is the only protocol ``core.gitProxy`` applies to — dropping it
#: removes that vector by construction rather than by pinning a key the repository could shadow.
#:
#: ``file`` is ABSENT for a stronger reason, and it is an operator decision recorded here because
#: it narrows what the panel can do. Local-path remotes were the source of every containment
#: question this module ever had to answer — ``file:///p`` vs ``file://localhost/p``, a bare
#: relative path, a percent-encoded ``..``, ``pushurl`` shadowing ``url`` — and underneath all of
#: them sat one unfixable shape: inspecting the repository's config and *then* invoking git is a
#: check-then-use race against a value the repository controls. Parsing harder never closes it.
#:
#: Dropping the protocol removes the *spelling*. MEASURED on git 2.43.0:
#: this allowlist refuses ``file://``, ``file://localhost/``, a bare absolute path and a bare
#: relative path alike (``fatal: transport 'file' not allowed``), and repo-local
#: ``protocol.file.allow=always`` does **not** override it — the environment wins.
#:
#: It does **not** remove the class, and an earlier version of this comment claimed it did —
#: "a network remote cannot name a path on this filesystem". That was false, and reproduced as
#: false: ``ssh`` is an allowed transport, ``ssh://localhost/<any readable path>`` is a network
#: URL by every syntactic test, and it fetched an outside repository. Loopback destinations are
#: therefore refused separately, on the resolved URL — see :func:`refuse_local_destination`.
#:
#: The cost, stated plainly: you cannot fetch or push between two clones on this machine *from
#: the panel*. That still works in the session's terminal, and the refusal says so.
GIT_ALLOW_PROTOCOL = "https:ssh"

#: Bound on how many paths one destructive request may name — a request, not a sweep.
MAX_PATHS = 500

#: A resolved push target (`origin/devopsagent/x`) — a remote and a branch, both name-shaped.
_TARGET_SHAPE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]{0,510}\Z")
#: A push expectation: the label the operator SAW, a digest of the destination it resolved to at
#: that moment, and the COMMIT that was going to be sent. `origin/master@1f2e…:9ab3…`.
#:
#: The source oid is the half that was missing. Binding only the destination answers "where does
#: this go", never "what goes there" — so an agent that advanced the branch after the preflight
#: had a still-valid token authorising a commit the operator was never shown. Measured: a probe
#: took a token, committed another file, and pushed it.
_EXPECT_SHAPE = re.compile(
    r"\A(?P<label>[A-Za-z0-9][A-Za-z0-9._/-]{0,510})@(?P<digest>[0-9a-f]{16})"
    r":(?P<oid>[0-9a-f]{40,64})\Z"
)

#: Refs are names, never options or expressions. `check-ref-format` is the authority; this is the
#: cheap filter in front of it that also refuses the shapes git would happily accept but the panel
#: has no business passing on (`@{...}`, a leading dash, anything non-printable).
_REF_SHAPE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._/-]{0,254}\Z")

_hooks_void_dir: str | None = None
_hooks_void_lock = threading.Lock()

#: One writer per repository *within this process*. Not a substitute for git's own `index.lock`
#: (another process — the agent — can hold that), but it stops the panel racing itself.
_repo_locks: dict[str, threading.Lock] = {}
_repo_locks_guard = threading.Lock()


def hooks_void() -> str:
    """An empty directory used as ``core.hooksPath`` so no repo-configured hook can run.

    A directory rather than ``/dev/null`` because git treats the value as a path to search; both
    were measured effective, and a real empty dir is the one that cannot surprise us on a git that
    starts stat-ing the target.
    """
    global _hooks_void_dir
    with _hooks_void_lock:
        if _hooks_void_dir is None or not os.path.isdir(_hooks_void_dir):
            _hooks_void_dir = tempfile.mkdtemp(prefix="agent-sessions-hooks-void-")
            os.chmod(_hooks_void_dir, 0o500)  # readable + searchable, never writable
        return _hooks_void_dir


def reset_hooks_void_for_test() -> None:
    global _hooks_void_dir
    with _hooks_void_lock:
        _hooks_void_dir = None


def _repo_lock(toplevel: str) -> threading.Lock:
    with _repo_locks_guard:
        lock = _repo_locks.get(toplevel)
        if lock is None:
            lock = threading.Lock()
            _repo_locks[toplevel] = lock
        return lock


def _write_env(root: str, gitdir: str = "", worktree: str = "") -> dict[str, str]:
    """An **allowlist** child environment — same rule as the read path, plus the write vectors.

    Built from nothing, so a git redirect variable has to be added here deliberately to have any
    effect. ``GIT_ALLOW_PROTOCOL`` is the measured-effective control for transport helpers;
    ``GIT_LITERAL_PATHSPECS`` belts the ``--literal-pathspecs`` flag on every subcommand including
    any this module grows later.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", root),
        "LANG": "C",
        "LC_ALL": "C",
        # Never prompt for credentials — a private remote must fail fast and readably rather than
        # hang a bounded worker until the timeout.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        # MEASURED: a repo-local `core.sshCommand` EXECUTES during fetch. `GIT_SSH_COMMAND` in the
        # environment overrides it (measured), and so does `-c` — both are set, because this one
        # is a plain command execution and one mechanism failing silently is not acceptable here.
        "GIT_SSH_COMMAND": "ssh",
        "GIT_PAGER": "cat",
        "GIT_CEILING_DIRECTORIES": root,
        "GIT_ALLOW_PROTOCOL": GIT_ALLOW_PROTOCOL,
        "GIT_LITERAL_PATHSPECS": "1",
        # Belt for the `--git-dir`/`--work-tree` braces above: anything git re-execs inherits the
        # pinned pair rather than re-reading `core.worktree`.
        "GIT_WORK_TREE": worktree,
        "GIT_DIR": gitdir,
        # An ssh remote still needs the operator's agent socket; the *repo* cannot influence it.
        **(
            {"SSH_AUTH_SOCK": os.environ["SSH_AUTH_SOCK"]}
            if os.environ.get("SSH_AUTH_SOCK")
            else {}
        ),
    }


def _base_argv(exe: str, repo: Repo, top: str) -> list[str]:
    """Options every write shares, in the order git requires.

    Main options must precede the subcommand, which is why this is a prefix rather than a suffix.
    """
    return [
        exe,
        # MEASURED: a repo-local `core.worktree` pointing outside `$HOME` makes `switch` write
        # THERE — the containment boundary the whole panel rests on, walked around by one config
        # key. Pinning both the gitdir and the worktree on the command line makes that key inert
        # (measured: `--work-tree` wins), and it is the structural fix rather than another entry
        # on a list of vectors to remember.
        f"--git-dir={repo.gitdir}",
        f"--work-tree={top}",
        # Measured: a repo-configured hook runs on a plain `switch`; an empty hooksPath stops it.
        "-c",
        f"core.hooksPath={hooks_void()}",
        # The read path's reasoning applies here too: an fsmonitor hook is repo-named code.
        "-c",
        "core.fsmonitor=false",
        # Every remaining repo-config key that names a PROGRAM. Each was measured to execute from
        # a repository's own config, and each is reset here rather than trusted:
        #   core.sshCommand   — runs on fetch/push over ssh (measured)
        #   credential.helper — an empty value RESETS the helper list (measured)
        #   core.gitProxy     — only applies to `git://`, which the protocol allowlist now omits
        #   gpg.program       — runs on commit when `commit.gpgSign` is on (measured)
        "-c",
        "core.sshCommand=ssh",
        "-c",
        "credential.helper=",
        "-c",
        "core.gitProxy=",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "gpg.program=false",
        # Defence in depth ONLY, and stated as such: a repository can shadow this with the
        # url-specific `http.<url>.sslVerify`, which is more specific and wins (measured). The
        # control that actually holds is `refuse_insecure_tls` below.
        "-c",
        "http.sslVerify=true",
        # `--` is not enough on its own (measured — see the module docstring).
        "--literal-pathspecs",
        "-C",
        top,
    ]


def tls_pin_for(url: str) -> list[str]:
    """``-c`` arguments that pin TRANSPORT for **this exact URL** — verification, and the proxy.

    MEASURED against a real self-signed endpoint, and the result overturns what this module used
    to claim. A repository's ``http.<url>.sslVerify=false`` beats a generic ``-c
    http.sslVerify=true`` — that part was right, and is why a generic pin was called useless. But
    at EQUAL specificity the command line wins: ``-c http.<that same url>.sslVerify=true`` made
    git refuse the self-signed certificate, where both weaker forms sailed through and the server
    logged the request.

    This is only reachable because the effective URL is now resolved and pinned (option C). Before
    that there was no "exact URL" to name, which is precisely why the earlier conclusion was that
    only a refusal could work. Nothing more specific than the full URL exists for a repository to
    outbid it with.

    **The proxy is pinned empty at the same specificity, and for the same reason.** Admitting the
    HOST says nothing about where the CONNECTION goes: with a repo-local ``http.proxy`` pointing
    at a loopback listener, git connected to that proxy even though the remote had been admitted
    as a public address — the local-service SSRF class restored behind a destination check that
    passed. Reproduced against the production `git_fetch()` path.

    Both spellings are reset, and the url-specific one is what actually holds: exactly as with
    ``sslVerify``, a repository's ``http.<url>.proxy`` outbids a generic ``-c http.proxy=``, so
    clearing only the generic key would look right and do nothing. The environment carries no
    proxy variables either — `_write_env` builds a fresh env rather than inheriting one — so a
    trusted operator proxy would have to be introduced deliberately as its own source rather than
    by whatever the repository happens to say.
    """
    if not url or not url.lower().startswith("https://"):
        return []
    return [
        "-c",
        f"http.{url}.sslVerify=true",
        "-c",
        "http.proxy=",
        "-c",
        f"http.{url}.proxy=",
        # A pin binds the URL that was CHECKED — it says nothing about where a 30x sends the next
        # hop. Measured: with the pins above in place, a redirect to a loopback HTTPS service was
        # followed and the connection accepted, so the original host's DNS pin constrained nothing
        # after the first response. Redirects are therefore switched off entirely rather than
        # re-admitted per hop: git's default is `initial`, which still follows one, and there is
        # no legitimate need for the panel's own fetch/push to change destination mid-flight.
        "-c",
        "http.followRedirects=false",
        "-c",
        f"http.{url}.followRedirects=false",
    ]


def run_git_write(
    repo: Repo,
    args: list[str],
    *,
    timeout: float = LOCAL_TIMEOUT_S,
    require_complete: bool = False,
    extra_config: list[str] | None = None,
    ssh_command: str | None = None,
    allow_protocol: str | None = None,
    stdin: bytes | None = None,
) -> str:
    """Run one write command with a literal argv list, bounded output, and a reaped child.

    Returns combined stdout for the caller to parse; raises :class:`GitError` with git's own
    (redacted) stderr on failure, because "why did my pull fail" is the whole value of the message.
    """
    return run_git_bytes(
        repo,
        args,
        timeout=timeout,
        require_complete=require_complete,
        extra_config=extra_config,
        ssh_command=ssh_command,
        allow_protocol=allow_protocol,
        stdin=stdin,
    ).decode("utf-8", "replace")


#: A stdin payload is a symlink target or a small blob, never a stream. Bounded well under the
#: pipe buffer so the single `write` below cannot deadlock against a child that is not reading.
MAX_STDIN = 32 * 1024


def run_git_bytes(
    repo: Repo,
    args: list[str],
    *,
    timeout: float = LOCAL_TIMEOUT_S,
    require_complete: bool = False,
    extra_config: list[str] | None = None,
    ssh_command: str | None = None,
    allow_protocol: str | None = None,
    stdin: bytes | None = None,
) -> bytes:
    """The same run, returning stdout as BYTES.

    `run_git_write` decodes with `errors="replace"`, which is right for messages and destroys
    content: a blob restored through it would come back with U+FFFD where any non-UTF-8 byte had
    been. Restoring a file means writing back exactly what git stored, so the byte-exact form is
    the primitive and the decoding one is the wrapper.
    """
    if stdin is not None and len(stdin) > MAX_STDIN:
        raise FsError("that content is too large for this operation", status=413)
    exe = git_bin()
    if not exe:
        raise GitError("git is not installed on this host", status=501)
    argv = [*_base_argv(exe, repo, repo.toplevel), *(extra_config or []), *args]
    env = {
        **_write_env(home_root(), repo.gitdir, repo.toplevel),
        # The pinned ssh command, when there is one. It goes in the ENVIRONMENT rather than as
        # `-c core.sshCommand`, because `_write_env` already measured that the env var wins —
        # a `-c` pin would be overridden by the plain "ssh" set there and silently do nothing.
        **({"GIT_SSH_COMMAND": ssh_command} if ssh_command else {}),
        # Narrowed to the scheme that was ADMITTED, so a `url.insteadOf` rewrite to any other
        # transport cannot connect even though it rewrote the pinned string.
        **({"GIT_ALLOW_PROTOCOL": allow_protocol} if allow_protocol else {}),
    }
    return _run_argv(
        argv, env, repo.toplevel, timeout, stdin=stdin, require_complete=require_complete
    )


def _run_argv(
    argv: list[str],
    env: dict[str, str],
    cwd: str,
    timeout: float,
    *,
    stdin: bytes | None = None,
    require_complete: bool = False,
) -> bytes:
    """Spawn one git, read it bounded, reap its whole process group, return stdout as bytes.

    Shared by the ordinary runner and the network one so there is a single place where a child is
    spawned, capped and reaped — two copies of this would drift, and the reaping is the part that
    stops a transport helper outliving the request.
    """
    proc = subprocess.Popen(  # noqa: S603 - literal argv, no shell, allowlisted subcommands
        argv,
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=cwd,
        # Its OWN process group, so a timeout can reap git's DESCENDANTS too. `proc.kill()` alone
        # signals git and nothing else: a transport helper git spawned (ssh, a credential helper)
        # outlives the route and keeps running after the operator has been told the operation was
        # stopped. Measured — a helper's child survived a plain kill.
        start_new_session=True,
    )
    if proc.stdin is not None:
        try:
            proc.stdin.write(stdin or b"")
        except (BrokenPipeError, OSError):
            pass  # the child died early; its exit status below is the real report
        finally:
            proc.stdin.close()
    out = bytearray()
    err = bytearray()
    truncated = False
    deadline = time.monotonic() + timeout
    streams = [p for p in (proc.stdout, proc.stderr) if p is not None]
    for p in streams:
        os.set_blocking(p.fileno(), False)
    try:
        while streams:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GitError("git took too long and was stopped", status=504)
            ready, _, _ = select.select(streams, [], [], min(remaining, 0.25))
            for p in ready:
                try:
                    chunk = os.read(p.fileno(), _READ_CHUNK)
                except BlockingIOError:
                    continue
                if not chunk:
                    streams.remove(p)
                    continue
                if p is proc.stdout:
                    if len(out) < _MAX_STDOUT:
                        out += chunk
                    else:
                        truncated = True
                elif len(err) < _MAX_STDERR:
                    err += chunk
        proc.wait(timeout=max(0.05, deadline - time.monotonic()))
    except GitError:
        _reap(proc)
        raise
    except subprocess.TimeoutExpired:
        _reap(proc)
        raise GitError("git took too long and was stopped", status=504) from None
    finally:
        for p in (proc.stdout, proc.stderr):
            if p is not None:
                p.close()
    if proc.returncode != 0:
        raise GitError(redact(bytes(err).decode("utf-8", "replace").strip()) or "git failed")
    if truncated and require_complete:
        # A SECURITY decision must never be taken on a partial read. `git config --list` past the
        # cap silently dropped a url-specific `sslVerify=false`, and the refusal that depends on
        # it returned "fine" — a fail-OPEN. Anything asking for a complete answer gets a refusal
        # instead of a shortened one.
        raise GitError(
            "this repository's configuration is too large for the panel to inspect safely",
            status=413,
        )
    return bytes(out)


def _reap(proc: subprocess.Popen) -> None:
    """Kill git **and everything it spawned**, then wait so nothing is left a zombie."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        proc.kill()  # the group is gone, or we never got one — fall back to the child itself
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:  # pragma: no cover - a SIGKILLed group does not linger
        pass


_CRED_IN_URL = re.compile(r"(?P<scheme>[a-zA-Z][\w+.-]*://)(?P<userinfo>[^/@\s]+)@")

#: Every ``name=value`` pair in a line of git output. Deliberately indiscriminate: which of them
#: is a secret is decided by :func:`_is_credential_key` on the DECODED name, not by this pattern.
#: Splitting the two is the fix for the encoded-key bypass — a single regex cannot both find
#: candidates and percent-decode them.
#: No length cap on the NAME. A cap there is a blind spot rather than a bound: a 65-character
#: key ending in `token` simply was not classified, and its value came back intact (measured).
#: The bound that matters already exists upstream — `_MAX_STDERR` caps the whole message at 8 KiB.
#:
#: The VALUE must not be allowed to swallow a following assignment. Excluding only `&` and
#: whitespace meant `?ok=1;client_secret=<secret>` parsed as ONE pair whose name was the benign
#: `ok`, so the secret was neither refused at admission nor redacted out of an error — both read
#: this matcher, so one blind spot produced both failures. `;` is a legacy query separator and
#: `?`/`#` start a nested URL's own query and the fragment, so all three end a value here.
#: Splitting too eagerly is the safe direction: it can only surface MORE candidate names for
#: `_is_credential_key` to judge, never fewer.
_QUERY_PAIR = re.compile(r"""(?<![\w%-])([A-Za-z0-9_%.~-]+)=([^&;#?\s"'<>]+)""")

#: A key name that names a secret. Substring match, so `client_secret`, `oauth_token` and
#: `x-api-key` are all caught without enumerating spellings.
_CRED_STEM = re.compile(
    r"(?i)secret|token|password|passwd|pwd|api[_-]?key|apikey|auth|credential|signature|sig"
)
#: Whole-name matches — OAuth's `code` and `state` are secrets under these names and nothing else.
_CRED_EXACT = {"code", "state"}


def _is_credential_key(name: str) -> bool:
    """Does this query-parameter name denote a secret, however it happens to be spelled?

    REPRODUCED: matching the literal name leaked `?to%6Ben=` while catching `?acce%73s_token=` —
    encoding a character *inside* the stem hid it, encoding one outside left the stem intact. So
    the name is percent-decoded before it is classified, and decoded **to a fixed point** (bounded)
    rather than once, because `%2574oken` is the same trick applied twice and costs nothing to
    close. Enumerating encoded spellings is not an option: there are unboundedly many.
    """
    seen = name
    # To a FIXED POINT, not a fixed number of rounds. Three rounds leaked at four (measured), and
    # any constant is a number an attacker just exceeds. This terminates without needing one:
    # every successful decode replaces a three-character `%XX` with one character, so the string
    # strictly shortens, and `len(name)` is therefore an upper bound that cannot be reached.
    for _ in range(len(name)):
        decoded = unquote(seen)
        if decoded == seen:
            break
        seen = decoded
    for candidate in (name, seen):
        if _CRED_STEM.search(candidate) or candidate.lower() in _CRED_EXACT:
            return True
    return False


#: A URL and everything from its `?` onward. The query is replaced as ONE unit — see `redact`.
_URL_QUERY = re.compile(r"""(?P<base>[A-Za-z][\w+.-]*://[^\s"'<>?]+)\?[^\s"'<>]*""")


def redact(text: str) -> str:
    """Strip credentials out of anything from git before it reaches a client or a log.

    Two forms, because the first alone was not enough (found in review): a remote URL can carry
    ``https://user:token@host/...`` **and** ``https://host/x.git?access_token=...``. Both are
    echoed by git.

    **The whole query component goes, not the keys this module recognises.** Deciding which names
    are secret lost four review rounds — `?access_token=`, a benign pair swallowing a later one
    across `;`, then `oauth[client_secret]=` and percent-encoded nesting — because a classifier
    competing with every way a secret can be written has no last move. A query string in a git
    URL is not information the operator needs in an error message, so it is replaced wholesale
    and the game ends. Values are replaced, never truncated: a prefix of a token is still a
    disclosure.

    Query pairs OUTSIDE a URL keep the old key-by-key treatment, because there the name genuinely
    is the useful part of the message.
    """
    out = _CRED_IN_URL.sub(lambda m: f"{m.group('scheme')}<redacted>@", text)
    out = _URL_QUERY.sub(lambda m: f"{m.group('base')}?<redacted>", out)
    return _QUERY_PAIR.sub(
        lambda m: f"{m.group(1)}=<redacted>" if _is_credential_key(m.group(1)) else m.group(0),
        out,
    )


# --------------------------------------------------------------------------- input validation


def validate_ref(name: object, *, what: str = "branch") -> str:
    """A branch/remote name, proven to be a *name*. Raises rather than returning a default."""
    if not isinstance(name, str) or not name.strip():
        raise FsError(f"a {what} name is required", status=422)
    name = name.strip()
    if not _REF_SHAPE.match(name) or ".." in name or name.endswith(".lock"):
        raise FsError(f"that is not a valid {what} name", status=422)
    return name


def check_ref_format(repo: Repo, branch: str) -> None:
    """git's own authority on the name, after the cheap shape filter above.

    No ``--`` here: ``check-ref-format`` has no option separator and would take it as the refname
    itself (measured — every call failed). It is safe to omit precisely because
    :func:`validate_ref` has already run, and it refuses anything that does not *start* with an
    alphanumeric — so a name that could be read as an option never reaches this line.
    """
    try:
        run_git_write(repo, ["check-ref-format", f"refs/heads/{branch}"])
    except GitError:
        raise FsError("that is not a valid branch name", status=422) from None


def validate_paths(repo: Repo, raw: object, *, staged: bool | None = None) -> list[str]:
    """Bound a destructive request to paths the server itself just reported.

    This is the control that makes ``discard`` safe, not ``--``. Every name must appear in a
    **freshly re-read** status: a pathspec the tree does not currently show as changed cannot be
    reached, so neither magic nor a stale UI can widen the blast radius past what was confirmed.
    """
    if not isinstance(raw, list) or not raw:
        raise FsError("at least one path is required", status=422)
    if len(raw) > MAX_PATHS:
        raise FsError(f"too many paths in one request (max {MAX_PATHS})", status=422)
    wanted: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item or "\x00" in item:
            raise FsError("a path must be a non-empty string", status=422)
        if item.startswith("/") or item.startswith("-"):
            raise FsError("a path must be relative to the repository", status=422)
        parts = item.split("/")
        if any(p in ("", ".", "..") for p in parts):
            raise FsError("a path must not contain traversal segments", status=422)
        wanted.append(item)

    fresh = git_status(repo.toplevel)
    known: set[str] = set()
    for entry in fresh.get("entries", []):
        if staged is None or (entry.get("kind") == "staged") == staged:
            known.add(entry["path"])
            if entry.get("orig_path"):
                known.add(entry["orig_path"])
    missing = [p for p in wanted if p not in known]
    if missing:
        raise FsError(
            f"the repository no longer reports a change for {missing[0]!r} — refresh and try again",
            status=409,
        )
    # De-duplicated but order-preserving: git is invoked once with the whole set.
    seen: set[str] = set()
    return [p for p in wanted if not (p in seen or seen.add(p))]


# --------------------------------------------------------------------------- repo resolution


def resolve_repo(path: str | None) -> Repo:
    """The contained repository for ``path``, or a stated refusal. Never a partial answer."""
    base = contained_path(path or "")
    repo = discover_repo(base)
    if repo is None:
        raise FsError("this folder is not inside a git working tree", status=404)
    return repo


def _head_branch(repo: Repo) -> str | None:
    """The current branch, or ``None`` when HEAD is detached — a state, never an exception.

    ``symbolic-ref`` *exits non-zero* on a detached HEAD even with ``--quiet``, so the obvious
    version turns "you are not on a branch" into a generic ``git failed`` 400 and every caller
    that wanted to answer 409 with the real reason loses it.
    """
    try:
        out = run_git_write(repo, ["symbolic-ref", "--quiet", "--short", "HEAD"]).strip()
    except GitError:
        return None
    return out or None


def _is_clean(repo: Repo) -> tuple[bool, int]:
    """Cheap, cached — fine for the EARLY refusal that just saves a round trip."""
    status = git_status(repo.toplevel)
    n = len(status.get("entries", []))
    return n == 0, n


def _is_clean_now(repo: Repo) -> tuple[bool, int]:
    """The same question, asked past the cache — for the re-check that has to BIND.

    `_is_clean` reads the cached status, which is by definition the state before the thing being
    guarded against, so using it inside the lock re-asks a question that was already answered and
    always agrees. Measured: pull fast-forwarded over a file dirtied as the lock was taken,
    because the cached read still said clean. (`switch` only escaped this by calling
    `verify_scalar` first, which bumps the epoch as a side effect — luck, not design.)
    """
    status = _fresh_status(repo)
    n = len(status.get("entries", []))
    return n == 0, n


def _fresh_status(repo: Repo) -> dict:
    """Status **produced after this call started** — the shape a write's response must report.

    Two distinct bugs live here, and only the second one is obvious:

    * ``_guarded`` invalidates in its ``finally``, which is right for the failure path but runs
      strictly *after* the operation built its payload — so a write reported the reality it had
      already replaced (a freshly staged file came back as ``changed``).
    * Dropping the cache is still not enough. A status read that was ALREADY IN FLIGHT when the
      write landed would be rejoined and its pre-write value returned as the write's own result —
      measured: a fetch answered ``behind: 0`` while a read immediately after said ``behind: 1``.

    So this bumps the repository's epoch and demands a value from at least that epoch, which is a
    thing a flight begun earlier cannot satisfy.
    """
    epoch = bump_epoch(repo.toplevel)
    return git_status(repo.toplevel, min_epoch=epoch)


def _has_unmerged(repo: Repo) -> bool:
    return any(e.get("kind") == "unmerged" for e in git_status(repo.toplevel).get("entries", []))


def require_bool(value: object, name: str, default: bool | None = None) -> bool:
    """A JSON boolean, or a refusal — never ``bool(value)``.

    ``bool("false")`` is **True**, so a client sending the string ``"false"`` for ``create`` got a
    new branch instead of a switch, and for ``staged`` got a stage instead of an unstage. Found in
    review and reproduced: a truthiness coercion can *invert* the requested operation, which is
    the one thing a write API must never do quietly.
    """
    if value is None and default is not None:
        return default
    if not isinstance(value, bool):
        raise FsError(f"{name} must be true or false", status=422)
    return value


#: `scheme://[user[:pass]@]host[:port]/path`.
_URL_AUTHORITY = re.compile(r"\A[A-Za-z][\w+.-]*://(?:[^/@]*@)?(?P<host>\[[^\]]*\]|[^/:?#]*)")
#: `[user@]host:path` — scp-like. git's own rule: the syntax is recognised only when there is no
#: slash before the first colon, which `[^/@]+@` and `[^/:]+` enforce between them — so a local
#: path that merely contains a colon (`/tmp/a:b/repo`) cannot match, while `git@host:/srv/x.git`
#: does. An earlier version had a `(?!/)` here to try to express that, which was wrong twice
#: over: it rejected the very common absolute-path form, and it let the IPv6 alternative backtrack
#: into matching a bare `[` as the host.
_SCP_LIKE = re.compile(r"\A(?:[^/@]+@)?(?P<host>\[[^\]]*\]|[^/:]+):")


def url_host(url: str) -> str | None:
    """The host a remote URL names, or ``None`` when it names no host.

    ``None`` means "this is a path, not a network destination" — a bare or ``file://`` path. Those
    are the protocol allowlist's business, not this function's; it would be a bug to treat "no
    host" as "safe host".
    """
    m = _URL_AUTHORITY.match(url)
    if m:
        host = m.group("host")
        return host or None
    m = _SCP_LIKE.match(url)
    if m:
        return m.group("host")
    return None


def _addr_is_bindable(addr: str) -> bool:
    """Can this process ``bind()`` to that address — i.e. is it assigned to a local interface?

    Loopback is not the only way to say "this machine", which is the finding this answers: the
    host's own LAN address reaches the same filesystem just as surely as ``127.0.0.1``, and a
    loopback-only predicate admits it.

    ``bind()`` is the test rather than an interface inventory because it asks the kernel the exact
    question that matters and needs no dependency: binding a UDP socket to an address that is not
    assigned to this host fails with ``EADDRNOTAVAIL``. Nothing is sent, no port is held (port 0,
    closed immediately), and it is correct inside a network namespace, where an inventory scraped
    from elsewhere would not be.
    """
    for fam in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.inet_pton(fam, addr)
        except OSError:
            continue
        try:
            with socket.socket(fam, socket.SOCK_DGRAM) as sock:
                sock.bind((addr, 0))
                return True
        except OSError:
            return False
    return False


def _addr_is_local(addr: str) -> bool:
    """Is this a literal address that means *this machine*?

    Three ways an address can mean "here", and all three were found by measurement rather than
    reasoning:

    * loopback, the whole ``127.0.0.0/8`` and ``::1`` — the obvious one;
    * the unspecified addresses, ``0.0.0.0`` and ``::``;
    * IPv4-mapped forms — ``ipaddress.IPv6Address("::ffff:127.0.0.1").is_loopback`` is **False**,
      so the mapped address has to be unwrapped explicitly or it walks straight through;
    * and **any address assigned to a local interface**, which the first three miss entirely: the
      host's own LAN address is none of the above and reaches this same machine.
    """
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.is_loopback or ip.is_unspecified:
        return True
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped and (mapped.is_loopback or mapped.is_unspecified):
        return True
    return _addr_is_bindable(str(ip))


def host_is_local(host: str, addrs: list[str] | None = None) -> bool:
    """Does this host name or address reach *this machine*?

    Three layers, because a literal check alone is trivially sidestepped by a name:

    1. RFC 6761 reserves ``localhost`` and everything under ``.localhost`` for loopback. No
       lookup needed, and no lookup wanted — the RFC is the guarantee.
    2. The literal forms, via :func:`_addr_is_local`.
    3. Anything else is RESOLVED, and every address it resolves to is checked. A name pointing at
       ``127.0.0.1`` is the obvious way past a literal-only check.

    **The rebinding window this used to leave is closed.** Step 3 is a lookup, and DNS can answer
    differently when git resolves the same name a moment later — so the answer is not to look it
    up again: `admit_destination` pins the destination as an ADDRESS, and refuses a named host
    that resolves to nothing rather than admitting it unpinned. git therefore never repeats the
    lookup this function just made.
    """
    name = host.strip().strip("[]").rstrip(".").lower()
    if not name:
        return False
    if name == "localhost" or name.endswith(".localhost"):
        return True
    if _addr_is_local(name):
        return True
    # The caller may already have resolved this; reuse its answer so the addresses that are
    # CHECKED are the same ones that get pinned.
    resolved = resolve_addresses(name) if addrs is None else addrs
    return any(_addr_is_local(a) for a in resolved)


#: `scheme://[user@]host[:port]` — the port, when the URL states one.
_URL_PORT = re.compile(
    r"\A[A-Za-z][\w+.-]*://(?:[^/@]*@)?(?:\[[^\]]*\]|[^/:?#]*):(?P<port>\d{1,5})"
)


def resolve_addresses(host: str) -> list[str]:
    """Every address this host resolves to, or ``[]`` for a literal / unresolvable name."""
    try:
        return sorted({str(info[4][0]) for info in socket.getaddrinfo(host, None)})
    except (socket.gaierror, UnicodeError, OSError):
        return []


#: A hostname we are willing to hand to `ssh` and to interpolate into `GIT_SSH_COMMAND`.
#: Deliberately a strict allowlist rather than an escape: no shell metacharacter, no whitespace,
#: no quote and no leading `-` can survive it, so the interpolation below cannot become an
#: injection or an option. Anything outside it is refused rather than quoted — a remote whose
#: hostname is not a hostname has nothing legitimate to do here.
_SSH_HOST_OK = re.compile(r"\A[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?\.?\Z")


def _ssh_safe_host(host: str) -> str:
    """The host, or a refusal. See :data:`_SSH_HOST_OK` for why this is an allowlist."""
    bare = host.strip("[]")
    if _addr_is_literal(bare):
        return bare
    if not _SSH_HOST_OK.match(bare):
        raise FsError(
            f"remote host {host!r} is not a plain hostname, so the panel will not connect to it. "
            "Do it in this session's terminal instead.",
            status=403,
        )
    return bare


def ssh_effective_host(host: str) -> str:
    """What ``ssh`` will ACTUALLY connect to for ``host``, per the operator's own config.

    `ssh -G` prints the fully resolved configuration and **connects to nothing**, so this is the
    cheap way to honour a perfectly ordinary ``~/.ssh/config`` ``Host`` block that rewrites
    ``Hostname`` (``github.com`` → ``ssh.github.com``). Resolving the *effective* name is what
    makes pinning safe: pinning the address of the name written in the remote URL would send the
    connection to a host resolved for the wrong name whenever such a block exists.

    An earlier revision of this module argued from that breakage that ssh could not be pinned at
    all. That was wrong, and wrong in a way worth naming: it reasoned from an untested premise
    instead of running `ssh -G`, which answers it in milliseconds.
    """
    safe = _ssh_safe_host(host)
    exe = shutil.which("ssh")
    if not exe:
        raise FsError("ssh is not installed on this host", status=501)
    try:
        out = subprocess.run(  # noqa: S603 - literal argv, no shell, host is allowlist-validated
            [exe, "-G", "--", safe],
            capture_output=True,
            text=True,
            timeout=LOCAL_TIMEOUT_S,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": os.path.expanduser("~")},
        )
    except (OSError, subprocess.SubprocessError):
        return safe
    if out.returncode != 0:
        return safe
    for line in out.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key.lower() == "hostname" and value.strip():
            return _ssh_safe_host(value.strip())
    return safe


class Admission(NamedTuple):
    """What a destination check yields: the ``-c`` pins, and the ssh command to run under.

    Two fields rather than two functions, for the same reason `admit_destination` is one function:
    whatever is checked has to be what is used, and a caller that can take one without the other
    is a caller that can rebuild the check-then-use shape by forgetting a line.
    """

    pins: list[str]
    ssh_command: str | None = None
    #: The ONLY transport this invocation may use — the scheme that was actually admitted.
    #:
    #: `url.<base>.insteadOf` rewrites a URL inside git, and it applies to a URL given on the
    #: COMMAND LINE too, so pinning the string is not enough: a rewrite added after admission
    #: turns the pinned `https://` into `ssh://127.0.0.1/...` and every https pin stays attached
    #: to a URL git no longer uses. Narrowing `GIT_ALLOW_PROTOCOL` to the admitted scheme makes
    #: that rewrite INERT — git refuses the transport itself — regardless of when it was added,
    #: which a config check performed at admission time cannot do.
    allow_protocol: str | None = None


#: `scheme://userinfo@host` — the userinfo half, which is where an inline credential rides.
_URL_USERINFO = re.compile(r"\A[A-Za-z][\w+.-]*://(?P<userinfo>[^/@]+)@")


def refuse_credential_in_url(url: str, name: str) -> None:
    """Refuse a remote whose URL carries inline credentials.

    `redact()` protects the stderr this module RETURNS; it cannot protect the command line. The
    resolved URL is placed directly in the fetch/pull/push argv and embedded in `-c` arguments, so
    an `https://user:token@host/x.git` remote publishes that token in `/proc/<pid>/cmdline` for
    the lifetime of the operation — readable by any other local user on this shared host.

    Refused rather than rewritten: stripping the userinfo would silently change WHICH credential
    git uses (falling back to a helper, or to none) and turn a visible failure into a confusing
    one. Any userinfo counts, because a bare `https://<token>@host` carries the secret in the
    username position just as surely as the password position does.
    """

    def _refuse(where: str) -> None:
        raise FsError(
            f"remote {name!r} has a credential in its URL {where}, which would be visible to "
            "other users on this machine while git runs. Use a credential helper instead.",
            status=403,
        )

    m = _URL_USERINFO.match(url)
    if m:
        userinfo = m.group("userinfo")
        # `ssh://git@host` is how EVERY git host spells an ssh remote — the username is a login
        # name, not a secret, and refusing it outright would have broken essentially every real
        # ssh remote this panel exists to drive. (It did: caught by probing an ordinary remote
        # rather than by a test, because the credential tests all used https.)
        #
        # A PASSWORD is another matter, on any transport. And over https a username is either
        # useless or a token wearing one — `https://<token>@host` is the documented form for
        # several hosts — so there the whole userinfo goes.
        if ":" in userinfo:
            _refuse("userinfo (a password)")
        if not _is_ssh_url(url):
            _refuse("userinfo")
    # And NO query or fragment at all — not "no credential-looking key".
    #
    # Classifying key names lost four review rounds running: userinfo, then `?access_token=`,
    # then a benign pair swallowing a later one across `;`, then `oauth[client_secret]=` and
    # percent-encoded nesting. Each fix was right and each time a new spelling appeared, because
    # a lexer competing with every way a secret can be written is a game with no last move.
    #
    # A git remote does not need a query string. Refusing the whole component ends the class
    # instead of narrowing it: there is nothing left to spell around, and the rule is one
    # character to check rather than a table of families to keep in step with `redact()`.
    for ch, where in (("?", "query string"), ("#", "fragment")):
        if ch in url:
            _refuse(f"{where} — the panel does not use one, and it can hide a credential")


#: The transports this panel will drive, as a SET rather than a string to compare against.
APPROVED_PROTOCOLS = frozenset(GIT_ALLOW_PROTOCOL.split(":"))


def _approved_scheme(url: str, name: str) -> str:
    """The URL's scheme — but only if it is one this module already allows.

    Narrowing `GIT_ALLOW_PROTOCOL` to the admitted scheme is what makes a `url.insteadOf` rewrite
    inert. Deriving that value from the URL alone inverted it: an `http://` or `git://` remote
    set the variable to its own scheme and thereby ENABLED a plaintext transport the module's
    allowlist exists to forbid — a pin that widened the very boundary it was added to hold.
    The set is the authority; the URL only ever selects from it.
    """
    # `git@host:path` carries no scheme and IS ssh — the form every git host documents. Reading
    # the scheme literally refused it, which would have broken the most common ssh remote there
    # is (caught by the test that exists for exactly that mistake, made once before).
    scheme = url.split("://", 1)[0].lower() if "://" in url else ("ssh" if _is_ssh_url(url) else "")
    if scheme not in APPROVED_PROTOCOLS:
        raise FsError(
            f"remote {name!r} uses {scheme or 'an unknown'!s} transport, which this panel does "
            f"not drive (allowed: {', '.join(sorted(APPROVED_PROTOCOLS))}). Do it in this "
            "session's terminal instead.",
            status=403,
        )
    return scheme


def refuse_url_rewrites(repo: Repo, url: str) -> None:
    """Refuse when repo config could REWRITE the URL that was just admitted.

    `url.<base>.insteadOf` (and `pushInsteadOf`) rewrite a URL *inside git*, after every check
    here has passed. A repository can therefore point the admitted `https://good/` at
    `ssh://127.0.0.1/outside/` and git will make that connection instead — leaving the whole
    HTTPS pin set (`curloptResolve`, url-specific `sslVerify`, the proxy reset) attached to a URL
    git no longer uses, and the local-destination refusal bypassed by a transport it never saw.

    This REFUSES rather than clearing the keys. Clearing looks tempting — `-c url.<b>.insteadOf=`
    — but an empty prefix matches every URL, so the "fix" would rewrite everything through that
    base. A rewrite that is about to be applied to this destination is a repository asking for
    something the panel has no way to re-admit, so it is answered with a refusal and a name.
    """
    try:
        blob = run_git_write(
            repo, ["config", "--get-regexp", r"^url\..*\.(insteadof|pushinsteadof)$"]
        )
    except (GitError, FsError):
        return  # no matching keys: `--get-regexp` exits non-zero when nothing matches
    for line in blob.splitlines():
        key, _, prefix = line.strip().partition(" ")
        if not key:
            continue
        # ANY rewrite key refuses, not just one whose prefix matches this URL today. Matching on
        # the prefix was defeated two ways: a same-scheme rewrite pointing at a different host
        # (the protocol pin does not see it), and a key added after the check. A repository that
        # rewrites URLs at all is one where the panel cannot say where a connection will land,
        # and that is the honest answer rather than a test the next rewrite walks around.
        raise FsError(
            f"this repository rewrites remote URLs ({key}), so the panel cannot tell where the "
            "connection would actually go. Do it in this session's terminal instead.",
            status=403,
        )


def admit_destination(url: str, name: str) -> Admission:
    """Refuse a destination that means this machine, and return the ``-c`` pins that keep it put.

    One function rather than a check and a separate pin, because the two have to agree: the
    addresses this refuses on are the addresses it pins, so there is no second resolution for
    anything to differ on. Splitting them is how the check-then-use shape gets rebuilt by
    accident.

    Returns the config to hand :func:`run_git_write`:

    * ``http.<url>.sslVerify=true`` — at the URL's own specificity, which is the only form that
      beats a repository's url-specific override (measured against a real self-signed endpoint;
      the generic key loses).
    * ``http.curloptResolve=<host>:<port>:<addrs>`` — git then skips DNS entirely and connects to
      the addresses that were just checked. MEASURED: with it the error becomes "Failed to connect
      to … port 443", without it "Could not resolve host", so the pin demonstrably takes effect.
      All resolved addresses are listed, so DNS failover still works.

    **ssh is pinned too**, via ``-o HostName=<addr> -o HostKeyAlias=<host>`` carried in
    ``GIT_SSH_COMMAND``. The env var is the mechanism and not ``-c core.sshCommand``, because this
    module already measured that the environment WINS over the config key — a `-c` pin here would
    be silently ignored by the value `_write_env` sets.

    Three things make that string safe to build by interpolation, in the module whose shell-free
    property is load-bearing:

    * the address is whatever ``getaddrinfo`` returned, so it is a numeric literal;
    * the host is held to :data:`_SSH_HOST_OK`, which admits no metacharacter, space, quote or
      leading ``-``, and REFUSES rather than quoting anything else;
    * the name pinned is the one :func:`ssh_effective_host` resolved, so an ordinary
      ``~/.ssh/config`` rewrite is honoured instead of overridden.

    ``HostKeyAlias`` keeps host-key checking against the NAME, so the operator's existing
    ``known_hosts`` entry still matches once the connection is addressed numerically — without it,
    pinning would turn every ssh remote into an unknown-host prompt.
    """
    refuse_credential_in_url(url, name)
    host = url_host(url)
    if host is None:
        return Admission([])  # a path, not a network destination — the allowlist owns it
    bare = host.strip("[]")

    def _refuse(shown: str) -> None:
        raise FsError(
            f"remote {name!r} points back at this machine ({shown}), which would let the panel "
            "reach files outside your home directory. Do it in this session's terminal instead.",
            status=403,
        )

    def _unresolved(shown: str) -> None:
        # FAIL CLOSED. `resolve_addresses` reports every lookup failure as an empty list, and an
        # empty list used to mean "admitted, but unpinned" — so a DNS-controlled host could answer
        # NXDOMAIN here and a LOCAL address when git looks the same name up a moment later. That
        # hands back the whole escape this function exists to prevent, through the one path that
        # skipped the pin. A named destination that resolves to nothing is refused instead.
        raise FsError(
            f"remote {name!r} ({shown}) could not be resolved, so the panel cannot check where it "
            "points. Do it in this session's terminal instead.",
            status=403,
        )

    if _is_ssh_url(url):
        # The name ssh will ACTUALLY use, resolved ONCE — the operator's config may rewrite it,
        # and an address resolved for the URL's name would then reach a different host entirely.
        # Deliberately not resolved twice: refusing on one answer and pinning another is exactly
        # the check-then-use shape this function exists to make unspellable.
        effective = ssh_effective_host(bare)
        addrs = [effective] if _addr_is_literal(effective) else resolve_addresses(effective)
        if host_is_local(effective, addrs):
            _refuse(effective)
        if not addrs:
            _unresolved(effective)
        # One address, because ssh takes one. Failover is lost for this operation, which is the
        # honest cost of connecting only to something that was actually checked.
        return Admission(
            [],
            f"ssh -o HostName={addrs[0]} -o HostKeyAlias={effective}",
            allow_protocol=_approved_scheme(url, name),
        )

    literal = _addr_is_literal(bare)
    addrs = resolve_addresses(bare)
    if host_is_local(host, addrs):
        _refuse(host)
    if not literal and not addrs:
        # Same fail-closed rule for https: without addresses there is no `curloptResolve` pin, so
        # git would resolve the name itself and the rebinding window reopens as local-service SSRF.
        _unresolved(host)
    pins = tls_pin_for(url)
    if pins and addrs and not literal:
        m = _URL_PORT.match(url)
        port = m.group("port") if m else "443"
        pins += ["-c", f"http.curloptResolve={bare}:{port}:{','.join(addrs)}"]
    return Admission(pins, allow_protocol=_approved_scheme(url, name))


def _is_ssh_url(url: str) -> bool:
    """An ssh destination — either an explicit scheme or the scp-like `user@host:path` form.

    An explicit scheme WINS, the same precedence `url_host` uses. Testing the scp-like pattern
    first classifies `https://h/x` as ssh, because `https` reads as the host and `//h/x` as the
    path — which silently dropped the TLS and DNS pins from every https remote. Caught by the
    https pin tests going empty, which is what they are for.
    """
    if "://" in url:
        return url.split("://", 1)[0].lower() in ("ssh", "git+ssh")
    return bool(_SCP_LIKE.match(url))


def _addr_is_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def refuse_local_destination(url: str, name: str) -> None:
    """Refuse a remote that points back at this machine.

    The premise the ``file:``-removal rested on — "a network remote cannot reach the local
    filesystem" — is FALSE for ``ssh``, and it was reproduced rather than argued:
    ``ssh://localhost/<path outside the root>`` fetched, and created the outside remote-tracking
    ref. ssh to loopback is a network URL by every syntactic test and a local read by effect.

    This check is only sound because of where it sits: the caller has already RESOLVED the
    effective URL and passes that exact string to git on the command line. Inspecting
    ``remote.<n>.url`` and then invoking ``git fetch <name>`` would be the same check-then-use
    race the local-path containment had — the config could move in between. Checking and using
    one value is the whole point.
    """
    host = url_host(url)
    if host is None or not host_is_local(host):
        return
    raise FsError(
        f"remote {name!r} points back at this machine ({host}), which would let the panel reach "
        "files outside your home directory. Do it in this session's terminal instead.",
        status=403,
    )


def _effective_url(repo: Repo, name: str, *, push: bool) -> str:
    """The one URL git would actually use — resolved here so it can be pinned on the argv.

    ``get-url`` applies ``insteadOf`` rewriting, so what comes back is the rewritten destination
    rather than the alias, which is what has to be both checked and used.

    A remote with SEVERAL effective push URLs is refused rather than narrowed: git would push to
    every one of them, and pinning the first would silently drop the rest — a push that reports
    success while half of it never happened.
    """
    args = ["remote", "get-url"]
    if push:
        args += ["--push", "--all"]
    args += ["--", name]
    urls = [u.strip() for u in run_git_write(repo, args).splitlines() if u.strip()]
    if not urls:
        raise FsError(f"remote {name!r} has no URL configured", status=409)
    if push and len(urls) > 1:
        raise FsError(
            f"remote {name!r} pushes to {len(urls)} URLs. The panel pushes to one destination it "
            "can name, so this one belongs in the session's terminal.",
            status=409,
        )
    return urls[0]


#: Any config key whose name ends in `sslverify`, so the url-specific form is caught too.
_SSLVERIFY = re.compile(r"(?im)^\s*(\S*sslverify)\s*=?\s*(\S*)\s*$")


def refuse_insecure_tls(repo: Repo) -> None:
    """Refuse a repository that turns TLS verification off, rather than pretending to pin it on.

    ``-c http.sslVerify=true`` is set (see :func:`_base_argv`) but is **not** the control, and
    saying otherwise would be the flattering version: measured, a repository's url-specific
    ``http.<url>.sslVerify=false`` is MORE SPECIFIC than the generic key and wins over the command
    line — the same shadowing shape that makes ``-c remote.<n>.uploadpack`` ineffective. There is
    no environment variable that force-*enables* verification. So the honest control is a refusal.
    """
    try:
        out = run_git_write(repo, ["config", "--list"], require_complete=True)
    except GitError as e:
        # A refusal to read the config is itself a refusal to proceed: "I could not check" must
        # not be spelled the same as "I checked and it was fine".
        if getattr(e, "status", None) == 413:
            raise
        return
    for key, value in _SSLVERIFY.findall(out):
        if value.strip().lower() in ("false", "no", "off", "0"):
            raise FsError(
                f"this repository disables TLS certificate verification ({key}), so the panel "
                "will not talk to its remotes. Remove that setting, or fetch in the session.",
                status=403,
            )


def list_remotes(repo: Repo) -> list[str]:
    out = run_git_write(repo, ["remote"])
    return [line.strip() for line in out.splitlines() if line.strip()]


def _upstream(repo: Repo, branch: str) -> str | None:
    try:
        out = run_git_write(
            repo, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", f"{branch}@{{upstream}}"]
        ).strip()
    except GitError:
        return None
    return out or None


# --------------------------------------------------------------------------- operations


def _busy(exc: GitError) -> GitError:
    """`index.lock` is the agent working in the same repo — a state, not a fault."""
    text = str(exc)
    if "index.lock" in text or "Unable to create" in text:
        return GitError(
            "the repository is busy — the agent in this session is running git right now. "
            "Nothing was changed.",
            status=423,
        )
    return exc


#: git's own words when the protocol allowlist turns a transport down:
#: ``fatal: transport 'file' not allowed`` (measured, git 2.43.0).
_TRANSPORT_REFUSED = re.compile(r"transport '([^']+)' not allowed", re.IGNORECASE)


def _refused_transport(exc: GitError) -> GitError:
    """Say what the allowlist did, in the operator's terms, and name the way to do it anyway.

    The refusal itself is structural — it comes from ``GIT_ALLOW_PROTOCOL`` in the child env, not
    from anything this module inspected, so it cannot be raced or spelled around. But git's own
    message is addressed to someone who chose the flag, and nobody here did. This translates it
    into the one thing the operator needs to know: the panel is network-only, and their terminal
    is not.

    Deliberately a translation of the FAILURE rather than a pre-flight check of the remote URL.
    Inspecting config to produce a nicer message would re-introduce, in miniature, exactly the
    check-then-use shape the operator decision removed.
    """
    m = _TRANSPORT_REFUSED.search(str(exc))
    if m is None:
        return exc
    return GitError(
        f"the panel speaks https and ssh only, and this remote uses {m.group(1)!r}. A remote "
        "that is a folder on this machine is not reachable from here — do it in this session's "
        "terminal instead.",
        status=403,
    )


def _guarded(repo: Repo, fn):
    """Serialize this process's writes per repository, and translate a lock collision honestly."""
    lock = _repo_lock(repo.toplevel)
    if not lock.acquire(timeout=15):
        raise GitError("another operation is already running on this repository", status=423)
    try:
        return fn()
    except GitError as e:
        raise _busy(_refused_transport(e)) from None
    finally:
        lock.release()
        invalidate_status(repo.toplevel)


# ======================================================================================
# Network writes run against a config this module wrote, not the repository's
#
# `refuse_url_rewrites` scanned the repository's config for `url.<base>.insteadOf` before handing
# git a pinned URL. That is a preflight, and a preflight loses to a writer: a rewrite installed
# after the scan still applies when git finally runs, and `GIT_ALLOW_PROTOCOL=https` does not stop
# an HTTPS→HTTPS redirect. A reviewer's real-git probe added the rewrite in that window and the
# "pinned" request reached a local endpoint with the `curloptResolve` pin still attached to the
# original host. Every parsing fix for this class had the same shape and the same hole underneath.
#
# The fix is to stop reading that config at all. Git takes its repository configuration from
# `$GIT_DIR/config` and there is no switch to relocate it — so the network command is given a
# DIFFERENT `$GIT_DIR`: a scratch directory holding a config this module wrote, with no `url.*`,
# no `http.*`, no remotes, nothing the repository can reach. `GIT_OBJECT_DIRECTORY` still points
# at the real object store, so a fetch writes its objects exactly where they belong and a push can
# read the commit it is sending. Only the refs land in the scratch directory, and this module
# copies the ones it wants across with an expected-old `update-ref`.
#
# Measured both ways: with a hostile `url.…insteadOf` in the repository's own config, an ordinary
# fetch followed it, and the same fetch through the scratch gitdir did not — git tried the literal
# URL. A legitimate fetch through it works and its objects are readable from the real repository.
#
# This also closes what #842 recorded as deferred: `http.<url>.sslVerify=false` written after
# `refuse_insecure_tls()` cannot take effect either, because it is in a file git no longer opens.
# ======================================================================================


@contextlib.contextmanager
def _isolated_gitdir(repo: Repo):
    """A scratch `$GIT_DIR` for one network command, sharing the real object store.

    Deliberately minimal: `HEAD`, an empty `refs/`, and a config with nothing in it but the
    repository format. Anything else would be another key the operation could be steered by.
    """
    tmp = tempfile.mkdtemp(prefix=".battlelab-net-", dir=repo.gitdir)
    try:
        os.mkdir(os.path.join(tmp, "refs"))
        with open(os.path.join(tmp, "HEAD"), "w", encoding="utf-8") as fh:
            fh.write("ref: refs/heads/placeholder\n")
        with open(os.path.join(tmp, "config"), "w", encoding="utf-8") as fh:
            fh.write("[core]\n\trepositoryformatversion = 0\n\tbare = true\n")
        yield tmp
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_git_net(
    repo: Repo,
    gitdir: str,
    args: list[str],
    *,
    timeout: float = NET_TIMEOUT_S,
    extra_config: list[str] | None = None,
    ssh_command: str | None = None,
    allow_protocol: str | None = None,
) -> str:
    """Run one NETWORK command against the scratch gitdir, never the repository's own.

    No `--work-tree`: nothing here touches a working tree, and naming one would only give the
    command a directory it has no business in.
    """
    exe = git_bin()
    if not exe:
        raise GitError("git is not installed on this host", status=501)
    env = _write_env(home_root(), gitdir, "")
    env.pop("GIT_WORK_TREE", None)
    # The real object store — `common()`, not `gitdir`. For a LINKED WORKTREE those differ:
    # `HEAD` and the index are per-worktree while objects and refs live in the shared directory,
    # which is the whole reason `Repo.common()` exists. Pointing at `<gitdir>/objects` there would
    # name a path that does not exist, and a fetch would quietly build its own store beside the
    # real one — objects written nowhere useful, refs pointing at them.
    env["GIT_OBJECT_DIRECTORY"] = os.path.join(repo.common(), "objects")
    if ssh_command:
        env["GIT_SSH_COMMAND"] = ssh_command
    if allow_protocol:
        env["GIT_ALLOW_PROTOCOL"] = allow_protocol
    argv = [
        exe,
        f"--git-dir={gitdir}",
        "-c",
        f"core.hooksPath={hooks_void()}",
        "-c",
        "core.fsmonitor=false",
        *(extra_config or []),
        *args,
    ]
    return _run_argv(argv, env, repo.gitdir, timeout).decode("utf-8", "replace")


def _fetch_isolated(repo: Repo, url: str, remote: str, dest) -> dict[str, str]:
    """Fetch every head into a scratch gitdir, then copy the tracking refs into the repository.

    Returns `{branch: oid}` for what was fetched. The refs move across with `update-ref` against
    the value each one held BEFORE the network call, so a concurrent fetch that already advanced
    a tracking ref is not rolled backwards by this one.
    """
    had = {}
    for line in run_git_write(
        repo, ["for-each-ref", "--format=%(refname) %(objectname)", f"refs/remotes/{remote}/"]
    ).splitlines():
        ref, _, oid = line.partition(" ")
        if ref and oid:
            had[ref] = oid
    with _isolated_gitdir(repo) as gitdir:
        run_git_net(
            repo,
            gitdir,
            # `--upload-pack` pinned on the COMMAND LINE: measured, `-c` does not override the
            # repository's own `remote.<n>.uploadpack` — and here that key is unreachable anyway.
            #
            # An explicit refspec is REQUIRED once the URL is pinned (a URL carries no configured
            # refspec) and is a control in its own right: `remote.<n>.fetch` would otherwise be
            # repo-controlled, and `+refs/heads/*:refs/heads/*` would overwrite LOCAL branches.
            [
                "fetch",
                "--upload-pack",
                "git-upload-pack",
                "--no-tags",
                "--",
                url,
                f"+refs/heads/*:refs/remotes/{remote}/*",
            ],
            # Forces TLS verification on for THIS url specifically. Still passed even though the
            # repository's competing key is now unreachable: the pin is the guarantee, not the
            # absence of a rival.
            extra_config=dest.pins,
            ssh_command=dest.ssh_command,
            allow_protocol=dest.allow_protocol,
        )
        got: dict[str, str] = {}
        for line in run_git_net(
            repo,
            gitdir,
            ["for-each-ref", "--format=%(refname) %(objectname)", f"refs/remotes/{remote}/"],
            timeout=LOCAL_TIMEOUT_S,
        ).splitlines():
            ref, _, oid = line.partition(" ")
            if ref and oid:
                got[ref] = oid
    for ref, oid in got.items():
        if had.get(ref) == oid:
            continue
        try:
            run_git_write(repo, ["update-ref", ref, oid, had.get(ref, "")])
        except GitError:
            # Something else moved this tracking ref while we were on the network. Theirs is
            # newer by definition; ours is not worth forcing over it.
            continue
    prefix = f"refs/remotes/{remote}/"
    return {r[len(prefix) :]: o for r, o in got.items() if r.startswith(prefix)}


def git_fetch(path: str | None, remote: object = None) -> dict:
    """Fetch one remote. Network-bound, bounded, and never prompts for credentials."""
    repo = resolve_repo(path)
    remotes = list_remotes(repo)
    if not remotes:
        raise FsError("this repository has no remotes", status=409)
    name = validate_ref(remote, what="remote") if remote is not None else None
    if name is None:
        branch = _head_branch(repo)
        up = _upstream(repo, branch) if branch else None
        name = up.split("/", 1)[0] if up else (remotes[0] if len(remotes) == 1 else None)
    if name is None:
        raise FsError(
            "this repository has several remotes and no upstream — name the one to fetch",
            status=409,
        )
    if name not in remotes:
        raise FsError(f"unknown remote {name!r}", status=422)
    refuse_insecure_tls(repo)
    # Resolve the destination ONCE, refuse on what was resolved, and hand git that same string.
    # Passing the remote NAME instead would let the config move between the check and the
    # invocation — the race that made local-path containment unfixable.
    url = _effective_url(repo, name, push=False)
    refuse_url_rewrites(repo, url)
    dest = admit_destination(url, name)

    def run() -> dict:
        _fetch_isolated(repo, url, name, dest)
        return {"remote": name, "status": _fresh_status(repo)}

    return _guarded(repo, run)


def git_pull(path: str | None) -> dict:
    """Fetch, then fast-forward. Never a merge, never a rebase.

    Never ``git pull --ff-only`` either: a repository's own ``pull.rebase`` reshapes what ``pull``
    does, and a fast-forward is the only outcome this route is allowed to have. Nothing here runs
    a merge driver, which is how conflict resolution stays out of this feature.

    Implemented as ``fetch``, then an expected-old ``update-ref`` on the branch, then ``read-tree``
    to settle the worktree — deliberately NOT ``merge --ff-only``, which advances whatever HEAD
    names when it runs and so could only ever be checked next to, never bound to, the branch the
    operator chose. The two halves are reported separately: once the ref moves the pull HAS
    happened, and a worktree that could not be settled afterwards is a different fact from a pull
    that failed.
    """
    repo = resolve_repo(path)
    branch = _head_branch(repo)
    if not branch:
        raise FsError("HEAD is detached — check out a branch before pulling", status=409)
    if _has_unmerged(repo):
        raise FsError("resolve the conflict in the session before pulling", status=409)
    up = _upstream(repo, branch)
    if not up:
        raise FsError(f"`{branch}` has no upstream to pull from", status=409)
    remote = up.split("/", 1)[0]
    refuse_insecure_tls(repo)
    url = _effective_url(repo, remote, push=False)
    refuse_url_rewrites(repo, url)
    dest = admit_destination(url, remote)
    # A fast-forward with a dirty tree SUCCEEDS and moves HEAD (measured: an unrelated modified
    # file survived and the branch advanced anyway). git is right that nothing was clobbered, but
    # this panel is docked into a session an agent is working in, and silently changing the base
    # under live work is the same class of hazard as `switch` carrying edits across. The way
    # forward — commit or discard — is in this same tab, so the refusal names it.
    clean, n = _is_clean(repo)
    if not clean:
        raise FsError(
            f"{n} uncommitted change{'s' if n != 1 else ''} would be left sitting on a new base — "
            "commit or discard them first",
            status=409,
        )

    def run() -> dict:
        # Cleanliness, re-checked where it can actually hold. The early refusal above is stale
        # the instant it is read: the session agent shares this worktree and never takes the
        # panel's lock, and a probe dirtied a tracked file while `_guarded` was being entered —
        # pull fast-forwarded anyway and left that edit sitting on the new base, which is exactly
        # what the named refusal promises will not happen.
        again, again_n = _is_clean_now(repo)
        if not again:
            raise FsError(
                f"{again_n} uncommitted change{'s' if again_n != 1 else ''} would be left sitting "
                "on a new base — commit or discard them first",
                status=409,
            )
        # Through a scratch gitdir, so nothing in this repository's config can redirect it.
        _fetch_isolated(repo, url, remote, dest)
        # The fetch is a network round trip — the widest gap in this function — so the
        # preconditions are re-read AFTER it. A tree dirtied during the fetch would otherwise be
        # fast-forwarded over.
        still, still_n = _is_clean_now(repo)
        if not still:
            raise FsError(
                f"{still_n} uncommitted change{'s' if still_n != 1 else ''} appeared while "
                "fetching — commit or discard them before pulling",
                status=409,
            )
        # The exact commit just FETCHED, not the tracking-ref name. A name is re-resolved by git
        # at merge time, so a second fetch landing in between would advance onto something this
        # call never saw.
        fetched = run_git_write(repo, ["rev-parse", "--verify", up]).strip()
        try:
            base = run_git_write(repo, ["rev-parse", "--verify", f"refs/heads/{branch}"]).strip()
        except GitError:
            base = ""  # unborn: the pull is what gives this branch its first commit
        if base:
            if base == fetched:
                return {
                    "branch": branch,
                    "upstream": up,
                    "settled": True,
                    "settle_error": None,
                    "status": _fresh_status(repo),
                }
            try:
                run_git_write(repo, ["merge-base", "--is-ancestor", base, fetched])
            except GitError as e:
                raise GitError(
                    f"`{branch}` has diverged from {up} — a pull here is fast-forward only. "
                    f"Rebase or merge in the session, then pull again. ({e})",
                    status=409,
                ) from None
        # THE BRANCH ITSELF, moved by compare-and-swap — and this is the ordering fix.
        #
        # `git merge --ff-only` advances whatever HEAD happens to name when it runs, so the check
        # that HEAD was still on the intended branch was check-then-use with git on the far side
        # of it: a probe switched branches at that boundary and the pull fast-forwarded a branch
        # nobody had selected. Naming the ref and giving `update-ref` the value it is expected to
        # still hold removes both halves of that — the wrong branch cannot be the one that moves,
        # and a branch that advanced underneath us fails the swap instead of being overwritten.
        #
        # A second consequence, and a welcome one: `merge` runs the repository's own `post-merge`
        # hook. `update-ref` and `read-tree` do not. This module already refuses a repo-configured
        # gpg helper and pins the transport for the same reason — a panel operation should not be
        # a way to make a checked-in script run — so losing that hook is the behaviour this route
        # should have had. Smudge filters still run under `read-tree -u`, exactly as they did
        # under the merge, and the module docstring already records those as not mitigated.
        try:
            run_git_write(repo, ["update-ref", f"refs/heads/{branch}", fetched, base])
        except GitError:
            raise GitError(
                f"`{branch}` moved while the panel was fetching, so nothing was merged. Refresh "
                "and pull again.",
                status=409,
            ) from None
        # THE BRANCH HAS NOW ADVANCED — everything below is settling the worktree to match, and
        # is reported separately for the same reason push reports its bookkeeping separately: a
        # failure here does not undo the ref move, and telling the operator the pull failed when
        # the branch did advance sends them to retry something already done.
        settled = True
        settle_error: str | None = None
        try:
            if _head_branch(repo) != branch:
                # They switched away mid-fetch. The branch they asked for is up to date, which is
                # the whole of what "pull this branch" means; there is simply no worktree here
                # belonging to it any more.
                raise FsError(
                    f"`{branch}` was updated, but the worktree had already been switched to "
                    "another branch, so it was left alone.",
                    status=409,
                )
            if base:
                # HEAD is a symref to the ref that just moved, so the worktree is now one commit
                # range behind its own branch. `-m` refuses rather than clobbers if anything
                # local would be overwritten.
                run_git_write(repo, ["read-tree", "-u", "-m", base, fetched])
            else:
                run_git_write(repo, ["read-tree", "-u", "--reset", fetched])
        except (GitError, FsError) as e:
            settled = False
            settle_error = redact(str(e))
        try:
            final_status = _fresh_status(repo)
        except (GitError, FsError) as e:
            settled = False
            settle_error = settle_error or redact(str(e))
            final_status = None
        return {
            "branch": branch,
            "upstream": up,
            "settled": settled,
            "settle_error": settle_error,
            "status": final_status,
        }

    return _guarded(repo, run)


def git_switch(
    path: str | None,
    branch: object,
    create: object = None,
    start: object = None,
    expect: object = None,
) -> dict:
    """Switch to a branch, or create one and switch to it. Refuses on a dirty tree.

    Measured: ``git switch`` with uncommitted changes **carries them onto the other branch**
    silently. In a pane docked to a session an agent is editing, that is work-loss wearing a
    success message — so a dirty tree is a refusal, and the panel's own commit/discard controls are
    the way forward.
    """
    repo = resolve_repo(path)
    create = require_bool(create, "create", default=False)
    name = validate_ref(branch)
    check_ref_format(repo, name)
    # Read early only so an obviously dirty tree refuses fast; the BINDING check is inside the
    # lock below, because this value is stale the instant it is read — the session agent shares
    # the worktree and never takes the panel's lock.
    clean, n = _is_clean(repo)
    if not clean:
        raise FsError(
            f"{n} uncommitted change{'s' if n != 1 else ''} would follow you onto `{name}` — "
            "commit or discard them first",
            status=409,
        )
    if not create:
        # MEASURED: `git switch -- <name>` with a single matching remote-tracking ref CREATES a
        # local branch and reports success. `--` does not stop it: DWIM is not option parsing.
        # A `create:false` request that can still bring a branch into existence has inverted the
        # only thing the flag says, which is the same class of bug as `bool("false")` being True.
        # Membership in the freshly-listed LOCAL set is what makes "switch" mean switch.
        if name not in set(git_branches(repo.toplevel).get("local", [])):
            raise FsError(
                f"`{name}` is not a local branch in this repository. Tick create to make it, or "
                "pick one from the list.",
                status=422,
            )
    start_point = validate_ref(start, what="start point") if start is not None else None
    start_oid: str | None = None
    if start_point is not None:
        # Shape-validating a start point is not enough: it reaches git as a REVISION, so an
        # arbitrary object id or `HEAD~40` would be accepted. #806 says branch creation starts
        # from HEAD or a listed ref, so membership in the freshly-listed set is what enforces it.
        known = git_branches(repo.toplevel)
        if start_point not in set(known.get("local", [])) | set(known.get("remote", [])):
            raise FsError(f"{start_point!r} is not one of this repository's branches", status=422)
        # Resolved to an object HERE, alongside the membership check, so the two agree on the same
        # commit. Validating a name and then handing that name to git is check-then-use with the
        # gap on git's side of the call.
        start_oid = run_git_write(repo, ["rev-parse", "--verify", start_point]).strip()

    def run() -> dict:
        # The dirty-tree refusal, re-checked where it can actually hold. `switch` silently
        # carries uncommitted work across, so a tree that went dirty after the check above would
        # have had those edits dragged onto another branch.
        verify_scalar(repo, expect, "dirty_fp", "the set of uncommitted changes")
        fresh_clean, fresh_n = _is_clean_now(repo)
        if not fresh_clean:
            raise FsError(
                f"{fresh_n} uncommitted change{'s' if fresh_n != 1 else ''} would be carried onto "
                "the other branch — commit or discard them first",
                status=409,
            )
        # Where to come back to if the switch turns out to have carried work with it.
        origin = _head_branch(repo)
        if create:
            # `switch -c <new> [<start-point>]` — the start point is a REF, not a pathspec, so it
            # is validated as a name above rather than fenced off with `--`.
            # Last look before the command: `switch` CARRIES uncommitted work across, so an
            # edit landing after the earlier check would be dragged onto the target branch.
            last, last_n = _is_clean_now(repo)
            if not last:
                raise FsError(
                    f"{last_n} uncommitted change{'s' if last_n != 1 else ''} appeared while the "
                    "panel was switching — commit or discard them first",
                    status=409,
                )
            args = ["switch", "--create", name]
            if start_point:
                # The OID the membership check validated, not the name. `git switch -c <n> <ref>`
                # resolves that mutable name when it runs, so a start point that moved after
                # validation produced a branch at an unseen commit — reproduced by the reviewer.
                args.append(start_oid or start_point)
            run_git_write(repo, args)
        else:
            # `--no-guess` is the structural half of the same refusal: the membership check above
            # is read-then-act, so a branch could in principle vanish between them, and this makes
            # the command itself incapable of inventing one. Measured: without it, `switch -- x`
            # creates and succeeds; with it, `fatal: invalid reference: x`.
            run_git_write(repo, ["switch", "--no-guess", "--", name])
        # AFTER, because there is no before that can hold.
        #
        # Every other write here is bound to an object, so it either applies to what was verified
        # or fails. `switch` cannot be: git has no conditional checkout, and the cleanliness that
        # makes a switch safe is a property of the whole worktree that only the command itself
        # could observe atomically. Checking again one syscall earlier does not change that, and
        # a probe writing at exactly that boundary had its edit carried onto the other branch.
        #
        # What saves it is that the failure is REVERSIBLE. `switch` carries uncommitted work
        # across rather than dropping it, so the edit is not lost — it is merely somewhere the
        # operator did not put it, and switching back carries it home. That makes a compensating
        # undo a complete one, which is a stronger guarantee than a narrower window would be.
        carried, carried_n = _is_clean_now(repo)
        if not carried:
            failed = _undo_switch(repo, origin, name, bool(create))
            n = f"{carried_n} uncommitted change{'s' if carried_n != 1 else ''}"
            if failed:
                # The compensation did not compensate. Saying "it went back" here would be a
                # confident lie about where the operator is standing, and the recovery advice
                # that follows from it would be wrong too.
                raise FsError(
                    f"{n} appeared while the panel was switching, and putting things back did "
                    f"not work: {failed}. You are on `{_head_branch(repo) or 'a detached commit'}` "
                    f"and your edits are there with you — sort this out in the session's "
                    "terminal before switching again.",
                    status=409,
                )
            raise FsError(
                f"{n} appeared while the panel was switching, so it went back to `{origin}` and "
                "those edits are still there. Commit or discard them, then switch again.",
                status=409,
            )
        return {"branch": name, "created": bool(create), "status": _fresh_status(repo)}

    return _guarded(repo, run)


def git_branch_delete(path: str | None, branch: object) -> dict:
    """``git branch -d`` only. An unmerged branch is a refusal, never a force path.

    ``-D`` is not reachable from this module at all, which is what keeps "no force variant exists
    in the API" literally true rather than a promise about client behaviour.
    """
    repo = resolve_repo(path)
    name = validate_ref(branch)
    current = _head_branch(repo)
    if name == current:
        raise FsError(
            f"`{name}` is the current branch — switch away before deleting it", status=409
        )

    def run() -> dict:
        try:
            run_git_write(repo, ["branch", "--delete", "--", name])
        except GitError as e:
            text = str(e)
            if "not fully merged" in text:
                raise GitError(
                    f"`{name}` is not fully merged, so the panel will not delete it. "
                    "Merge it, or delete it in the session if you mean to lose those commits.",
                    status=409,
                ) from None
            raise
        return {"deleted": name, "status": _fresh_status(repo)}

    return _guarded(repo, run)


def _unborn(repo: Repo) -> bool:
    """True when HEAD points at a branch that has no commit yet.

    Unstaging installs HEAD's object for the path; on an unborn branch there is no HEAD to read
    one from, so it drops the index entry outright instead — which is what "unstage" means when
    nothing has ever been committed. Detected up front rather than by catching git's error text:
    a message is a string that can change, a ref either resolves or does not.
    """
    try:
        run_git_write(repo, ["rev-parse", "--verify", "--quiet", "HEAD"])
    except GitError:
        return True
    return False


# ======================================================================================
# Binding a verified observation through the mutation
#
# Every write below used to have the same shape: read the worktree, compare it against what the
# operator was shown, then run a git command that reads the worktree AGAIN. That second read is
# the whole problem. This panel is docked into a session whose agent shares the worktree and
# takes no lock, so the bytes git finally acts on are not necessarily the bytes anyone verified.
#
# The response, four times over, was to move the check closer to the command and write a comment
# admitting the window could not be closed. That was wrong on both counts. A narrowed race is
# still a race, and it loses data at whatever rate the machine can hit it — a reviewer's probes
# hit it on the first try, repeatedly. And the window is closeable; it just is not closeable by
# checking harder.
#
# What closes it is refusing to let git re-read. Name the exact OBJECT the operation applies to
# and hand git that object:
#
#   * staging hashes the file first and installs THAT blob id in the index, so a later edit
#     changes the file, never what was staged;
#   * unstaging installs HEAD's blob, which is immutable, so there is nothing left to race;
#   * discarding displaces the current bytes with `rename` — which moves them without reading
#     them first — and then creates the replacement with `O_EXCL`, so a writer that claims the
#     freed name is DETECTED rather than overwritten;
#   * pull advances the branch with an expected-old `update-ref` and only then settles the
#     worktree, so a branch that moved underneath us fails cleanly instead of fast-forwarding
#     something nobody selected;
#   * switch cannot be bound this way at all — git offers no conditional checkout — so it is
#     made REVERSIBLE instead: `switch` carries uncommitted work rather than dropping it, which
#     means carrying it back is a complete undo.
#
# The one thing none of these do is pretend. Where a guarantee is not available, the operation
# refuses or reports; it does not narrow the window and call it safe.
# ======================================================================================

#: Bounded retries for the displace/claim loop. Each turn requires another writer to have won a
#: race in the microseconds between two syscalls; a handful of losses in a row is not contention,
#: it is something writing in a loop, and continuing to fight it would be the wrong answer.
REPLACE_ATTEMPTS = 8

#: Prefix for the temporary name a displaced file is moved to. Inside the same directory, because
#: `rename` is only atomic within one filesystem, and prefixed so a leftover from a killed process
#: is identifiable rather than mysterious.
DISPLACED_PREFIX = ".battlelab-displaced-"

#: How many index entries or paths go into one git invocation. Bounded so a 500-path request
#: cannot build an argv near `ARG_MAX`, and small enough that a failure names a readable subset.
_BATCH = 200

_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _configured_upstream(repo: Repo, branch: str) -> bool:
    """True when `branch` already names a remote to track."""
    try:
        return bool(run_git_write(repo, ["config", "--get", f"branch.{branch}.remote"]).strip())
    except (GitError, FsError):
        return False


def _undo_switch(repo: Repo, origin: str | None, created: str, was_created: bool) -> str | None:
    """Go back to `origin`, carrying any late edits home with us.

    This is the compensating half of the switch. It relies on the same behaviour that made the
    original problem — `switch` moves uncommitted work rather than refusing it — which is what
    makes going back a real undo rather than a second guess: the edits end up where they were.

    A branch this call had just created is removed too, so a refused switch leaves no trace. It
    is deleted with `-d`, never `-D`: the branch points at the commit it was made from and is
    fully merged, so if git disagrees something else has happened and the branch stays.
    """
    if not origin:
        return "HEAD was detached, so there was no branch to go back to"
    try:
        run_git_write(repo, ["switch", "--no-guess", "--", origin])
    except (GitError, FsError) as e:
        return f"could not switch back to `{origin}`: {redact(str(e))}"
    # Believe the ref, not the exit status. This is the one place whose whole job is to restore a
    # state, and reporting success from a command's silence is how a caller ends up telling the
    # operator they are somewhere they are not.
    landed = _head_branch(repo)
    if landed != origin:
        return f"tried to go back to `{origin}` but HEAD is on `{landed or 'a detached commit'}`"
    if was_created and created != origin:
        try:
            run_git_write(repo, ["branch", "--delete", "--", created])
        except (GitError, FsError) as e:
            return (
                f"went back to `{origin}`, but `{created}` could not be removed: {redact(str(e))}"
            )
    return None


def _walk_to_parent(repo: Repo, name: str) -> tuple[int, str]:
    """Open the directory holding `name`, one ``O_NOFOLLOW`` step at a time.

    `validate_paths` already contained the path as a STRING. That is not the same as containing
    it at the moment of the write: a component can become a symlink between the two, and a
    string check has nothing to say about it. Descending by descriptor and refusing to follow a
    link at every level is what makes "inside this worktree" true when the bytes actually move.

    The caller owns the returned descriptor and must close it.
    """
    parts = [p for p in name.split("/") if p]
    if not parts:
        raise FsError("that path names no file", status=422)
    fd = os.open(repo.toplevel, _DIR_FLAGS)
    try:
        for part in parts[:-1]:
            try:
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                # `git restore` recreates a directory the operator removed, and refusing here
                # would make "discard this deletion" impossible in exactly the case the control
                # exists for — `rm -rf sub/` then discarding `sub/a.txt`. Measured: the walk
                # refused where `restore` had rebuilt the path.
                #
                # Creating it changes nothing about containment: the `O_NOFOLLOW` open below is
                # still what vets the result, so a symlink swapped into the gap is refused rather
                # than followed, and `FileExistsError` (something else won the race) simply falls
                # through to that same check instead of being special-cased.
                try:
                    os.mkdir(part, 0o755, dir_fd=fd)
                except FileExistsError:
                    pass
                nxt = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nxt
    except OSError:
        os.close(fd)
        raise FsError(
            f"{name!r} could not be opened inside this worktree — a directory on the way to it "
            "is missing, or is a symbolic link",
            status=409,
        ) from None
    return fd, parts[-1]


def _preserve_displaced(repo: Repo, dir_fd: int, aside: str, rel_dir: str) -> str:
    """Hash an already-displaced file into the object database, then remove it.

    **The removal happens only if the hash succeeded**, and that ordering is the whole of this
    function's safety. Written the obvious way — hash in a `try`, unlink in a `finally` — a git
    failure would swallow the error AND delete the only copy of the bytes, which is a worse
    version of the defect this displacement exists to fix. Unreachable in practice and
    catastrophic when reached is exactly the combination worth spending four lines on.

    On failure the file is LEFT where it is, under its `.battlelab-displaced-` name, and the
    caller refuses. It is deliberately not moved back: the original name may already have been
    claimed by whatever else is writing, and renaming over that would destroy a third set of
    bytes while cleaning up after the second. Refusing with the file intact and its location
    named costs the operator one `mv`; guessing could cost them work.
    """
    rel = f"{rel_dir}{aside}"
    try:
        if _stat.S_ISLNK(os.lstat(aside, dir_fd=dir_fd).st_mode):
            # A symlink's content IS its target; `hash-object` on a path would follow it and
            # store whatever it pointed at, which is not what was displaced.
            target = os.readlink(aside, dir_fd=dir_fd).encode("utf-8", "surrogateescape")
            oid = run_git_write(repo, ["hash-object", "-w", "--stdin"], stdin=target).strip()
        else:
            # `--no-filters` because this is a PRE-IMAGE. A clean filter would store the
            # converted form, and the bytes the operator would want back are the ones that were
            # on disk, not git's idea of them.
            oid = run_git_write(repo, ["hash-object", "-w", "--no-filters", "--", rel]).strip()
    except (GitError, FsError, OSError) as e:
        raise FsError(
            f"the bytes at that path could not be copied into git, so nothing was discarded. "
            f"They are intact at {rel!r} — move that back into place. ({e})",
            status=409,
        ) from None
    if not oid:
        raise FsError(
            f"git returned no object id for the bytes it was asked to preserve, so nothing was "
            f"discarded. They are intact at {rel!r} — move that back into place.",
            status=409,
        )
    try:
        os.unlink(aside, dir_fd=dir_fd)
    except OSError:
        pass  # the bytes are in the object database; a leftover temp file is not worth failing on
    return oid


def _displace(repo: Repo, dir_fd: int, leaf: str, rel_dir: str) -> str | None:
    """Move whatever is at `leaf` aside ATOMICALLY, then hash it. Returns the oid, or None.

    `rename(2)` is the entire argument for this shape. It displaces the bytes that are at the
    name *at that instant*, without reading them first — so unlike "hash the file, then overwrite
    it", there is no interval during which content can arrive and be destroyed unseen. Whatever
    was there is already safe under a name only this call knows before anything is hashed.
    """
    aside = DISPLACED_PREFIX + os.urandom(8).hex()
    try:
        os.rename(leaf, aside, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except FileNotFoundError:
        return None  # nothing at the name — a tracked path deleted in the worktree
    except OSError as e:
        raise FsError(f"{leaf!r} could not be set aside: {e.strerror}", status=409) from None
    return _preserve_displaced(repo, dir_fd, aside, rel_dir)


def _claim_symlink(dir_fd: int, leaf: str, target: bytes) -> bool:
    """Create `leaf` as a symlink to `target`. False means someone else claimed the name first."""
    try:
        os.symlink(target.decode("utf-8", "surrogateescape"), leaf, dir_fd=dir_fd)
    except FileExistsError:
        return False
    except OSError as e:
        raise FsError(f"{leaf!r} could not be restored: {e.strerror}", status=409) from None
    return True


def _claim_fd(dir_fd: int, leaf: str, mode: str) -> int | None:
    """Create `leaf` and return a writable fd, or None if someone else claimed the name first.

    `O_EXCL` is the load-bearing part: it is the difference between "this file is ours because we
    made it" and "this file is ours because it was free a moment ago". Without it the replacement
    would silently overwrite a writer that won the gap — the exact loss this path exists to stop.
    """
    perm = 0o755 if mode == "100755" else 0o644
    try:
        return os.open(
            leaf,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            perm,
            dir_fd=dir_fd,
        )
    except FileExistsError:
        return None
    except OSError as e:
        raise FsError(f"{leaf!r} could not be restored: {e.strerror}", status=409) from None


def _cat_blob_into(repo: Repo, oid: str, fd: int) -> None:
    """Write the blob `oid` straight into `fd`, with no buffer in between.

    Reading it through the ordinary runner would be a **silent truncation**: stdout is capped at
    `_MAX_STDOUT` (1 MiB) and the cap is not an error unless the caller asks for one, so a discard
    of any larger tracked file would have restored a file cut off at the cap while reporting
    success. Handing git the descriptor removes the size question entirely — the bytes never pass
    through this process — and the descriptor is the one `O_EXCL` just created, so nothing else
    can be holding it.
    """
    exe = git_bin()
    if not exe:
        raise GitError("git is not installed on this host", status=501)
    proc = subprocess.Popen(  # noqa: S603 - literal argv, no shell, allowlisted subcommands
        [*_base_argv(exe, repo, repo.toplevel), "cat-file", "blob", oid],
        stdin=subprocess.DEVNULL,
        stdout=fd,
        stderr=subprocess.PIPE,
        env=_write_env(home_root(), repo.gitdir, repo.toplevel),
        cwd=repo.toplevel,
        start_new_session=True,
    )
    try:
        _, err = proc.communicate(timeout=LOCAL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _reap(proc)
        raise GitError("git took too long and was stopped", status=504) from None
    if proc.returncode != 0:
        raise GitError(redact((err or b"").decode("utf-8", "replace").strip()) or "git failed")


def _parse_ls(
    out: str, oid_at: int, wanted: set[str], stage_at: int | None = None
) -> dict[str, tuple[str, str]]:
    """Parse `-z` records shaped `<meta...>\t<path>` into `{path: (mode, oid)}`.

    `oid_at` is the field index of the object id, which differs between `ls-files --stage`
    (mode, oid, stage) and `ls-tree` (mode, type, oid). `stage_at`, when given, keeps only stage
    0 — a settled index entry; an unmerged path carries stages 1-3 and is refused upstream rather
    than half-read here.

    Splitting on the FIRST tab is what keeps a path containing one intact — the same hazard that
    rules out `--index-info` on the write side.
    """
    found: dict[str, tuple[str, str]] = {}
    for record in out.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        bits = meta.split()
        if len(bits) <= oid_at or path not in wanted:
            continue
        if stage_at is not None and (len(bits) <= stage_at or bits[stage_at] != "0"):
            continue
        found[path] = (bits[0], bits[oid_at])
    return found


def _index_entries(repo: Repo, names: list[str]) -> dict[str, tuple[str, str] | None]:
    """`(mode, oid)` at stage 0 of the index for each name, or None where the index has no such
    path. One `ls-files` per batch rather than per path."""
    found: dict[str, tuple[str, str]] = {}
    for i in range(0, len(names), _BATCH):
        chunk = names[i : i + _BATCH]
        out = run_git_write(repo, ["ls-files", "--stage", "-z", "--", *chunk])
        found.update(_parse_ls(out, 1, set(chunk), stage_at=2))
    return {n: found.get(n) for n in names}


def _restore_from_index(repo: Repo, name: str, entry: tuple[str, str] | None) -> list[str]:
    """Put the index's content at `name`, preserving every set of bytes displaced on the way.

    The loop is the point, and it is what the earlier "hash it, then `git restore` over it"
    could not do. Displacing the current file frees the name, and a writer can claim it before
    we do; `O_EXCL` is what tells us that happened rather than letting us overwrite it. When it
    does happen, THAT file is displaced and hashed too, and we go round again.

    So the invariant holds all the way through: every distinct set of bytes that occupied this
    name during the operation is in the object database and is returned to the caller, and the
    content that finally lands is content this function created itself.
    """
    if entry is None:
        raise FsError(
            f"{name!r} is not in the index, so there is nothing to restore it from. Handle it in "
            "this session's terminal.",
            status=409,
        )
    mode, oid = entry
    if mode == "160000":
        raise FsError(
            f"{name!r} is a submodule. Discarding it means moving another repository's checkout, "
            "which this panel does not do — handle it in this session's terminal.",
            status=409,
        )
    # A symlink's content is its target — bounded by PATH_MAX, so reading it into memory is
    # safe, and `require_complete` turns the stdout cap into a refusal rather than a short link.
    link_target = (
        run_git_bytes(repo, ["cat-file", "blob", oid], require_complete=True)
        if mode == "120000"
        else b""
    )
    rel_dir = name.rsplit("/", 1)[0] + "/" if "/" in name else ""
    dir_fd, leaf = _walk_to_parent(repo, name)
    saved: list[str] = []
    try:
        for _ in range(REPLACE_ATTEMPTS):
            got = _displace(repo, dir_fd, leaf, rel_dir)
            if got:
                saved.append(got)
            if mode == "120000":
                if _claim_symlink(dir_fd, leaf, link_target):
                    return saved
                continue
            fd = _claim_fd(dir_fd, leaf, mode)
            if fd is None:
                continue  # someone else got the name; displace THEM too and go again
            try:
                _cat_blob_into(repo, oid, fd)
            except (GitError, FsError):
                # We created this file, so removing it is ours to do — and leaving a
                # half-written one at a tracked path would be worse than the refusal.
                os.close(fd)
                fd = -1
                with contextlib.suppress(OSError):
                    os.unlink(leaf, dir_fd=dir_fd)
                raise
            finally:
                if fd >= 0:
                    os.close(fd)
            return saved
        raise FsError(
            f"{name!r} kept being rewritten while it was being discarded, so the panel stopped "
            "rather than fight whatever is writing it. Nothing was lost — every version it saw "
            "is recoverable by object id.",
            status=409,
        )
    finally:
        os.close(dir_fd)


def _lstat_or_none(path: str) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as e:
        raise FsError(f"{path!r} could not be read: {e.strerror}", status=409) from None


def _identical(a: os.stat_result | None, b: os.stat_result | None) -> bool:
    """Whether two stats describe the same unchanged file — absence included."""
    if a is None or b is None:
        return a is None and b is None
    return (a.st_ino, a.st_size, a.st_mtime_ns, a.st_mode) == (
        b.st_ino,
        b.st_size,
        b.st_mtime_ns,
        b.st_mode,
    )


def _snapshot_for_index(repo: Repo, names: list[str]) -> dict[str, tuple[str, str] | None]:
    """Hash these worktree paths, and prove none of them moved while being hashed.

    Maps each name to `(mode, oid)`, or to None where the path is absent — a deletion to stage.

    The stat either side is what upgrades this from "hash then hope" to a snapshot: if anything
    changed while it was being read, the pair disagrees and the whole set is taken again. What
    comes back is a set of blob ids the caller installs directly, so nothing downstream looks at
    the worktree a second time.

    Hashing is ONE `hash-object` call for all the regular files rather than one per path.
    Measured on 200 files: per-path was 1.24s against 0.01s for a plain `git add`, and this runs
    under the repository lock — a hundredfold slowdown on a routine multi-select is not a price
    worth paying for a guarantee that a batch gives just as well. `hash-object` prints one oid
    per input line, in order, which is what makes the batch safe to zip back together.
    """
    full = {n: os.path.join(repo.toplevel, n) for n in names}
    for _ in range(REPLACE_ATTEMPTS):
        before = {n: _lstat_or_none(full[n]) for n in names}
        # Carried as (name, stat) pairs rather than names alone, so the stat is non-None by
        # construction where it is used. The alternative needed an `assert` to re-establish what
        # this comprehension already decided — and an assert is stripped under `python -O`, which
        # makes it exactly the wrong tool for a fact the next line depends on.
        regular = [
            (n, st)
            for n, st in before.items()
            if st is not None and not _stat.S_ISLNK(st.st_mode) and not _stat.S_ISDIR(st.st_mode)
        ]
        out: dict[str, tuple[str, str] | None] = {}
        for i in range(0, len(regular), _BATCH):
            chunk = regular[i : i + _BATCH]
            # Filters ARE applied here, deliberately: converting worktree content into an index
            # entry is what staging means, and `git add` in the same worktree would run them too.
            # The module docstring records that this executes a repo-configured clean driver.
            oids = run_git_write(repo, ["hash-object", "-w", "--", *(n for n, _ in chunk)]).split()
            if len(oids) != len(chunk):
                raise GitError("git did not return one object id per path", status=500)
            for (n, st), oid in zip(chunk, oids, strict=True):
                out[n] = ("100755" if st.st_mode & 0o111 else "100644", oid)
        for n, st in before.items():
            if st is None:
                out[n] = None
            elif _stat.S_ISDIR(st.st_mode):
                # A directory at a tracked path is a submodule checkout. Its "content" is another
                # repository's HEAD, not a blob, so there is no object here to bind to.
                out[n] = ("160000", "")
            elif _stat.S_ISLNK(st.st_mode):
                target = os.readlink(full[n]).encode("utf-8", "surrogateescape")
                out[n] = (
                    "120000",
                    run_git_write(repo, ["hash-object", "-w", "--stdin"], stdin=target).strip(),
                )
        if all(_identical(before[n], _lstat_or_none(full[n])) for n in names):
            return out
    raise FsError(
        "those files kept changing while they were being staged, so nothing was staged. Let them "
        "settle and try again.",
        status=409,
    )


def _head_entries(repo: Repo, names: list[str]) -> dict[str, tuple[str, str] | None]:
    """`(mode, oid)` in the HEAD commit for each name, or None where HEAD does not carry it.

    One `ls-tree` per batch rather than per path: unstaging 200 files measured 0.66s when each
    name cost its own git process, and the objects being immutable is what makes unstaging safe —
    not the number of round trips used to find them.
    """
    found: dict[str, tuple[str, str]] = {}
    for i in range(0, len(names), _BATCH):
        chunk = names[i : i + _BATCH]
        out = run_git_write(repo, ["ls-tree", "-z", "--full-name", "HEAD", "--", *chunk])
        found.update(_parse_ls(out, 2, set(chunk)))
    return {n: found.get(n) for n in names}


def _install_index_entries(repo: Repo, entries: dict[str, tuple[str, str] | None]) -> None:
    """Set (or clear) index entries from object ids, never from the worktree.

    `--cacheinfo` repeats, and the path rides as an argv element — which is why this is not the
    `--index-info` stdin form. That format's records are newline-terminated, and `validate_paths`
    refuses only NUL, as it must: a name has to match a real status row, and a newline is legal in
    a Linux filename. Measured — one fed through `--index-info` dies with `fatal: malformed index
    info` and the entry is simply not staged. (A tab is fine either way, since `--index-info`
    takes everything after the first tab as the path; the newline is the case that decides it.)
    """
    removals = [n for n, e in entries.items() if e is None]
    # A submodule pointer is the one thing with no blob to name: its value is another repository's
    # HEAD, which git has to read for itself. Recorded rather than pretended.
    gitlinks = [n for n, e in entries.items() if e is not None and e[0] == "160000" and not e[1]]
    sets = [(n, e) for n, e in entries.items() if e is not None and n not in set(gitlinks)]
    for group, args in (
        (removals, ["update-index", "--force-remove", "--"]),
        (gitlinks, ["add", "--"]),
    ):
        for i in range(0, len(group), _BATCH):
            run_git_write(repo, [*args, *group[i : i + _BATCH]])
    for i in range(0, len(sets), _BATCH):
        flags: list[str] = []
        for n, e in sets[i : i + _BATCH]:
            flags += ["--cacheinfo", f"{e[0]},{e[1]},{n}"]
        run_git_write(repo, ["update-index", "--add", *flags])


def git_stage(
    path: str | None, paths: object, staged: object = True, expect: object = None
) -> dict:
    """Stage whole files, or take them back out of the index. Never by hunk.

    **This executes the repository's own ``filter.<drv>.clean`` / ``.process`` driver**, measured
    on git 2.43.0 (a repo-configured ``filter.evil.clean`` ran under ``git add -- a.txt``). It is
    not mitigated, and the reason is the same one the module docstring gives for smudge filters on
    checkout: converting worktree content into an index entry *is* the operation being requested,
    and it is exactly what the agent's own ``git add`` does in that worktree. It is recorded here
    rather than dropped from the claimed surface — the read path documents the same execution
    during status/diff, and this module must not quietly claim less than that one.
    """
    want_staged = require_bool(staged, "staged", default=True)
    repo = resolve_repo(path)
    if _has_unmerged(repo):
        raise FsError("resolve the conflict in the session before staging", status=409)
    # Staging reads from the NOT-staged side, unstaging from the staged side. Getting this
    # backwards would let a request name a path the panel never showed in that group.
    names = validate_paths(repo, paths, staged=not want_staged)
    unborn = _unborn(repo) if not want_staged else False

    def run() -> dict:
        # Inside the lock, and against what the row SHOWED. "Stage a.txt" used to mean "stage
        # whatever a.txt contains now", so an edit landing between the panel's read and this call
        # was staged without ever being displayed, under a row describing the old content.
        want = _expect_map(expect, names, "stage")
        verify_rows(repo, want, "stage")
        # Bound to OBJECTS, not to the worktree at command time.
        #
        # `git add -- <path>` re-reads the file when it runs, so "stage a.txt" meant "stage
        # whatever a.txt holds by the time git gets there" — a probe changed the bytes at that
        # boundary and the unseen version was staged under a row describing the old one. Three
        # copies of a comment here admitted the window and called it narrow.
        #
        # Hashing first and installing the resulting blob id closes it outright: what lands in
        # the index is the object that was just verified, and an edit arriving afterwards
        # changes the FILE — which is exactly what it should do, and shows up as a fresh
        # unstaged row rather than as content nobody reviewed riding into the index.
        if want_staged:
            snapshots = _snapshot_for_index(repo, names)
            verify_rows(repo, want, "stage")
            _install_index_entries(repo, snapshots)
        elif unborn:
            # Nothing to restore *from* on an unborn branch; dropping the index entry is what
            # "unstage" means there, and it leaves the file in the worktree as untracked.
            verify_rows(repo, want, "stage")
            _install_index_entries(repo, dict.fromkeys(names))
        else:
            # HEAD's objects, which are immutable — so unstaging has nothing left to race
            # against at all. `restore --staged` had to consult HEAD itself at command time.
            targets = _head_entries(repo, names)
            verify_rows(repo, want, "stage")
            _install_index_entries(repo, targets)
        return {"staged": want_staged, "paths": names, "status": _fresh_status(repo)}

    return _guarded(repo, run)


def _expect_map(expect: object, names: list[str], verb: str) -> dict[str, str]:
    """The `{path: fingerprint}` a client echoes back, validated as data — and REQUIRED.

    Optional was the hole: a caller that simply omitted `expect` got the old unbound behaviour
    back, so the safety contract held only for clients that opted into it. A guarantee any caller
    can decline is not a guarantee. It is mandatory, and its keys must be exactly the paths being
    acted on — an expectation covering two of three paths would otherwise bless the third.
    """
    if expect is None:
        raise FsError(
            f"this {verb} did not say what it expected to find, so it was refused. Refresh the "
            "panel and try again.",
            status=422,
        )
    if not isinstance(expect, dict):
        raise FsError("expect must be an object of path -> fingerprint", status=422)
    out: dict[str, str] = {}
    for k, v in expect.items():
        if not isinstance(k, str) or not isinstance(v, str) or not v:
            raise FsError("expect must map a path to a fingerprint string", status=422)
        out[k] = v
    missing = sorted(set(names) - set(out))
    extra = sorted(set(out) - set(names))
    if missing or extra:
        raise FsError(
            "the expectation does not match the paths being changed, so nothing was done "
            f"(unaccounted: {(missing + extra)[0]!r}). Refresh the panel and try again.",
            status=422,
        )
    return out


def verify_rows(repo: Repo, expect: dict[str, str], verb: str) -> dict:
    """Re-read the rows INSIDE the lock and refuse if any changed since the operator saw it.

    This is the half a lock cannot provide. The panel's lock serialises the panel against itself,
    but the session agent shares the worktree and never takes it — so "check, then run" is wide
    open to the one writer most likely to be active, and no amount of locking on our side closes
    it. Re-reading here and comparing against what the CLIENT was shown is what makes the
    operation act on the bytes that were confirmed, rather than on whatever now sits at that name.

    Returns the fresh status so the caller does not read it a second time and reopen the gap.

    `_fresh_status` rather than `git_status`, and that distinction is the whole check: the read
    path CACHES, and a cached payload is by definition the state before the thing being guarded
    against. Verifying against it would compare the operator's fingerprint with the very read the
    operator was shown and pass every time — a check that cannot fail. Measured: a plain
    `git_status` returned an unchanged fingerprint across an edit that had definitely landed.
    """
    fresh = _fresh_status(repo)
    now = {e.get("path"): e.get("fp") for e in fresh.get("entries", [])}
    for name, fp in expect.items():
        cur = now.get(name)
        if cur is None:
            raise FsError(
                f"{name!r} is no longer changed, so there is nothing to {verb} — the panel "
                "refreshed instead of acting on a stale row.",
                status=409,
            )
        if cur != fp:
            raise FsError(
                f"{name!r} changed after you confirmed it, so nothing was {verb}ed. Check the "
                "row again — the file was edited while the panel was showing the old contents.",
                status=409,
            )
    return fresh


def verify_scalar(repo: Repo, expect: object, field: str, what: str) -> dict:
    """The same rule for the whole-repository preconditions: staged set, and cleanliness.

    `_fresh_status` for the same reason as `verify_rows` — a cached read makes the comparison
    vacuous.
    """
    fresh = _fresh_status(repo)
    if not isinstance(expect, str) or not expect:
        raise FsError(f"{field} must be a fingerprint string", status=422)
    if fresh.get(field) != expect:
        raise FsError(
            f"{what} changed after the panel showed it to you, so nothing was done. Refresh and "
            "check it again.",
            status=409,
        )
    return fresh


def git_discard(path: str | None, paths: object, expect: object = None) -> dict:
    """Throw away worktree changes to tracked files. The only destructive operation here.

    Two boundaries, both deliberate:

    * **Only paths the server itself just reported as changed** are reachable — that intersection,
      not ``--``, is what keeps a tampered request from destroying more than the confirmation
      named. ``--literal-pathspecs`` then makes ``:(glob)*.txt`` an ordinary (non-existent)
      filename rather than a wildcard.
    * **An untracked file is refused, not deleted.** Discarding means putting back the content
      the index holds for that path; an untracked file has none, so "discarding" one is an
      unrecoverable delete wearing the same button. The panel will not do that, and says which
      path stopped it.

    For a path that is staged *and* modified, this restores from the index — it discards the
    unstaged edit and leaves the staged one, which is what the row the operator clicked showed.

    The replacement itself never reads a file before overwriting it; see ``_restore_from_index``
    for why that is what makes the returned pre-image complete rather than merely likely.
    """
    repo = resolve_repo(path)
    names = validate_paths(repo, paths, staged=False)
    want = _expect_map(expect, names, "discard")

    def run() -> dict:
        # BOTH checks live in here, and that placement is the fix. Reading the status outside
        # `_guarded` and acting on it inside is check-then-use against the one writer most likely
        # to be active — the session agent, which shares this worktree and never takes the panel's
        # lock. Re-reading here, and comparing against the fingerprints the operator was shown,
        # is what binds "discard a.txt" to the bytes that were confirmed rather than to whatever
        # now sits at that name.
        # The index is read on BOTH sides of the row check, because the index is mutable too.
        # `verify_rows` binds the worktree bytes the operator confirmed; it says nothing about
        # which blob the index would put BACK. A stage landing between the two would have discard
        # restore a version that was never on screen — still a real version of the file, which is
        # exactly what makes it easy to miss.
        before_index = _index_entries(repo, names)
        fresh = verify_rows(repo, want, "discard")
        entries = _index_entries(repo, names)
        if entries != before_index:
            moved = next((n for n in names if entries.get(n) != before_index.get(n)), names[0])
            raise FsError(
                f"the staged version of {moved!r} changed while the panel was checking, so "
                "nothing was discarded — a discard now would put back content you were not "
                "shown. Refresh and try again.",
                status=409,
            )
        untracked = {e["path"] for e in fresh.get("entries", []) if e.get("kind") == "untracked"}
        hit = [n for n in names if n in untracked]
        if hit:
            raise FsError(
                f"{hit[0]!r} is untracked — git has no copy to restore, so the panel will not "
                "delete it. Remove it in the session if that is what you mean.",
                status=409,
            )
        # THE REPLACEMENT, which cannot lose a byte it never saw.
        #
        # This used to hash each file and then run `git restore` over it, with a comment saying
        # the gap between the two could not be closed and could only be made survivable. Both
        # halves were wrong: an edit landing in that gap was overwritten by `restore` and was
        # absent from every returned oid, so it was neither prevented nor recoverable — and the
        # gap does close, just not by checking anything.
        #
        # `_restore_from_index` never reads a file before replacing it. It DISPLACES the current
        # bytes with `rename`, which moves whatever is at the name at that instant, and then
        # creates the replacement with `O_EXCL` — so a writer that claims the freed name in
        # between is detected and displaced in turn rather than silently overwritten. Every
        # version that ever occupied the name comes back as an object id.
        saved: dict[str, list[str]] = {}
        for n in names:
            try:
                saved[n] = _restore_from_index(repo, n, entries[n])
            except (FsError, GitError) as e:
                # A multi-path discard that fails partway has ALREADY preserved and replaced the
                # paths before this one. Letting the exception through as-is would report the
                # failure and drop those object ids on the floor — the operator would be told the
                # discard failed while some of their files had in fact been replaced, with the
                # only copies unreferenced and unnamed. The ids ride along in the message.
                done = "; ".join(f"{k}: {', '.join(v)}" for k, v in saved.items() if v)
                tail = (
                    f" Paths already discarded, recoverable with "
                    f"`git cat-file -p <id>` — {done}."
                    if done
                    else ""
                )
                raise FsError(f"{e}{tail}", status=getattr(e, "status", 409)) from None
        return {"discarded": names, "recoverable": saved, "status": _fresh_status(repo)}

    return _guarded(repo, run)


#: A commit message is data, not an argument list — but it is still bounded.
MAX_MESSAGE = 16 * 1024


def git_commit(path: str | None, message: object, expect: object = None) -> dict:
    """Commit what is staged. No amend, no force, no hook.

    Hooks are already neutralized by ``core.hooksPath`` (see :func:`_base_argv`), so this needs no
    ``--no-verify`` — and deliberately does not offer one, because a flag that exists gets used.
    The author is whatever git resolves from the operator's own identity; this module never sets
    one, so a commit from the panel is indistinguishable from a commit in the session.

    **Refused on a detached HEAD.** The commit itself would succeed, but this panel's ``switch``
    requires a clean tree — which a fresh commit produces — so the very next thing the operator can
    do would leave that commit unreachable from any branch. Creating a branch here first is one
    control away in the same tab, so the refusal names it rather than setting the trap.
    """
    repo = resolve_repo(path)
    if not isinstance(message, str) or not message.strip():
        raise FsError("a commit message is required", status=422)
    if "\x00" in message:
        raise FsError("a commit message must not contain NUL", status=422)
    if len(message) > MAX_MESSAGE:
        raise FsError(f"that commit message is too long (max {MAX_MESSAGE} bytes)", status=422)
    if _has_unmerged(repo):
        raise FsError("resolve the conflict in the session before committing", status=409)
    branch = _head_branch(repo)
    if not branch:
        raise FsError(
            "HEAD is detached — create a branch here before committing, or the commit will not "
            "be reachable from one",
            status=409,
        )

    def run() -> dict:
        # Every precondition below reads the index INSIDE the lock and, where the operator was
        # shown something, checks it still holds. `git commit` records the whole index, so a file
        # the session agent staged between the panel's read and this call used to ride along
        # unseen — and the response still reported the old count. `staged_fp` is the whole staged
        # SET for exactly that reason: binding a commit to the individual rows would not have
        # caught a file that was added to the index rather than changed in it.
        st = verify_scalar(repo, expect, "staged_fp", "the staged set")
        # `git commit` commits the INDEX; the panel only ever showed a truncated view of it. Past
        # the entry cap the control would advertise the rows it could see and then commit
        # everything staged, including files the operator was never shown — so the refusal is the
        # only honest answer. Committing just the listed paths would be worse: it looks like it
        # worked while quietly leaving the rest staged.
        if st.get("truncated"):
            raise FsError(
                "this repository has too many changes for the panel to list, so a commit here "
                "would include files it never showed you. Commit in this session's terminal "
                "instead.",
                status=409,
            )
        staged = [e for e in st.get("entries", []) if e.get("kind") == "staged"]
        if not staged:
            raise FsError("nothing is staged — stage a change first", status=409)
        # The branch this commit will extend, and the commit it is extending — resolved by NAME
        # inside the lock. `HEAD` is not used for either: it is a moving target, and reading it
        # here is what let a mid-flight switch put the commit somewhere the operator did not pick.
        branch = _head_branch(repo)
        if not branch:
            raise FsError(
                "HEAD is detached — create a branch here before committing, or the commit will "
                "not be reachable from one",
                status=409,
            )
        # An UNBORN branch has no tip, and that is the first commit in a fresh repository — a
        # perfectly ordinary thing to do from this panel. Resolving the tip unconditionally broke
        # it outright ("fatal: Needed a single revision"), which no test caught because none
        # committed into an empty repository.
        try:
            before_head = run_git_write(
                repo, ["rev-parse", "--verify", f"refs/heads/{branch}"]
            ).strip()
        except GitError:
            before_head = ""
        # THE COMMIT IS BUILT, NOT REQUESTED.
        #
        # `git commit` writes whatever the index holds when it runs, onto whatever branch HEAD
        # names when it runs. Both were read earlier, so both were racing: a stage landing in the
        # gap put an unreviewed file into the commit, and a switch in the gap put the commit on a
        # different branch while this call went on reporting the original name.
        #
        # Building the object directly removes both. `commit-tree` takes the exact tree that was
        # verified and the exact parent this branch was on; `update-ref` then moves THAT branch,
        # by name, only if it is still where it was. HEAD is a symref, so it follows on its own
        # when it points here — and when it does not, this commits to the branch the operator
        # chose rather than to wherever they wandered.
        #
        # It also cannot be made to run a gpg helper. Measured: with `commit.gpgSign=true` and
        # `gpg.program=/bin/false`, `git commit` dies trying to execute it; `commit-tree` ignores
        # the key entirely. The old `--no-gpg-sign` flag was defending against something this
        # shape simply does not do.
        want_tree = run_git_write(repo, ["write-tree"]).strip()
        head_tree = (
            run_git_write(repo, ["rev-parse", "--verify", f"{before_head}^{{tree}}"]).strip()
            if before_head
            else ""
        )
        if want_tree and want_tree == head_tree:
            raise FsError(
                "nothing is staged any more — the index matches the last commit. Refresh and "
                "look again.",
                status=409,
            )
        try:
            mine = run_git_write(
                repo,
                # No `-p` on an unborn branch: a root commit has no parent, and an empty one
                # would be a revision git cannot resolve.
                ["commit-tree", want_tree, *(["-p", before_head] if before_head else [])],
                stdin=message.encode("utf-8"),
            ).strip()
        except GitError as e:
            text = str(e)
            if "Please tell me who you are" in text or "empty ident" in text:
                raise GitError(
                    "git has no identity configured on this host, so it will not record an "
                    "author. Set user.name and user.email in the session, then commit again.",
                    status=409,
                ) from None
            raise
        try:
            run_git_write(repo, ["update-ref", f"refs/heads/{branch}", mine, before_head])
        except GitError:
            raise FsError(
                f"`{branch}` moved while the commit was being written, so nothing was recorded "
                "on it. Refresh and commit again.",
                status=409,
            ) from None
        head = mine[:7]
        return {
            "commit": head,
            "branch": branch,
            "files": len(staged),
            "status": _fresh_status(repo),
        }

    return _guarded(repo, run)


# --------------------------------------------------------------------------- push


def _push_default(repo: Repo) -> str | None:
    """``remote.pushDefault`` if the repository sets one. A name, validated like any other."""
    try:
        out = run_git_write(repo, ["config", "--get", "remote.pushDefault"]).strip()
    except GitError:
        return None  # unset exits 1 — measured; absence is not a failure
    if not out:
        return None
    try:
        return validate_ref(out, what="remote")
    except FsError:
        return None


def resolve_push(repo: Repo, branch: str, explicit: object = None) -> tuple[str, bool]:
    """Which remote a push goes to, decided **server-side**, and whether it sets upstream.

    "Refuse, don't guess" applies to the target as much as to the operation, so ``origin`` is
    never assumed: the order is the branch's own upstream, then ``remote.pushDefault``, then a
    sole remote. Two or more remotes with no upstream and no default is a **refusal naming the
    candidates**, not a coin flip — the client must come back with one of them.
    """
    remotes = list_remotes(repo)
    if not remotes:
        raise FsError("this repository has no remotes to push to", status=409)
    up = _upstream(repo, branch)
    if explicit is not None:
        name = validate_ref(explicit, what="remote")
        if name not in remotes:
            raise FsError(f"unknown remote {name!r}", status=422)
        return name, up is None
    if up:
        return up.split("/", 1)[0], False
    default = _push_default(repo)
    if default and default in remotes:
        return default, True
    if len(remotes) == 1:
        return remotes[0], True
    raise FsError(
        f"`{branch}` has no upstream and this repository has several remotes "
        f"({', '.join(remotes)}) — name the one to push to",
        status=409,
    )


def destination_digest(url: str) -> str:
    """A short, stable fingerprint of the destination a push would actually reach.

    The point is drift detection, not authentication — the client here is the authenticated
    operator, so this needs to be unforgeable by *config*, not by a person. A label like
    ``origin/master`` is not: it stays identical while ``remote.origin.pushurl`` moves underneath
    it, which is exactly the reproduced finding — the preflight displayed `origin/master`, the
    pushurl changed, and the push wrote somewhere else while the expectation still matched.
    """
    return hashlib.sha256(url.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def push_target(path: str | None, remote: object = None) -> dict:
    """The dry preflight the panel renders **before** the operator commits to a push.

    A read: it resolves and reports, and changes nothing. It answers with a *state* rather than
    raising on ambiguity, because the control has to render the refusal (and the candidate list)
    rather than collapse it into a failed request. Enforcement still lives on the POST — this is
    advisory, exactly like the admission layer it mirrors.
    """
    repo = resolve_repo(path)
    branch = _head_branch(repo)
    remotes = list_remotes(repo)
    if not branch:
        return {
            "ok": False,
            "reason": "HEAD is detached — check out a branch before pushing",
            "branch": None,
            "remote": None,
            "target": None,
            "expect": None,
            "candidates": remotes,
            "set_upstream": False,
        }
    try:
        name, will_set = resolve_push(repo, branch, remote)
    except FsError as e:
        return {
            "ok": False,
            "reason": str(e),
            "branch": branch,
            "remote": None,
            "target": None,
            "expect": None,
            "candidates": remotes,
            "set_upstream": False,
        }
    # Resolved HERE so the expectation carries the destination the operator is being shown, not
    # just its name. `target` stays the human-readable label the control renders; `expect` is what
    # the POST must send back, and it pins both.
    try:
        url = _effective_url(repo, name, push=True)
    except FsError as e:
        return {
            "ok": False,
            "reason": str(e),
            "branch": branch,
            "remote": None,
            "target": None,
            "expect": None,
            "candidates": remotes,
            "set_upstream": False,
        }
    # The COMMIT this would send, resolved at the same moment as the destination. Without it the
    # token answers "where does this go" and never "what goes there".
    head_oid = run_git_write(repo, ["rev-parse", "--verify", f"refs/heads/{branch}"]).strip()
    return {
        "ok": True,
        "reason": None,
        "branch": branch,
        "remote": name,
        "target": f"{name}/{branch}",
        "expect": f"{name}/{branch}@{destination_digest(url)}:{head_oid}",
        "candidates": remotes,
        "set_upstream": will_set,
    }


def git_push(path: str | None, remote: object = None, expect: object = None) -> dict:
    """Push the current branch to a server-resolved remote, setting upstream on a first push.

    Never ``--force``, never a refspec the client supplied: the refspec is built here from a
    branch name this module validated, so there is no force variant and no arbitrary ref update
    to reach for. ``--receive-pack`` is pinned on the **command line** for the same measured
    reason ``--upload-pack`` is on fetch — ``-c`` does not override the repository's own value.
    """
    repo = resolve_repo(path)
    branch = _head_branch(repo)
    if not branch:
        raise FsError("HEAD is detached — check out a branch before pushing", status=409)
    # The control renders a resolved destination before the operator commits to it, so the write
    # is bound to the one they SAW. Config can move between the preflight and the click (an
    # upstream change is enough), and re-resolving silently would push somewhere they were never
    # shown — the opposite of what "the resolved target is rendered first" promises.
    #
    # REQUIRED, not honoured-if-present. An optional binding binds nothing: a stale client, or a
    # caller following the body #806 originally documented, would simply omit it and get the old
    # re-resolve-and-hope path back. Absence is a refusal, and it is checked here — before
    # resolution — so an unbound push cannot even reach the config it would have raced.
    m = _EXPECT_SHAPE.match(expect) if isinstance(expect, str) else None
    if m is None:
        raise FsError(
            "a push has to name the destination the panel showed you, and this request did not. "
            "Refresh the push target and try again.",
            status=422,
        )
    name, will_set = resolve_push(repo, branch, remote)
    if m.group("label") != f"{name}/{branch}":
        raise FsError(
            f"this branch now pushes to {name}/{branch}, not {m.group('label')!r} — nothing was "
            "pushed. Check the destination and try again.",
            status=409,
        )
    refuse_insecure_tls(repo)
    # The PUSH url specifically: a repo can keep an innocent `url` and send pushes elsewhere via
    # `pushurl`. Resolved once, refused on, and then handed to git verbatim.
    url = _effective_url(repo, name, push=True)
    # The label matching is not enough on its own: `origin/master` stays true while
    # `remote.origin.pushurl` is repointed underneath it, and a real probe pushed to the new
    # destination with the expectation still satisfied. The digest is of the URL the preflight
    # resolved, so a destination that moved between the display and the click is a refusal.
    if m.group("digest") != destination_digest(url):
        raise FsError(
            f"the destination for {name}/{branch} changed after the panel showed it to you, so "
            "nothing was pushed. Refresh the push target and check where it points.",
            status=409,
        )
    refuse_url_rewrites(repo, url)
    dest = admit_destination(url, name)
    want_oid = m.group("oid")

    def run() -> dict:
        # The source, resolved INSIDE the lock and required to be the commit the preflight
        # signed. `refs/heads/<branch>` is a mutable name: an agent that commits after the
        # preflight leaves the token valid while the branch now points somewhere else, and a
        # probe pushed exactly that unseen commit.
        now_oid = run_git_write(repo, ["rev-parse", "--verify", f"refs/heads/{branch}"]).strip()
        if now_oid != want_oid:
            raise FsError(
                f"`{branch}` moved after the panel showed you what would be pushed, so nothing "
                f"was sent. It is now at {now_oid[:8]}, not {want_oid[:8]} — refresh and look "
                "again before pushing.",
                status=409,
            )
        # The tracking ref as it stands BEFORE the network call. Reading it afterwards and then
        # using that value as the expected-old side of a swap back to `want_oid` is not a
        # compare-and-swap at all — it is "whatever is there now, make it what I want", and a
        # probe that advanced the ref during the push had it rewound. The snapshot is what the
        # swap has to be against.
        tracking = f"refs/remotes/{name}/{branch}"
        try:
            tracked_before = run_git_write(repo, ["rev-parse", "--verify", tracking]).strip()
        except (GitError, FsError):
            tracked_before = ""
        args = ["push", "--receive-pack", "git-receive-pack"]
        # An explicit src:dst to a PINNED url — never `git push <remote>`, whose meaning
        # `push.default` in the repository's own config would get to reshape, and never the
        # remote name, whose URL could move after the check.
        #
        # The source is the OID, not the branch name: that is what makes the check binding rather
        # than merely close to the write. Even if the branch moves between the check above and
        # this command, git sends the commit that was verified.
        args += ["--", url, f"{want_oid}:refs/heads/{branch}"]
        # Through a scratch gitdir for the same reason as fetch: this repository's own config
        # must not get to redirect where the operator's commits are sent. `GIT_OBJECT_DIRECTORY`
        # points at the real store, so the commit being pushed is readable from there.
        with _isolated_gitdir(repo) as gitdir:
            run_git_net(
                repo,
                gitdir,
                args,
                extra_config=dest.pins,
                ssh_command=dest.ssh_command,
                allow_protocol=dest.allow_protocol,
            )
        # THE REMOTE HAS NOW CHANGED. Everything below is local bookkeeping, and every line of it
        # can fail — an injected `update-ref` failure made this raise while the bare remote had
        # already advanced, so the API reported a failure for a push that had succeeded. "It
        # failed" and "it worked but the panel did not finish tidying up" are different facts and
        # the operator acts on them differently: the first invites a retry, the second makes one
        # pointless.
        #
        # So the remote result is recorded first and never revoked, and the local settlement is
        # reported separately. Each step is idempotent — `update-ref` to a known value and
        # `config` to a fixed key both converge on a retry — so "run it again" is a safe repair
        # rather than a gamble.
        settled = True
        settle_error: str | None = None
        try:
            # Pushing to a NAME updates `refs/remotes/<name>/<branch>` as a side effect; pushing
            # to a URL cannot, because git has no remote name to attribute the result to. Restore
            # it by hand, or the panel keeps showing the branch as ahead after a successful push —
            # and `@{upstream}` cannot resolve without it either (measured: the two config keys
            # alone leave `rev-parse --abbrev-ref master@{upstream}` unresolved).
            # CAS against what the tracking ref held when this push started, so a concurrent
            # fetch that already advanced it is not rolled backwards by our bookkeeping. An
            # unconditional write here would overwrite newer state with older.
            run_git_write(repo, ["update-ref", tracking, want_oid, tracked_before])
            if will_set:
                # `--set-upstream` is not usable with a pinned URL: it would record the URL as the
                # branch's remote instead of the name. These are the two keys `-u` writes, and
                # with the tracking ref above in place they yield the same `branch@{upstream}`.
                #
                # Written only while the branch STILL has no upstream. `will_set` was decided in
                # the preflight; if something configured one in between, this would replace a
                # deliberate choice with a guess derived from which button was pressed. There is
                # no compare-and-swap for `git config`, so the check is immediately before the
                # write and the operation is idempotent either way — but unlike a mutation, the
                # loss from being wrong here is a setting, and it is one the operator can see.
                if not _configured_upstream(repo, branch):
                    run_git_write(repo, ["config", f"branch.{branch}.remote", name])
                    run_git_write(
                        repo, ["config", f"branch.{branch}.merge", f"refs/heads/{branch}"]
                    )
        except (GitError, FsError) as e:
            settled = False
            settle_error = redact(str(e))
        # The post-write status read is settlement too. It sat outside this envelope, so a status
        # failure after a SUCCESSFUL remote update escaped as a route error and hid the partial
        # success — the same misreport the envelope exists to prevent, one line further down.
        # Everything after the remote has changed belongs inside it.
        try:
            final_status = _fresh_status(repo)
        except (GitError, FsError) as e:
            settled = False
            settle_error = settle_error or redact(str(e))
            final_status = None
        return {
            "remote": name,
            "branch": branch,
            "target": f"{name}/{branch}",
            "set_upstream": will_set,
            # The remote DID advance — stated separately from whether the local bookkeeping
            # finished, because a retry is the right move for one and pointless for the other.
            "pushed": want_oid,
            "settled": settled,
            "settle_error": settle_error,
            "status": final_status,
        }

    return _guarded(repo, run)


__all__ = [
    "GIT_ALLOW_PROTOCOL",
    "LOCAL_TIMEOUT_S",
    "MAX_MESSAGE",
    "MAX_PATHS",
    "NET_TIMEOUT_S",
    "check_ref_format",
    "destination_digest",
    "git_branch_delete",
    "git_commit",
    "git_discard",
    "git_fetch",
    "git_pull",
    "git_push",
    "git_stage",
    "git_switch",
    "hooks_void",
    "list_remotes",
    "push_target",
    "refuse_insecure_tls",
    "require_bool",
    "redact",
    "refuse_local_destination",
    "admit_destination",
    "tls_pin_for",
    "reset_hooks_void_for_test",
    "resolve_push",
    "resolve_repo",
    "run_git_write",
    "validate_paths",
    "validate_ref",
]
