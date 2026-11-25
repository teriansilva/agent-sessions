"""Real-agent harness for #801 — does a delivered nudge actually SUBMIT a turn?

The defect is real: a `continue` landed in a live `claude` session on v0.18.0, the text
appeared in the prompt, and no turn was ever taken — while the ledger said `delivered`,
because the bytes did reach the PTY. `continue` is the only verb the orchestrator fires
autonomously, so a nudge that does not submit means the autonomous half does nothing while
reporting success.

**The first hypothesis was tested and refuted.** It blamed `bracketed_paste()` bundling the
trailing CR into one write. A real-agent repro disproved it: on Claude Code 2.1.223 the
bundled form submits exactly as reliably as the split form. So the mechanism is unknown, five
of the six actuable engines have never been tested at all, and the deliverable of the first
half of #801 is **evidence** — a matrix — not a patch.

This module is that instrument. It exists so the next input regression is *measured* rather
than argued, and so the traps below are never walked into again.

──────────────────────────────────────────────────────────────────────────────────────────
THE TRAPS BELOW. Every one produced a confident false negative, and every one looked exactly
like the bug under test. They are listed here, at the top, because reading them is cheaper
than rediscovering them.

1. **Inherited agent environment.** This harness usually runs *inside* an agent session.
   Passing `os.environ` through hands the child `CLAUDECODE=1` / `CLAUDE_CODE_*` and the
   engine behaves like a nested call. Use `service_env()`.

2. **No controlling terminal.** Spawning the engine directly on a PTY with
   `start_new_session=True` leaves `tcgetpgrp() == 0`: the TUI renders perfectly and answers
   no keystroke. `dtach` is what supplies the controlling terminal in production — it is not
   incidental packaging. Launch through the production argv (`ptybridge.launch_argv` wrapping
   the provider's own `new_launch_argv`), never a bare binary.

3. **A launch flag that makes the engine exit.** `--dangerously-skip-permissions` in a
   directory claude has not been trusted for exits immediately, leaving `dtach` holding a dead
   screen that ignores every key (`ps` shows a master with no child). Related, and separately
   measured: a first-run trust dialog hangs the session invisibly — the process is alive and
   the transcript never appears. **Alive is not started**, which is why `wait_ready()` gates on
   the transcript store rather than on the process.

4. **The wrong predicate.** "Did the assistant reply" conflates *submission* with *model
   health*. A `model: "default"` in this host's `~/.claude/settings.json` made every payload
   look unsubmitted when the turns had in fact committed. **Measure submission**: a committed
   user turn in the engine's own transcript store, which is exactly what `user_turns()` reads.

5. **Reading the screen instead of the store.** Several engines run in the alternate buffer,
   so a scrape of the visible grid cannot see history and "the prompt cleared" is not proof of
   anything. The store is the only durable evidence. An engine with no adapter is recorded
   `untested`, never assumed to behave like claude.

6. **Measuring the wrong store on a mint-own-id engine.** `codex` / `opencode` / `kimi` /
   `antigravity` launch under a `new-<uuid>` placeholder and mint their REAL id themselves; the
   snapshot-and-reconcile that aliases the two lives on the viewer path only (#739). A harness
   that reads `user_turns(engine, <placeholder>)` therefore reads a store that will never
   exist, sees zero turns, and reports **"did not submit"** for an engine that submitted
   perfectly. That is this harness's own version of trap 4 and it would have shipped as a
   four-engine false negative, so those engines are refused with a reason rather than measured.
   Unblocking them is #739's headless reconcile, not a change here.

7. **Nobody draining the PTY.** A pty buffer is finite. With no reader, a talkative agent
   fills it and blocks on write — the session stops making progress and every payload after
   that looks unsubmitted. The harness runs a drain thread for exactly this reason; it is not
   there to collect output for its own sake.

8. **Leaving the quiet gate a no-op.** `send_input(require_quiet=True)` waits on
   `scrollback.get_last_output_at(key)`, which returns `None` when nothing has fed the
   scrollback store — and `_wait_quiet` treats `None` as "quiet, go ahead". So a harness that
   drains the PTY *without* feeding `scrollback` silently disables the very gate the
   mid-render hypothesis is about, and would have reported the gate as irrelevant no matter
   what it does. The drain thread therefore feeds `scrollback._buffer_append`, the same call
   the web terminal makes, so the gate behaves exactly as it does in production.
──────────────────────────────────────────────────────────────────────────────────────────

Opt-in, like `e2e_install`: nothing here runs unless the engine binary is present AND
`AGENT_SESSIONS_REAL_AGENT=1` is set, because these tests spend real tokens and take real
seconds. CI never runs them.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import termios
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent_sessions import (
    ptybridge,
    runtime_cleanup,
    scopedspawn,
    scrollback,
    session_input,
    transcript,
)
from agent_sessions.engines import registry

#: The env var that arms the whole module. Absent ⇒ every real-agent test skips.
OPT_IN_ENV = "AGENT_SESSIONS_REAL_AGENT"

#: Bounded raw-output copy kept for prompt detection. Diagnostics only.
_SEEN_MAX = 256 * 1024

#: Geometry given to the pty BEFORE the engine starts. A fresh `openpty()` is **0×0**, and
#: production floors both axes for exactly this reason — a 0×0 controlling tty makes
#: Ink-style agents render into nothing (`webterm._set_winsize`, #292/#293). An engine that
#: cannot paint then looks like an engine that cannot be nudged, which is the same false
#: negative in a new costume. Ordinary terminal size, so the TUI behaves as it does for a user.
_PTY_ROWS, _PTY_COLS = 32, 120

#: Substrings identifying a first-run trust dialog, matched against output that has had its
#: escape sequences AND all whitespace removed — hence no spaces here.
#:
#: The normalisation is not defensive coding, it is required: a TUI draws by moving the
#: cursor rather than emitting spaces, so "trust this folder" never appears contiguously in
#: the raw stream and a naive substring match silently never fires. Measured, after a first
#: attempt matched nothing and looked exactly like "the engine simply never started".
_TRUST_MARKERS = (
    "trustthisfolder",
    "doyoutrustthefiles",
    "quicksafetycheck",
    # gemini's wording, MEASURED from a real dialog rather than guessed. Its absence produced a
    # textbook false negative: `wait_ready` returned True with the trust modal still up, every
    # nudge went into a three-option dialog instead of a prompt, and the matrix reported gemini
    # as "delivered, no turn committed" — indistinguishable from the defect under test, and
    # published as one before the screen was looked at.
    "trustingafolderallows",
    "trustparentfolder",
    "trustfolder",
)

#: Glyphs a TUI uses to mark the currently selected option.
_CURSOR_MARKS = ("\u276f", "\u25b8", "\u203a", ">")

#: Frame and bullet characters a dialog draws around its options. Stripped before parsing so a
#: boxed menu (`│●1.Trustfolder(…)│`) is read as the option it is.
_BOX = "\u25cf\u2502\u2503|*-\u2022\u00b7"
_DECOR = "".join(_CURSOR_MARKS) + _BOX

#: Markers of an engine that launched but is NOT authenticated. Matched on the same
#: whitespace-stripped screen as the trust markers.
#:
#: `registry.present_providers()` answers *is the binary installed*, never *is it logged
#: in* — and `service_env()` deliberately strips provider credential families. So a
#: logged-out CLI passes availability, fails readiness, and would be reported as a RED
#: matrix cell: a confident false negative about submission caused by a missing login
#: (review on #858). #801 asks for a skip when the binary or its auth is absent; this is
#: the auth half.
#: Terminal capability queries some TUIs send at startup and then WAIT for. A browser's
#: xterm.js answers these in production; a bare pty does not, so an unanswered engine sits
#: alive and silent forever — which `wait_ready`'s first-paint+quiet reads as ready, and the
#: matrix then reads as 'delivered but never submitted'. Measured on gemini, which emits
#: nothing but `ESC[>q` until answered. Replies are the minimal plausible ones; the goal is
#: to unblock the engine, not to emulate a terminal.
_QUERY_REPLIES: tuple[tuple[bytes, bytes], ...] = (
    (b"\x1b[>q", b"\x1bP>|xterm(370)\x1b\\"),  # XTVERSION
    (b"\x1b[>0q", b"\x1bP>|xterm(370)\x1b\\"),
    (b"\x1b[c", b"\x1b[?62;1;2;6;9;15;22c"),  # Primary DA
    (b"\x1b[0c", b"\x1b[?62;1;2;6;9;15;22c"),
    (b"\x1b[>c", b"\x1b[>0;370;0c"),  # Secondary DA
    (b"\x1b[>0c", b"\x1b[>0;370;0c"),
    (b"\x1b[5n", b"\x1b[0n"),  # DSR: terminal OK
    (b"\x1b[6n", b"\x1b[24;80R"),  # CPR
    (b"\x1b[?u", b"\x1b[?0u"),  # kitty keyboard protocol
)

#: CSI / OSC / charset-select sequences, stripped before matching.
_ESC_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][B0]")

#: Environment keys that mark this process as living inside an agent. Trap 1: inherited, they
#: change how the child engine behaves. Prefixes, because the families are open-ended.
_AGENT_ENV_PREFIXES = (
    "CLAUDE_CODE_",
    "CLAUDECODE",
    "ANTHROPIC_",
    "OPENCODE",
    "CODEX_",
    "GEMINI_",
    "KIMI_",
    "AGY_",
    "ANTIGRAVITY_",
)
#: Keys a *service* legitimately has. Everything else is dropped rather than enumerated, so a
#: newly-invented agent marker is excluded by default instead of leaking until someone notices.
_SERVICE_ENV_KEYS = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "SHELL",
    "USER",
    "LOGNAME",
    "XDG_RUNTIME_DIR",
)


def _pending_prefix(tail: bytes) -> bytes:
    """The longest suffix of ``tail`` that is a PROPER prefix of some supported query.

    Anything shorter loses a query straddling a read boundary; anything longer (notably
    "keep the last max-query-length bytes") retains complete matched queries and replays them.
    """
    limit = max(len(q) for q, _ in _QUERY_REPLIES) - 1
    tail = tail[-limit:] if limit > 0 else b""
    for i in range(len(tail)):
        cand = tail[i:]
        if any(q.startswith(cand) and len(cand) < len(q) for q, _ in _QUERY_REPLIES):
            return cand
    return b""


def service_env(**extra: str) -> dict[str, str]:
    """A service-like environment — trap 1.

    Allowlist, not denylist. A denylist has to be updated every time an engine invents a new
    marker, and the failure mode of forgetting is silent: the child quietly behaves like a
    nested agent and the run looks like the bug under test.
    """
    env = {k: os.environ[k] for k in _SERVICE_ENV_KEYS if k in os.environ}
    env["TERM"] = os.environ.get("TERM", "xterm-256color")
    env["COLORTERM"] = os.environ.get("COLORTERM", "truecolor")
    for k in list(env):
        if k.startswith(_AGENT_ENV_PREFIXES):  # belt and braces over the allowlist
            del env[k]
    env.update(extra)
    return env


def engine_available(engine: str) -> tuple[bool, str]:
    """``(ok, reason)`` — is this engine testable on this host right now?

    Presence is asked of the provider registry, never guessed from a binary name, so the
    harness and the app agree on what "installed" means.
    """
    if os.environ.get(OPT_IN_ENV) != "1":
        return False, f"{OPT_IN_ENV}=1 not set (real-agent tests are opt-in: they spend tokens)"
    prov = next((p for p in registry.present_providers() if p.engine_id == engine), None)
    if prov is None:
        return False, f"engine {engine!r} is not installed on this host"
    if not registry.supports_orchestrator_input(prov):
        return False, f"engine {engine!r} is not actuable (supports_orchestrator_input is False)"
    if getattr(prov, "new_session_reconciles", False):
        # Trap 6. Refusing beats measuring: a placeholder id resolves to a store that never
        # exists, which reads as "no turn committed" — the exact false negative this whole
        # issue exists to stop being convinced by.
        return False, (
            f"engine {engine!r} mints its own session id and reconciles on the viewer path, so "
            "the harness would read a store that never exists and report a false negative. "
            "Blocked on #739's headless reconcile; recorded as UNTESTED, not as passing."
        )
    if not (scopedspawn.enabled() and scopedspawn.available()):
        # Containment first: without a transient scope the only fallback is descendant
        # inventory, and inventory is racy by construction — a child forked after the final
        # scan and detached with `setsid` is both unrememberable and undiscoverable (review on
        # #858). Refusing to launch beats launching an authenticated, token-spending agent that
        # cannot be guaranteed reapable.
        return False, (
            "no usable transient scope (systemd-run --user unavailable, or session scopes "
            "disabled by config), so a real agent could not be guaranteed reapable — "
            "refusing to launch one; recorded as UNTESTED"
        )
    if transcript.adapter_for(engine) is None:
        # Trap 5. Recorded as untestable rather than silently downgraded to a screen scrape,
        # because a screen scrape cannot see a committed turn.
        return False, f"engine {engine!r} registers no transcript adapter — submission unmeasurable"
    return True, "ok"


def user_turns(engine: str, native_id: str, home: Path | None = None) -> int:
    """Committed **user** turns in the engine's own store — trap 4, the whole predicate.

    Not "did the assistant answer": a broken model setting makes every payload look
    unsubmitted while the turns have in fact committed. Not the screen: several engines run in
    the alternate buffer, so the visible grid cannot see history (trap 5).
    """
    adapter = transcript.adapter_for(engine)
    if adapter is None:
        raise RuntimeError(f"no transcript adapter for {engine!r} — submission is unmeasurable")
    try:
        turns = adapter(native_id, home or Path.home())
    except (OSError, ValueError):
        return 0  # store not written yet — zero committed turns is the honest answer
    return sum(1 for t in turns if t.role == "user")


@dataclass
class RealSession:
    """A live engine launched exactly the way the app launches one.

    Not a convenience wrapper around the binary: the argv is the provider's own
    `new_launch_argv` wrapped in `ptybridge.launch_argv`, so what is under test is the
    production launch path (trap 2). The PTY master this holds is the same handle the web
    terminal writes through, registered in the same registry `send_input` consults.
    """

    engine: str
    cwd: str
    native_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    bypass: bool = False
    #: TESTS ONLY: launch this argv instead of the provider's. Exists so the teardown
    #: contract — which is about reaping a dtach MASTER, not about any particular engine —
    #: can be regression-tested against a real dtach with `sleep`, costing no tokens.
    argv_override: list[str] | None = None

    _master: int | None = field(default=None, init=False, repr=False)
    _slave: int | None = field(default=None, init=False, repr=False)
    _proc: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _token: int | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _drain: threading.Thread | None = field(default=None, init=False, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _seen: bytearray = field(default_factory=bytearray, init=False, repr=False)
    #: Total bytes ever read. `len(_seen)` CANNOT serve: `_seen` is a capped diagnostics
    #: buffer, so once it reaches `_SEEN_MAX` every append is trimmed straight back and the
    #: 'counter' stops advancing while output is still flowing (review on #858).
    _out_bytes: int = field(default=0, init=False, repr=False)
    _trusted: bool = field(default=False, init=False, repr=False)
    #: Outcome of the master reap on exit — asserted by the teardown regression.
    reap_outcome: str = field(default="", init=False, repr=False)
    #: Transient systemd scope holding the whole session subtree, or None when scopes are
    #: unavailable (then the id/pgid/descendant sweeps below are the fallback).
    scope_unit: str | None = field(default=None, init=False, repr=False)
    scope_stopped: bool = field(default=False, init=False, repr=False)
    kill_error: str | None = field(default=None, init=False, repr=False)
    #: PIDs the straggler sweep had to kill after the master reap (gemini leaves some).
    straggler_pids: list[int] = field(default_factory=list, init=False, repr=False)
    #: Wall-clock when the last write was ISSUED, and the gap since the session's previous
    #: output at that instant. Measured before the write on purpose — anything measured
    #: after is satisfied by the write's own echo and proves nothing about overlap.
    last_write_at: float = field(default=0.0, init=False, repr=False)
    output_gap_at_write: float | None = field(default=None, init=False, repr=False)
    #: Every pid ever seen descending from the launch, sampled while the runtime is ALIVE.
    #: A child that escapes its process group carrying no session id is undiscoverable once
    #: the master is gone, so it has to be remembered before then.
    _seen_descendants: dict[int, int] = field(default_factory=dict, init=False, repr=False)
    _last_descendant_scan: float = field(default=0.0, init=False, repr=False)
    _watch: threading.Thread | None = field(default=None, init=False, repr=False)
    _watch_stop: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _qbuf: bytearray = field(default_factory=bytearray, init=False, repr=False)

    @property
    def key(self) -> str:
        return f"{self.engine}:{self.native_id}"

    def __enter__(self) -> RealSession:
        # Decide containment FIRST — before any argv is built. This does not depend on the
        # launch command, and putting it after argv construction made it unreachable whenever
        # the engine binary is absent (`ptybridge.launch_argv` rejects a bare name first), which
        # is exactly the case on CI. A guard that only fires where the engine happens to be
        # installed is not a guard.
        if self.argv_override is None and not (scopedspawn.enabled() and scopedspawn.available()):
            raise RuntimeError(
                "refusing to launch a real agent without a transient scope: it could not be "
                "guaranteed reapable, and the inventory fallback is racy by construction"
            )
        if self.argv_override is not None:
            argv = list(self.argv_override)
        else:
            prov = next(p for p in registry.present_providers() if p.engine_id == self.engine)
            # Trap 3: `bypass` defaults False. `--dangerously-skip-permissions` in a directory
            # the engine has not been trusted for exits on the spot, and dtach is then holding
            # a dead screen that ignores every key — indistinguishable, from the outside,
            # from the defect.
            argv = prov.new_launch_argv(self.native_id, cwd=self.cwd, bypass=self.bypass)
        create = ptybridge.launch_argv(
            engine=self.engine, session_id=self.native_id, launch_argv=argv
        )
        # Launch inside a transient systemd scope — the same containment production gives every
        # session. This is what finally closes the escaped-child hole, and it closes it by
        # construction rather than by inventory: a cgroup holds EVERY descendant regardless of
        # process group, of whether the argv carries the session id, and — the part no snapshot
        # can do — regardless of when it was forked. A final scan cannot stop the master forking
        # after it (review on #858), so stop taking inventory and put a boundary around it.
        create, self.scope_unit = scopedspawn.wrap(
            create, engine=self.engine, session_id=self.native_id
        )
        # Fail closed at the launch, not only at availability. `wrap()` honours BOTH `enabled()`
        # and `available()`, so a config-disabled scope (AGENT_SESSIONS_SESSION_SCOPES=0) returns
        # the bare argv with no unit — and an availability check alone would wave that through
        # onto the racy inventory fallback (review on #858). A real engine is never launched
        # without containment; the `sleep` stand-ins used by the harness's own tests are exempt
        # because they spend nothing and are hermetic.
        if self.scope_unit is None and self.argv_override is None:
            raise RuntimeError(
                "refusing to launch a real agent without a transient scope: it could not be "
                "guaranteed reapable, and the inventory fallback is racy by construction"
            )
        self._master, self._slave = pty.openpty()
        # BEFORE the spawn, so the engine inherits a usable size on its very first paint.
        fcntl.ioctl(
            self._slave, termios.TIOCSWINSZ, struct.pack("HHHH", _PTY_ROWS, _PTY_COLS, 0, 0)
        )
        # dtach gets the slave as its stdio, exactly as `webterm` does. dtach then makes a
        # controlling terminal for the engine itself — the thing a bare spawn cannot provide.
        self._proc = subprocess.Popen(  # noqa: S603 - literal argv, no shell (repo guarantee)
            create,
            stdin=self._slave,
            stdout=self._slave,
            stderr=self._slave,
            cwd=self.cwd,
            env=service_env(),
            close_fds=True,
        )
        # Everything past the spawn is fail-closed. `__exit__` only runs if `__enter__`
        # RETURNS, so an exception in `register_writer` or `Thread.start()` would leave the
        # master and a live authenticated agent running with nothing scheduled to reap them —
        # the same leak as before, reachable through a different door (review on #858).
        try:
            self._token = session_input.register_writer(
                self.key, self._master, self._lock, "harness"
            )
            self._drain = threading.Thread(target=self._drain_loop, daemon=True)
            self._drain.start()
            # Independent of PTY output: a child that starts AFTER the last byte would never be
            # sampled by an output-driven scan, and teardown stops the reader before killing
            # anything (review on #858).
            self._watch = threading.Thread(target=self._descendant_loop, daemon=True)
            self._watch.start()
        except BaseException:
            self._teardown()
            raise
        return self

    def __exit__(self, *exc) -> None:
        self._teardown()

    def _teardown(self) -> None:
        """Reap the MASTER, not just the client — and tolerate partial initialisation.

        `dtach -c` runs in the foreground as a **client**. Terminating it only *detaches*: this
        repo guarantees the master and its agent survive the spawner's death, and pins that
        with `test_dtach_master_survives_spawner_death`. So killing `_proc` and unlinking the
        socket leaves a live agent and removes the only handle on it — one abandoned
        token-burning agent per cell.

        Every step is individually guarded because this also runs as `__enter__`'s rollback,
        where any subset of the fields may be unset.
        """
        self._stop.set()
        if self._drain is not None:
            # `join()` on a thread that was never started raises — and this path runs as
            # `__enter__`'s rollback precisely when `start()` was what failed. An exception
            # here would abort the rest of the teardown and leak the agent, which is the very
            # thing the rollback exists to prevent. Found by the injected-failure regression.
            with contextlib.suppress(RuntimeError):
                self._drain.join(timeout=3)
        # A FINAL scan while the root is still alive. The watcher samples every 0.5s, so a
        # child that appears between its last scan and teardown would otherwise never be
        # remembered — and once the client/root is gone the tree walk has no root to start
        # from, so it can never be discovered afterwards either. This is the last moment the
        # descendant tree is knowable (review on #858).
        with contextlib.suppress(Exception):
            self._sample_descendants(force=True)
        if self._token is not None:
            with contextlib.suppress(Exception):
                session_input.unregister_writer(self.key, self._token)
        # Detach the foreground client first; this alone does NOT stop the agent.
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        # Now actually reap the master + agent process group, through the production path.
        try:
            self.reap_outcome = asyncio.run(
                runtime_cleanup.cleanup_runtime(self.engine, self.native_id)
            )
        except Exception as exc_:  # teardown must not mask a test failure
            self.reap_outcome = f"error: {exc_!r}"
        # THE containment boundary: stopping the scope kills every process in the cgroup at
        # once — anything the session forked, whenever it forked it. The sweeps below stay as
        # the fallback for hosts without a usable `systemd-run --user`.
        self.scope_stopped = self._stop_scope()
        if self.scope_unit and not self.scope_stopped:
            # Escalate inside the boundary before giving up: SIGKILL the whole cgroup, then
            # re-verify. `stop` can fail or time out while the scope is still holding live
            # processes, and silently continuing would fall back on the inventory sweep that
            # cannot see an id-less child forked after the final snapshot (review on #858).
            self.scope_stopped = self._kill_scope()
        # Belt and braces, and NOT redundant — measured. `cleanup_runtime` kills the master's
        # process GROUP, which is enough for claude but not for gemini: its node child escapes
        # the group and survives. (Beyond this harness: production archive uses the same call,
        # so archiving a gemini session leaves its agent running too.) The session's own id is
        # in the engine's argv, which makes this sweep precise rather than a pattern kill.
        with contextlib.suppress(Exception):
            self._reap_stragglers()
        # Only now: the watcher must keep sampling across the reap, since a child can appear
        # while the master is being torn down.
        self._watch_stop.set()
        if self._watch is not None:
            with contextlib.suppress(RuntimeError):
                self._watch.join(timeout=3)
        for fd in (self._master, self._slave):
            if fd is not None:
                with contextlib.suppress(OSError):
                    os.close(fd)
        self._master = self._slave = None
        # Containment is part of the teardown CONTRACT, not a status field. If the scope could
        # not be verified gone even after SIGKILL, a real authenticated agent may still be
        # running — and the inventory sweeps cannot see an id-less child forked after the final
        # snapshot. Say so loudly rather than returning normally with a False flag nobody reads
        # (review on #858).
        if self.scope_unit and not self.scope_stopped:
            raise RuntimeError(
                f"containment NOT verified: scope {self.scope_unit} is still active after stop "
                "and SIGKILL — a real agent may still be running. Failing the cell rather "
                "than reporting a clean teardown."
                + (f" Last kill error: {self.kill_error}" if self.kill_error else "")
            )

    def _stop_scope(self, timeout: float = 30.0) -> bool:
        """Stop the transient scope and **verify it is actually gone**.

        Returning success on a failed `systemctl stop` would silently drop the session back
        onto the inventory path — which is racy by construction and is exactly what the scope
        exists to replace (review on #858). So the exit status is checked, and the unit is then
        confirmed inactive before this reports success.
        """
        if not self.scope_unit:
            return False
        systemctl = shutil.which("systemctl")
        if not systemctl:
            return False
        try:
            out = subprocess.run(  # noqa: S603 - literal argv, no shell
                [systemctl, "--user", "stop", self.scope_unit],
                capture_output=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if out.returncode != 0:
            return False
        # `stop` returning 0 is not proof the cgroup is empty; ask for the unit's state.
        return self._scope_inactive()

    def _scope_inactive(self, timeout: float = 10.0) -> bool:
        """Is the scope definitively in a terminal state? Errors and transitions are NOT.

        `is-active` was the wrong probe: it returns non-zero for perfectly normal states, so
        the status could not be checked — and treating "stdout is not exactly `active`" as
        proof of death accepted both a D-Bus/manager error (empty stdout) and the transitional
        `deactivating`, in which processes are still running (review on #858).

        `show --property=ActiveState` is queried instead: it exits 0 even for a unit that no
        longer exists (reporting `inactive`), so a non-zero exit genuinely means the query
        failed and nothing can be concluded. Only the recognised terminal states count.
        """
        systemctl = shutil.which("systemctl")
        if not systemctl or not self.scope_unit:
            return False
        terminal = {"inactive", "failed"}
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                st = subprocess.run(  # noqa: S603 - literal argv, no shell
                    [
                        systemctl,
                        "--user",
                        "show",
                        self.scope_unit,
                        "--property=ActiveState",
                        "--value",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            if st.returncode != 0:
                return False  # the query itself failed — prove nothing, claim nothing
            state = st.stdout.strip()
            if state in terminal:
                return True
            # `active`, `activating`, `deactivating` all still hold processes; keep waiting.
            time.sleep(0.25)
        return False

    def _kill_scope(self, timeout: float = 30.0) -> bool:
        """SIGKILL every process in the scope, then re-verify it is gone."""
        systemctl = shutil.which("systemctl")
        if not systemctl or not self.scope_unit:
            return False
        try:
            out = subprocess.run(  # noqa: S603 - literal argv, no shell
                [systemctl, "--user", "kill", "--signal=SIGKILL", self.scope_unit],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            self.kill_error = "systemctl kill could not be run"
            return False
        # The kill's status is consulted, but it cannot be the verdict on its own — and
        # MEASURING that mattered: on the ordinary success path `stop` has already reaped the
        # scope, so `kill` exits 1 with "Unit ... not loaded". Refusing on a non-zero status
        # therefore reported a *correctly* reaped cgroup as uncontained and broke three real
        # teardown tests. What is provable is the unit's state, so that stays authoritative:
        # a failed kill over a verifiably terminal unit is success (it was already dead), and
        # a failed kill whose state cannot be queried is a refusal, because a broken manager
        # fails BOTH commands and the fail-closed branch below is the one that then runs.
        if out.returncode != 0:
            self.kill_error = (out.stderr or "").strip() or f"exit {out.returncode}"
        return self._scope_inactive()

    def master_alive(self) -> bool:
        """Is a dtach master still serving this session? Used to prove teardown worked."""
        return ptybridge.session_exists(self.engine, self.native_id)

    @staticmethod
    def _starttime(pid: int) -> int | None:
        """Field 22 of /proc/<pid>/stat — the process's start time in clock ticks.

        A bare PID is **not a durable identity**: Linux reuses numbers, and a cell can run for
        three minutes. Signalling a remembered number without checking that the process is
        still the same one risks killing unrelated work — and then its whole process group
        (review on #858).
        """
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as fh:
                return int(fh.read().rsplit(")", 1)[-1].split()[19])
        except (OSError, IndexError, ValueError):
            return None

    def _reap_stragglers(self, grace: float = 3.0) -> list[int]:
        """SIGTERM→SIGKILL anything still carrying this session's id, **and its process group**.

        Matching on the session id alone is not enough. The id appears in the engine's argv and
        in the dtach socket path, so it finds the master and the agent — but not a grandchild
        that carries neither (gemini's node child; the `sleep` stand-in in the tests). Those are
        in the master's process *group*, so the group is what gets signalled.

        The group is resolved per victim rather than assumed, and this process's own group is
        never signalled — a harness that kills its own test runner proves nothing.
        """
        mine = os.getpgid(0)
        # The union is the point: the remembered half covers a child that escaped its group and
        # carries no session id (undiscoverable once the master is gone); the id-matched half
        # covers anything started since the last sample.
        victims: list[int] = [
            pid
            for pid, born in self._seen_descendants.items()
            if pid != os.getpid() and self._starttime(pid) == born
        ]
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/cmdline", encoding="utf-8", errors="replace") as fh:
                    cmd = fh.read()
            except OSError:
                continue
            pid_i = int(entry)
            if self.native_id in cmd and pid_i != os.getpid() and pid_i not in victims:
                victims.append(pid_i)

        groups: set[int] = set()
        for pid in victims:
            with contextlib.suppress(OSError):
                pgid = os.getpgid(pid)
                if pgid != mine:
                    groups.add(pgid)

        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pgid in groups:
                with contextlib.suppress(OSError):
                    os.killpg(pgid, sig)
            for pid in victims:
                with contextlib.suppress(OSError):
                    os.kill(pid, sig)
            if sig is signal.SIGTERM:
                deadline = time.time() + grace
                while time.time() < deadline and any(Path(f"/proc/{p}").exists() for p in victims):
                    time.sleep(0.1)
                if not any(Path(f"/proc/{p}").exists() for p in victims):
                    break
        self.straggler_pids = victims
        return victims

    def _drain_loop(self) -> None:
        """Read the PTY and feed `scrollback`, exactly as the web terminal does — traps 7 & 8.

        Two jobs, and neither is optional:

        * **Drain**, or the pty buffer fills and the agent blocks on write. Everything after
          that point looks unsubmitted, which is indistinguishable from the defect.
        * **Feed `scrollback._buffer_append`**, or `send_input(require_quiet=True)` finds no
          recorded output, treats the session as quiet, and writes immediately — silently
          disabling the gate the mid-render hypothesis is about.
        """
        while not self._stop.is_set():
            try:
                r, _, _ = select.select([self._master], [], [], 0.2)
            except (OSError, ValueError):
                return
            if not r:
                continue
            try:
                data = os.read(self._master, 65536)
            except OSError:
                return
            if not data:
                return
            scrollback._buffer_append(self.key, data)
            self._answer_capability_queries(data)
            # A bounded copy for prompt detection only — never the submission predicate, which
            # is always the transcript store (traps 4 and 5).
            self._out_bytes += len(data)
            self._seen.extend(data)
            if len(self._seen) > _SEEN_MAX:
                del self._seen[: len(self._seen) - _SEEN_MAX]

    def screen_text(self) -> str:
        """Recent raw output, escapes left in. Diagnostics only — never evidence."""
        return bytes(self._seen).decode("utf-8", "replace")

    def _answer_capability_queries(self, data: bytes) -> None:
        """Reply to the capability queries a TUI blocks on — exactly once each — and never twice.

        Two failure modes, opposite directions, both fatal to the matrix:

        * **Missing one.** PTY read boundaries are arbitrary, so `ESC[` can end one read and
          `>q` begin the next. Matching per chunk misses it, the engine waits forever, and the
          harness reports "delivered but never submitted".
        * **Replaying one.** Retaining a whole matched query in the rolling buffer makes it
          match *again* on the next ordinary output chunk, writing an unsolicited second
          response into the agent's input stream. A probe with the longest query plus one noise
          byte produced `reply_count=2` (review on #858). Perturbing the TUI invalidates the
          matrix just as surely as blocking it does.

        So matched bytes are **consumed**, and what is retained is only the longest suffix that
        is a *proper prefix* of some supported query — the exact state where more bytes could
        still complete a match, and nothing more.
        """
        self._qbuf.extend(data)
        buf = bytes(self._qbuf)
        consumed_to = 0
        while True:
            best = None
            for query, reply in _QUERY_REPLIES:
                i = buf.find(query, consumed_to)
                if i != -1 and (best is None or i < best[0]):
                    best = (i, query, reply)
            if best is None:
                break
            i, query, reply = best
            try:
                os.write(self._master, reply)
            except OSError:
                return
            consumed_to = i + len(query)  # consume it, so it can never match again
        rest = buf[consumed_to:]
        self._qbuf[:] = _pending_prefix(rest)

    def _normalised_screen(self) -> str:
        """Recent output with escapes and whitespace removed, lowercased — for prompt matching.

        See `_TRUST_MARKERS`: a TUI positions the cursor instead of emitting spaces, so raw
        substring matching does not work on drawn text.
        """
        return "".join(_ESC_RE.sub("", self.screen_text()).split()).lower()

    def _answer_trust_prompt(self) -> bool:
        """Accept the engine's first-run "do you trust this folder?" dialog — and VERIFY it.

        Trap 3's other half, and the reason `wait_ready` cannot gate on the process: this
        dialog leaves the engine **alive with no transcript, forever**.

        **A bare CR is not an answer — it is a bet on the default selection.** Claude 2.1.247
        renders `❯ No, exit` *first*, so a bare CR chooses **No** and the engine exits; the
        matrix then reports the engine dropping the nudge, when in fact the harness declined
        the folder on its behalf. Measured after claude passed on an already-trusted directory
        and failed on every fresh one — a false negative created entirely by this method.

        So the affirmative option is located from POSITIVE evidence — the drawn option list and
        the cursor's position within it — and selected in one deterministic move. Trialing
        candidate keystrokes was the earlier design and it is unsound in both directions: a
        speculative Down selects "No" on any dialog that lists Yes first, which exits the
        engine, and the liveness check then (correctly) returns before the Up fallback is ever
        reachable. A destructive first guess cannot be rescued by a later attempt (review on
        #858). If the layout cannot be read, nothing is sent at all.

        Answering is safe here specifically because the directory is one the test created
        moments earlier — it is not a judgement about arbitrary code.
        """
        if self._trusted:
            return False
        if not self._screen_has_trust_marker():
            return False
        keys = self._trust_selection_keys()
        if keys is None:
            return False  # layout unreadable — never guess with a destructive keystroke
        self._seen.clear()  # judge on FRESH output, never on the stale dialog text
        before = self._out_bytes
        try:
            os.write(self._master, keys)
        except OSError:
            return False
        deadline = time.time() + 6
        while time.time() < deadline:
            time.sleep(0.25)
            # Liveness is checked INSIDE the loop: a wrong answer makes the engine exit, and an
            # empty buffer would otherwise read as "the dialog went away".
            if self._proc is not None and self._proc.poll() is not None:
                return False
            # Absence of the marker is only meaningful once NEW output has arrived — `_seen`
            # was just cleared, so "no marker" is trivially true until the engine repaints.
            if self._out_bytes > before and not self._screen_has_trust_marker():
                self._trusted = True
                return True
        return False

    def _numbered_trust_key(self) -> bytes | None:
        """A NUMBERED trust dialog answered by its digit, or None if this is not one.

        Gemini draws (whitespace collapsed):

            ●1.Trustfolder(<dir>)
             2.Trustparentfolder(<parent>)
             3.Don'ttrust

        The cursor-arithmetic path below cannot read this, and correctly refused to guess — so
        every gemini session sat on the modal and every nudge went into it, which the matrix
        published as an engine defect. Two reasons this needs its own branch rather than an
        extension of that one:

        * its selected-option glyph is `●`, not one of `_CURSOR_MARKS`;
        * it has THREE options, two of which begin "trust", so the frame-splitting rule there
          ("a repeated kind means a new frame") would cut the dialog apart.

        A numbered menu does not need any of that. It wants its digit — the same reasoning
        `actuator.render` uses for `choose`: "a numbered prompt wants a keypress, and the
        narrower the payload the smaller the blast radius."

        **Narrowest affirmative only.** `Trust parent folder` is also a "trust" option and is
        strictly broader — it would trust `/tmp` for every future run. If the exact-folder
        option is not found, this returns None rather than reaching for the parent.
        """
        numbered: list[tuple[int, str]] = []
        for raw in _ESC_RE.sub("", self.screen_text()).splitlines():
            # Strip the frame as well as the cursor: gemini draws its dialog inside a box, so a
            # collapsed line reads `│●1.Trustfolder(…)│` and the digit is not at the start.
            # Anchoring after the decoration keeps this a MATCH rather than a search — a search
            # would happily find a numbered-looking run inside prose.
            line = "".join(raw.split()).lower().lstrip(_DECOR)
            m = re.match(r"^(\d+)[.)](.+)$", line)
            if m:
                numbered.append((int(m.group(1)), m.group(2).rstrip(_DECOR)))
        if not numbered:
            return None
        # Last frame only: the dialog repaints, and the buffer holds every earlier frame.
        last: dict[int, str] = {}
        for n, body in numbered:
            if n == 1 and last:
                last = {}
            last[n] = body
        yes = [n for n, body in last.items() if body.startswith("trustfolder")]
        if len(yes) != 1:
            return None  # ambiguous or absent — refuse rather than guess
        return f"{yes[0]}\r".encode()

    def _trust_selection_keys(self) -> bytes | None:
        """Keystrokes that move the cursor onto the affirmative option, or None if unreadable.

        Reads the dialog rather than guessing at it. Measured against claude 2.1.247, which
        draws (whitespace collapsed, because a TUI positions the cursor instead of emitting
        spaces):

            ❯No,exit
            Yes,Itrustthisfolder

        The cursor starts on the DECLINE option, so the affirmative one is one Down away — but
        that is a fact to be read off the screen each time, not a constant to hard-code, since
        the inverse ordering makes the same keystroke destructive.
        """
        # A numbered menu is answered by its digit — no cursor reading, no frame splitting.
        numbered = self._numbered_trust_key()
        if numbered is not None:
            return numbered
        opts = self._trust_options()
        if not opts:
            return None
        cursor = [i for i, (_, c) in enumerate(opts) if c]
        yes = [i for i, (a, _) in enumerate(opts) if a]
        if len(yes) != 1:
            return None  # ambiguous or absent affirmative — refuse rather than guess
        if len(opts) == 1:
            return b"\r"  # single option, and it is the affirmative one
        if len(cursor) != 1:
            return None  # cannot know where a relative move would land
        delta = yes[0] - cursor[0]
        if delta == 0:
            return b"\r"
        arrow = b"\x1b[B" if delta > 0 else b"\x1b[A"
        return arrow * abs(delta) + b"\r"

    def _trust_options(self) -> list[tuple[bool, bool]]:
        """The dialog's options as (is_affirmative, bears_cursor), in drawn order.

        Only the LAST drawn frame is used: the dialog repaints, the rolling buffer holds every
        earlier frame, and a stale cursor position would otherwise be read as the current one.

        Frames are separated by a REPEATED option kind, not by blank lines — measured, because
        claude 2.1.247 draws a blank line *between* its two options, so splitting on blank lines
        would cut the real dialog in half. A second "No"-style option instead means the block
        has started over.
        """
        blocks: list[list[tuple[bool, bool]]] = []
        run: list[tuple[bool, bool]] = []
        for raw in _ESC_RE.sub("", self.screen_text()).splitlines():
            line = "".join(raw.split())
            if not line:
                continue
            low = line.lower()
            cursor = low[:1] in _CURSOR_MARKS
            body = re.sub(r"^\d+[.)]", "", low.lstrip("".join(_CURSOR_MARKS)))
            if body.startswith("yes"):
                kind = True
            elif body.startswith(("no,", "no.", "noexit", "no-")):
                kind = False
            else:
                continue
            if any(k == kind for k, _ in run):  # this kind repeated ⇒ a new frame began
                blocks.append(run)
                run = []
            run.append((kind, cursor))
        if run:
            blocks.append(run)
        return blocks[-1] if blocks else []

    def _screen_has_trust_marker(self) -> bool:
        """Read the screen ONCE and test every marker against it.

        `any(m in self._normalised_screen() for m in _TRUST_MARKERS)` re-runs the reader for
        each marker — three full buffer decodes per poll, and a stubbed reader gets drained
        three times as fast than the caller expects.
        """
        screen = self._normalised_screen()
        return any(m in screen for m in _TRUST_MARKERS)

    def trust_prompt_showing(self) -> bool:
        """Is a trust dialog currently on screen? Readiness must never be true while it is."""
        return any(m in self._normalised_screen() for m in _TRUST_MARKERS)

    def wait_ready(self, timeout: float = 120.0, quiet: float = 2.0) -> bool:
        """Wait until the engine is actually ready for input — first paint, then quiet.

        **Not "the transcript store exists".** That was the first attempt and it can never
        succeed for a fresh session: an engine writes its store when it commits its first
        turn, so requiring the store before sending the turn is a deadlock that presents as a
        timeout — and a careless reading of that timeout is "the nudge did not submit".

        The predicate is the one `webterm`'s handoff seeding already pays for and the one #801
        points at: **first paint plus a quiet window**. `DECSET 2004` is deliberately not used
        — an engine can arm bracketed-paste mode in its preamble and still be eating stdin.

        A store that already has turns still short-circuits, so a resumed session is ready
        immediately rather than waiting out a redraw that is not coming.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if user_turns(self.engine, self.native_id) > 0:
                return True  # resumed session: already has history
            if self._proc is not None and self._proc.poll() is not None:
                return False  # engine exited (trap 3) — not a submission failure
            # Trap 3's other half: this dialog holds the engine alive and silent forever.
            if self._answer_trust_prompt():
                time.sleep(1.0)  # let the accept redraw before judging quiet
                continue
            last = scrollback.get_last_output_at(self.key)
            if (
                last is not None
                and (time.time() - last) >= quiet
                and not self.trust_prompt_showing()
            ):
                # A modal that paints and then waits is quiet too — the same shape as the
                # logged-out prompt. "Quiet" alone is not readiness.
                #
                # The onboarding/auth check is here for the same reason the trust one is, and it
                # was missing: gemini's Terms-of-Service notice is neither a trust dialog nor an
                # exit, so readiness returned True with it on screen and the matrix published a
                # red cell for an engine that never reached a prompt. Readiness is "at a prompt",
                # and a modal is not a prompt whichever modal it is.
                return True
            time.sleep(0.25)
        return False

    def _store_exists(self) -> bool:
        loc = transcript.locator_for(self.engine)
        if loc is None:
            return False
        try:
            return bool(loc(self.native_id, Path.home()))
        except (OSError, ValueError):
            return False

    def _descendant_loop(self) -> None:
        """Sample descendants on a timer, right up to the reap.

        Output-driven sampling misses a child that starts after the last PTY byte — and
        teardown stops the reader *before* killing anything, so such a child is never seen at
        all. A probe emitted one byte, later launched a silent out-of-group child, and teardown
        reported `remembered=False`, `stragglers=[]` (review on #858).
        """
        while not self._watch_stop.is_set():
            with contextlib.suppress(Exception):
                self._sample_descendants(force=True)
            self._watch_stop.wait(0.5)

    def _sample_descendants(self, force: bool = False) -> None:
        """Remember every pid descending from the launch, while it is still discoverable.

        Periodic rather than once at teardown, because that is the only window in which an
        escaping child is attributable: `cleanup_runtime` kills the master's group first, and a
        child that has left that group carrying neither the session id nor the socket path
        cannot be found afterwards from anything. A probe reproduced exactly that —
        `escaped_child_survived_teardown=True` with `straggler_pids=[]` (review on #858).
        """
        now = time.monotonic()
        if not force and now - self._last_descendant_scan < 1.0:
            return
        self._last_descendant_scan = now
        root = self._proc.pid if self._proc is not None else None
        if root is None:
            return
        children: dict[int, list[int]] = {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as fh:
                    ppid = int(fh.read().rsplit(")", 1)[-1].split()[1])
            except (OSError, IndexError, ValueError):
                continue
            children.setdefault(ppid, []).append(int(entry))
        stack, seen = [root], set()
        while stack:
            pid = stack.pop()
            if pid in seen:
                continue
            seen.add(pid)
            stack.extend(children.get(pid, ()))
        for pid in seen:
            born = self._starttime(pid)
            if born is not None:
                self._seen_descendants.setdefault(pid, born)

    def deliver(self, payload: bytes, *, require_quiet: bool = True) -> session_input.Outcome:
        """Write through the production seam — the same call `actuator.deliver` makes.

        Records the **gap between the session's last output and this write**, because that is
        the only attributable evidence about what the terminal was doing when the bytes went
        out. Post-write output cannot serve: the nudge's own echo, and the turn it starts,
        both produce output afterwards, so "output after the write" is satisfied even when the
        repaint ended long before (review on #858).
        """
        last = scrollback.get_last_output_at(self.key)
        now = time.time()
        self.last_write_at = now
        self.output_gap_at_write = (now - last) if last is not None else None
        return session_input.send_input(self.key, payload, require_quiet=require_quiet)


#: **Per-engine, independently demonstrated prompt-ready signals — and it is EMPTY.**
#:
#: An entry maps an engine id to `(session) -> (ready, detail)`, proving the session is sitting at
#: an input prompt by some means other than submitting to it. Adding one requires DEMONSTRATING
#: the engine's real store/state boundary, not reasoning about it — five attempts at a general
#: rule were each refuted, four of them by evidence rather than by argument:
#:
#: 1. pattern-match the screen — six rounds of narrowing, each defeated by ordinary agent output;
#: 2. …and it cannot work in principle: `screen_text()` is a rolling BYTE buffer and a TUI
#:    repaints by overwriting, so tail-slicing cannot establish what is DISPLAYED;
#: 3. delete the classifier — every blocked engine became a red cell, which is a false negative
#:    about the ENGINE and exactly what #801 forbids;
#: 4. a warm-up turn — CIRCULAR: it made a committed turn the prerequisite for measuring turn
#:    commitment, so the defect under test classified itself as UNTESTED;
#: 5. transcript-record existence — refuted by the installed stores, in BOTH directions. Measured
#:    on this host: gemini has 323 session files and **zero** with a user record (317 under 4 KB),
#:    so its header exists before the prompt and a session parked on the Terms notice would pass
#:    the check and publish a false red. claude has 170 files and **all 170** carry a user record,
#:    so nothing exists until a turn commits — the same check would skip claude, the one engine
#:    that passes. A signal that is early for one engine and late for another is not a signal.
#:
#: So the registry starts empty and is the honest extension point: to turn an engine's failing
#: cell from UNTESTED into RED, contribute a demonstrated signal for it here.
PROMPT_READY: dict[str, Callable[[RealSession], tuple[bool, str]]] = {}


def prompt_ready(session: RealSession) -> tuple[bool | None, str]:
    """`(ready, detail)` — `None` means NO SIGNAL EXISTS for this engine, which is not `False`.

    The three answers are genuinely different and the caller acts differently on each: `True` is
    "it was at a prompt, so a dropped payload is a defect", `False` is "it was demonstrably not",
    and `None` is "nobody has established how to tell for this engine" — the state every engine
    is in today.
    """
    signal = PROMPT_READY.get(session.engine)
    if signal is None:
        return None, f"no demonstrated prompt-ready signal for {session.engine}"
    return signal(session)


def submits(
    session: RealSession, payload: bytes, *, require_quiet: bool = True, settle: float = 120.0
) -> tuple[bool, str]:
    """``(submitted, detail)`` for one payload against one live session.

    Counts committed user turns before and after. `settle` is a bounded wait, not a sleep-and-
    hope: a turn that has not appeared in the store by then is reported as not submitted, and
    the count is included so a reader can tell "no turn" from "the store moved unexpectedly".

    **The window is deliberately generous, because a tight one measures the wrong thing.** It
    answers "did this commit within N seconds", not "did it submit" — and at 12s it produced a
    false FAIL three separate times: once on the busy cell (the same nudge landed 1.0s after
    the window closed) and once on claude quiescent, which committed between 12s and 15s while
    running at xhigh effort. A slow engine is not a dropped nudge. Erring long costs only wall
    clock on a genuine failure; erring short publishes an engine defect that does not exist.
    """
    before = user_turns(session.engine, session.native_id)
    outcome = session.deliver(payload, require_quiet=require_quiet)
    if outcome.state != "delivered":
        return False, f"not delivered: {outcome.state} ({outcome.detail})"
    deadline = time.time() + settle
    while time.time() < deadline:
        after = user_turns(session.engine, session.native_id)
        if after > before:
            return True, f"user turns {before} → {after}"
        time.sleep(0.5)
    # The SCREEN rides on every failure, because a red cell here has two very different causes
    # and they are indistinguishable from the turn count alone: the engine dropped the nudge, or
    # the engine was never at a prompt. An unrecognised modal reads exactly like the defect under
    # test — that is not hypothetical, it is what an unmatched gemini trust dialog did to this
    # matrix. Naming the screen in the failure makes the second cause announce itself instead of
    # being published as the first.
    tail = "".join(_ESC_RE.sub("", session.screen_text()).split())[-220:]
    return (
        False,
        f"delivered, but user turns stayed at {before} after {settle:.0f}s "
        f"| screen tail: {tail!r}",
    )
