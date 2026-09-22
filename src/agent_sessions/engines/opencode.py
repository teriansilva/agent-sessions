"""opencode engine provider (split out of the single-file ``engines.py``, #265 S1)."""

from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
from datetime import datetime
from pathlib import Path

from .. import discover
from .. import metadata as _metadata
from ..scanner import Session, is_ephemeral_cwd
from . import base

# Columns the opencode reader depends on, pinned so a schema rename fails the
# fixture test (loud) rather than silently dropping rows in prod (it would just
# fail-soft to no opencode rows).
OPENCODE_SCHEMA = (
    "id",
    "parent_id",
    "directory",
    "title",
    "time_created",
    "time_updated",
    "time_archived",
)


# --- the opencode log, as START evidence (#1050) ----------------------------------------------
#
# One shared append-only file (94 MB on the author's install), written by every concurrent
# opencode on the host — so every read here is a BOUNDED tail, and every match is bound to this
# launch's own directory and start time rather than to the file merely having been touched.

#: How far back the first read looks. A launch's own `creating instance` line lands ~1.8 s in, and
#: the dispatcher polls within seconds, so the window only has to cover what other opencodes wrote
#: in that gap.
LOG_TAIL_BYTES = 256 * 1024
#: The ceiling on that growth. Past this the honest answer is "we could not look back far enough",
#: never "there was nothing there" — the #989 rule that unreadable is not a flavour of absent.
LOG_TAIL_MAX_BYTES = 8 * 1024 * 1024

_LOG_TS = re.compile(r"^timestamp=(\S+)")

#: How far BEFORE this launch's own start a `creating instance` line may be stamped and still count.
#: Covers the log's millisecond rounding and the gap between `launched_at` being taken and the spawn
#: — never wide enough to admit an instance that existed before this launch. Pinned by a test that
#: goes red if it is widened (review comment 72377 finding 6).
LOG_TIME_TOLERANCE_S = 2.0


class _LogUnreadable(Exception):
    """The log could not be read, or not read back far enough to cover this launch."""


def _resolved(path: str) -> str:
    """Compare directories by what they ARE, not how they were spelled — claude's rule (#916)."""
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _in_dir(cwd: str):
    """A predicate: does an opencode ``directory`` value name the same place as ``cwd``?

    Resolved on BOTH sides (review comment 72377 finding 2). opencode records the directory it was
    started in as its real path, while the dispatcher and the new-session route hold the name the
    operator picked — which may be a symlinked checkout. Comparing the raw strings let
    `start_evidence` (already resolved) say `found` while `bind_by_nonce`'s candidate list, built
    from these ids, stayed empty for ever: the brief was typed, the binding timed out, and the
    dispatch tore down a working agent. The exact string still matches first, so an unresolvable
    name compares as it always did; resolutions are memoised per call because the store holds
    every session on the host.
    """
    want = _resolved(cwd)
    seen: dict[str, bool] = {}

    def match(directory) -> bool:
        d = directory or ""
        if d == cwd:
            return True
        if not d:
            return False
        if d not in seen:
            seen[d] = _resolved(d) == want
        return seen[d]

    return match


def _log_timestamp(line: str) -> float | None:
    """The epoch seconds of one log line, or ``None`` when it does not carry a parseable one."""
    m = _LOG_TS.match(line)
    if not m:
        return None
    raw = m.group(1)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _log_directory(line: str) -> str | None:
    """The ``directory=`` value, taken to END OF LINE.

    Deliberately not a space-delimited field: the log is ``key=value`` separated by spaces, and a
    launch directory may contain spaces. On the ``creating instance`` line — the only one this
    reads — ``directory=`` is last, so the remainder of the line IS the path.
    """
    i = line.find("directory=")
    if i < 0:
        return None
    value = line[i + len("directory=") :].strip()
    return value or None


def _log_tail_since(path: Path, floor: float) -> list[str]:
    """The log's lines, read back far enough to cover everything written at or after ``floor``.

    Grows the window until the OLDEST line in it predates ``floor`` — which is what proves the
    window did not cut off a line we needed. Raises `_LogUnreadable` rather than returning a short
    answer: a partial window that happens to contain no match is indistinguishable from a genuine
    absence, and reporting it as absence is the failure this module refuses to make.

    A MISSING log is not unreadable: opencode has simply never run on this host yet, so there is
    genuinely no instance line — the caller polls and the file appears on the first launch.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return []
    except OSError as e:
        raise _LogUnreadable(f"opencode's log could not be read ({type(e).__name__})") from None

    window = LOG_TAIL_BYTES
    while True:
        start = max(0, size - window)
        try:
            with path.open("rb") as fh:
                fh.seek(start)
                text = fh.read(window + 4096).decode("utf-8", errors="replace")
        except OSError as e:
            raise _LogUnreadable(f"opencode's log could not be read ({type(e).__name__})") from None
        lines = text.split("\n")
        if start > 0 and lines:
            lines = lines[1:]  # the window cut the first line in half
        if start == 0:
            return lines
        oldest = next((_log_timestamp(x) for x in lines if _log_timestamp(x) is not None), None)
        if oldest is not None and oldest < floor:
            return lines
        if window >= LOG_TAIL_MAX_BYTES:
            raise _LogUnreadable(
                "opencode's log could not be read back as far as this launch within "
                f"{LOG_TAIL_MAX_BYTES // 1024}KiB"
            )
        window = min(window * 4, LOG_TAIL_MAX_BYTES)


# --- permissions for an unattended launch (#1050) --------------------------------------------

#: opencode reads an inline JSON config from this variable and merges it LAST, over the global,
#: project and `OPENCODE_CONFIG` files (measured on 1.18.32).
OPENCODE_CONFIG_CONTENT_ENV = "OPENCODE_CONFIG_CONTENT"

#: The primary agent an unattended launch without bypass runs as is ``<prefix><16 random hex>``,
#: minted PER LAUNCH (`mint_unattended_agent`). Not ``build``, and not any FIXED name either:
#: opencode merges `OPENCODE_CONFIG_CONTENT` key by key into an existing declaration of the same
#: agent, so a project ``.opencode/agent/<name>.md`` (or ``opencode.json`` ``agent.<name>``, or the
#: global agent directory) declaring ``"*": allow, bash: allow`` keeps its ``*`` position and its
#: ``bash`` lands after ours — reproduced on 1.18.32 for a fixed ``battlelab-mission``. A name
#: generated at launch cannot have been declared by anything written before the launch.
UNATTENDED_AGENT_PREFIX = "battlelab-mission-"
_UNATTENDED_AGENT_RE = re.compile(r"\Abattlelab-mission-[0-9a-f]{16}\Z")


def mint_unattended_agent() -> str:
    """A fresh, unguessable agent name for ONE unattended launch."""
    return f"{UNATTENDED_AGENT_PREFIX}{secrets.token_hex(8)}"


#: That agent's whole permission block — ORDER IS THE POLICY. opencode takes the LAST matching rule,
#: so the catch-all ``ask`` comes FIRST and only the read-only tools named after it can override it.
#: Parity with claude's no-bypass default (reads go through, anything that writes, runs or reaches
#: the network asks), rather than a prompt on every file read:
#:
#: * ``read`` / ``glob`` / ``list`` / ``todowrite`` — allowed. ``todowrite`` keeps the agent's own
#:   todo list and touches no file; ``todoread`` does not exist in 1.18.32.
#: * ``grep`` ASKS, although it only reads. Its permission is keyed on the search REGEX, not on a
#:   path, and it runs ``rg --hidden --glob=<include>``, which reads files ``.gitignore`` hides —
#:   so an allowed grep with ``include: .env`` would print the secrets the ``read`` guard below
#:   exists to protect, with no prompt (re-review at 89defb5).
#: * ``read`` keeps opencode's own ``.env`` safeguard, restated here because our ``*`` for ``read``
#:   would otherwise come after it: ``.env`` and ``.env.*`` ask, ``.env.example`` is allowed.
#: * Everything else — ``bash``, ``edit`` (which covers write and patch), ``webfetch``,
#:   ``websearch``, ``task``, ``skill``, ``external_directory``, and any tool a later opencode adds
#:   — matches only the catch-all, and asks. Reading OUTSIDE the project is gated by
#:   ``external_directory``, which is not allowed here, so it asks even though ``read`` is allowed.
UNATTENDED_PERMISSION: dict = {
    "*": "ask",
    "read": {"*": "allow", "*.env": "ask", "*.env.*": "ask", "*.env.example": "allow"},
    "glob": "allow",
    "list": "allow",
    "todowrite": "allow",
}


def _ask_config_content(existing: str | None, agent: str) -> str:
    """The `OPENCODE_CONFIG_CONTENT` that holds this launch to `UNATTENDED_PERMISSION`: reads
    inside the project go through, everything else asks.

    opencode evaluates a permission by the LAST matching rule (`findLast` in its `evaluate`), and
    an agent's rules are the defaults, then the config-level ``permission`` block, then the
    agent's own block. So what decides is the agent's own block, and this makes that block ours
    alone, in our order:

    * **Its own agent, named at launch (``agent``, from `mint_unattended_agent`).** A block is
      merged key by key into whatever the operator or the project already declared under the
      same name, so an existing agent whose block begins ``"*": "allow"`` keeps our ``*`` at ITS
      position, and every key after it — ``bash: allow``, say — still wins (measured, with a
      project ``.opencode/agent/build.md`` and with a fixed ``battlelab-mission``). A name minted
      from 64 random bits at launch has no earlier declaration to merge into.
    * **``default_agent`` names it**, so the TUI starts in it rather than in the operator's own
      default (measured: an operator ``default_agent: "yolo"`` is overridden). The model cannot
      switch primary agents; a subagent needs ``task``, which is itself ``ask``.
    * **The config-level block is replaced too**, belt and braces: it no longer decides, but a
      launch that says "ask" should not also carry an ``allow`` it does not need.

    An operator's own `OPENCODE_CONFIG_CONTENT` is kept — it may carry providers the launch needs
    to authenticate — with these keys set over it. One that is not a JSON object cannot be merged
    safely, so it is a `ValueError` and the launch is refused.

    Out of reach, stated rather than hidden: this policy constrains the MODEL, not the
    repository. An opencode plugin (global, or a project's ``.opencode/plugin/*.js``) runs code
    when opencode starts and can answer its own ``permission.ask`` hook; a project config can
    start MCP servers. Hostile project content is out of this policy's scope, exactly as a
    claude hook checked into a project is for claude.
    """
    if not isinstance(agent, str) or not _UNATTENDED_AGENT_RE.match(agent):
        raise ValueError("malformed unattended agent name")
    base_cfg: dict = {}
    if existing is not None and existing.strip():
        try:
            base_cfg = json.loads(existing)
        except ValueError:
            raise ValueError(
                f"{OPENCODE_CONFIG_CONTENT_ENV} in the app's environment is not valid JSON, so "
                "the permission policy for an unattended opencode cannot be applied over it"
            ) from None
        if not isinstance(base_cfg, dict):
            raise ValueError(
                f"{OPENCODE_CONFIG_CONTENT_ENV} in the app's environment is not a JSON object, so "
                "the permission policy for an unattended opencode cannot be applied over it"
            )
    agents = base_cfg.get("agent")
    agents = dict(agents) if isinstance(agents, dict) else {}
    agents[agent] = {
        "description": "BattleLab unattended mission: reads go through, everything else asks",
        "mode": "primary",
        "permission": json.loads(json.dumps(UNATTENDED_PERMISSION)),
    }
    return json.dumps(
        {
            **base_cfg,
            "permission": {"*": "ask"},
            "default_agent": agent,
            "agent": agents,
        }
    )


def _launch_master_state(launch) -> str:
    """``alive`` / ``dead`` / ``unknown`` for the dtach master THIS launch spawned, by its socket.

    Liveness alone is never start evidence (the 2026-08-25 incident: a live master that had received
    nothing); it is the conjunct that ties the log line to a process that is still ours.
    """
    from .. import ptybridge

    try:
        sock = ptybridge.socket_path(launch.engine, launch.native)
    except ptybridge.PtyBridgeError:
        return ptybridge.UNKNOWN
    return ptybridge.probe_master(sock)


class OpenCodeProvider:
    """opencode: sessions live in a SQLite DB (``~/.local/share/opencode/opencode.db``),
    resumed via ``opencode <dir> --session <id>``.

    **Read-only to opencode.db:** the sidebar never writes opencode's DB.
    ``archive``/``unarchive`` flip the engine-agnostic sidecar flag (``metadata.json``,
    OR'd into the row by ``list_sessions``), never ``opencode.db`` — opencode has no JSONL
    to move, so archive is a pure sidecar toggle. Rename/sticky work the same way (sidecar
    only). All DB access is read-only and
    **fail-soft**: any sqlite error (missing / locked / corrupt / schema drift)
    yields no opencode rows rather than taking down the Claude list.
    """

    engine_id = "opencode"
    id_pattern = base._SES_RE
    # opencode can't pin a new-session id (``opencode --session`` only *continues*; there
    # is no create-returning-id). So new-session uses launch-then-reconcile (#127): launch
    # ``opencode <dir>`` (mints its own ``ses_…``) under a client-minted ``new-<uuid>``
    # placeholder, then diff opencode.db to find the new ``ses_…`` for that cwd and record
    # a persisted placeholder→real alias. The ws route + alias layer do the reconcile; the
    # provider only supplies the snapshot/diff primitives and the new-launch argv.
    supports_new = True
    supports_orchestrator_input = True  # a TUI agent that reads a prompt (#726)
    expects_raw_tty = True  # ratatui/Ink TUI: its PTY must stay raw (#804)
    new_session_reconciles = True  # mints its own id → placeholder/reconcile flow (#127/#315)
    # Cross-engine handoff target (#597): the fresh opencode TUI accepts the seed as a
    # bracketed paste on its PTY input (never argv).
    supports_seed_start = True

    def _query_rows(self) -> list:
        """Read top-level opencode sessions, RAISING ``sqlite3.Error`` on a real read
        failure (locked / corrupt / schema drift). A genuinely absent DB file returns ``[]``
        — that's a valid empty (fresh opencode, no sessions yet), not a failure. Callers that
        must not confuse "read failed" with "empty" (the new-session baseline snapshot) use
        this directly; ``_query`` wraps it fail-soft for scan / is_present."""
        db = base._opencode_db()
        try:
            os.stat(db)
        except FileNotFoundError:
            return []  # a genuinely absent DB is a valid empty — fresh opencode, no sessions yet
        # Anything else is a FAILURE, not an absence: `os.path.exists` answers False for a
        # directory it cannot traverse, so an existing DB under an inaccessible ancestor was
        # classified as "no sessions" (review 4915/4919, finding 4). `_query` and `scan_checked`
        # both widen their handlers accordingly, so the sidebar stays fail-soft.
        cols = ", ".join(OPENCODE_SCHEMA)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
        try:
            con.execute("PRAGMA busy_timeout=500")
            return con.execute(
                f"SELECT {cols} FROM session WHERE parent_id IS NULL"  # noqa: S608 fixed cols
            ).fetchall()
        finally:
            con.close()

    def _query(self) -> list:
        # Fail-soft wrapper: any sqlite error yields no opencode rows rather than taking
        # down the Claude list. (The baseline snapshot can't use this — see _query_rows.)
        try:
            return self._query_rows()
        except (sqlite3.Error, OSError):
            return []

    def _db_readable(self) -> bool:
        db = base._opencode_db()
        if not os.path.exists(db):
            return False
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
            try:
                con.execute("SELECT 1 FROM session LIMIT 1")
                return True
            finally:
                con.close()
        except sqlite3.Error:
            return False

    def _bin(self) -> str:
        # The service may have started before opencode was installed/doctor rewrote env.
        # Resolve dynamically so ~/.opencode/bin/opencode still launches as an absolute argv[0].
        return discover.resolve(self.engine_id) or base.OPENCODE_BIN

    def is_present(self) -> bool:
        # A fresh opencode install has a CLI before it has an opencode.db. Treat either a
        # launchable binary or a readable DB as enough for the provider to participate.
        return discover.resolve(self.engine_id) is not None or self._db_readable()

    def _row(self, record) -> Session | None:
        """One row from one ``OPENCODE_SCHEMA`` record, or ``None`` if it is not listable. Shared by
        ``scan`` and ``lookup`` (#991)."""
        sid, _parent, directory, title, time_created, time_updated, time_archived = record
        if not isinstance(sid, str) or not self.id_pattern.match(sid):
            return None
        # Drop ephemeral CI-runner sessions (#452): their cwd is a throwaway
        # ``act`` workdir that's already deleted, so they can never be resumed
        # and only clutter the list / resume allowlist / picker.
        if is_ephemeral_cwd(directory or ""):
            return None
        return Session(
            engine=self.engine_id,
            uuid=sid,
            cwd=directory or "",
            # opencode stores epoch *milliseconds*; Claude uses seconds.
            last_mtime=(time_updated or 0) / 1000.0,
            first_user_message=title or "",  # opencode maintains a real title
            archived=time_archived is not None,
            # Real creation time from the DB (#506), ms → s; fall back to the update
            # time if a row somehow lacks time_created.
            created_at=(time_created or time_updated or 0) / 1000.0,
        )

    def scan(self) -> list[Session]:
        return self._build(self._query())

    def scan_checked(self) -> tuple[list[Session], list[str]]:
        """``scan()``'s rows, plus a line if the DB could not be READ (#993).

        A genuinely absent ``opencode.db`` is a valid empty — fresh opencode, no sessions yet — so
        it yields no problem. A locked, corrupt or schema-drifted one is a failure that ``_query``
        would have swallowed into "no sessions", which is the answer that must never authorise a
        deletion. The sidebar keeps the fail-soft ``scan()``.
        """
        try:
            rows = self._query_rows()
        except (sqlite3.Error, OSError) as e:
            return [], [f"{self.engine_id}: its database could not be read ({type(e).__name__})"]
        return self._build(rows), []

    def _build(self, records) -> list[Session]:
        """Listable rows from raw ``OPENCODE_SCHEMA`` records — the tail both scans share, built
        on ``_row`` (#991) so a record becomes a ``Session`` in exactly one place."""
        rows = (self._row(record) for record in records)
        return [row for row in rows if row is not None]

    def lookup(self, native_id: str) -> Session | None:
        """This one top-level session, read fresh (#991), or ``None``: one indexed row by id, with
        the same read-only connection and the same fail-soft as ``scan``."""
        if not self.id_pattern.match(native_id or ""):
            return None
        db = base._opencode_db()
        if not os.path.exists(db):
            return None
        cols = ", ".join(OPENCODE_SCHEMA)
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
            try:
                con.execute("PRAGMA busy_timeout=500")
                record = con.execute(
                    f"SELECT {cols} FROM session WHERE id = ? AND parent_id IS NULL",  # noqa: S608 fixed cols
                    (native_id,),
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            return None
        return self._row(record) if record is not None else None

    def launch_argv(self, native_id, *, cwd, bypass):
        # opencode resumes a session by id within its project dir. `bypass` is
        # accepted only for interface parity (permissions are config-side).
        return [self._bin(), cwd, "--session", native_id]

    def new_launch_argv(self, native_id, *, cwd, bypass):
        # Start a *fresh* opencode session in `cwd`. We deliberately pass NO `--session`:
        # ``opencode <dir>`` mints its own ``ses_…`` id, which the reconcile step (DB-diff)
        # discovers afterwards. `native_id` here is the client-minted ``new-<uuid>``
        # placeholder the bridge keys the socket/lock by; opencode never sees it. `bypass`
        # is config-side for opencode, so it doesn't change the argv (interface parity) — which
        # is exactly why an UNATTENDED launch does not come through here: `unattended_launch`
        # below is the one that turns `bypass=False` into an enforced policy (#1050).
        return [self._bin(), cwd]

    def unattended_launch(self, native_id, *, cwd, bypass, env):
        """``(argv, env_overrides)`` for an unattended launch — the one place ``bypass`` is honoured
        for opencode (#1050, review comment 72377 finding 1).

        **opencode's permissions are config, and its default is allow-all.** Measured on 1.18.32
        under a clean ``HOME``: ``opencode debug agent build`` resolves ``{"permission": "*",
        "action": "allow"}``. So the argv-only launch ignored `bypass=False` and a mission got an
        agent with unrestricted bash and edit that nobody was watching — exactly the grant the
        dispatcher refuses for claude. Without bypass this therefore holds the launch to
        `UNATTENDED_PERMISSION` — reads inside the project go through, everything else asks, as
        with claude's no-bypass default — through `OPENCODE_CONFIG_CONTENT` (see
        `_ask_config_content` for why that form and why its own agent). A prompt nobody answers is
        surfaced the way claude's is: the session
        sits on a screen that is blocked on the user, which the review pass reads off the live
        screen for any engine.

        **The cwd is ``"."``, never the name.** The dispatcher spawns in the directory it opened as
        a descriptor (``/proc/self/fd/N``, #904 review 3 finding 4); a path argument would make
        opencode resolve the operator's folder BY NAME again, after every check that pinned it.
        ``"."`` resolves to the process's own working directory — the pinned inode — and opencode
        records ``getcwd()``, its real path (review comment 72377 finding 5).

        Shell-free: the override rides in the child's environment, never in a command string.
        Raises `ValueError` when the enforcement cannot be guaranteed, and the dispatcher refuses
        the launch rather than starting an agent with the operator's (possibly allow-all) policy.
        """
        overrides: dict[str, str] = {}
        if not bypass:
            overrides[OPENCODE_CONFIG_CONTENT_ENV] = _ask_config_content(
                env.get(OPENCODE_CONFIG_CONTENT_ENV), mint_unattended_agent()
            )
        return [self._bin(), "."], overrides

    # --- the unattended-launch capabilities (#989 contract, #1050 evidence) ------------------
    #
    # `snapshot_session_ids` below is the fourth; `bind_session` is `bind_by_nonce`; these two are
    # the ones that had to be established against the real CLI before they could be written.

    def unattended_preflight(self, *, cwd, probe=None, gate=None):
        """Will opencode come up authenticated, in this directory, with nobody watching? (#1050)

        ``opencode auth list`` is the non-interactive check: measured on 1.18.31 it exits 0 and
        prints a credential count without opening a TUI or touching the network. The credential
        count is the verdict — a logged-out install prints ``0 credentials``, which is a REFUSAL
        (the agent would take the brief and be unable to act on it), while a probe that does not
        complete is ``unknown``, and `headless_dispatch` refuses an unknown exactly as it refuses a
        no: "refusing to start an unattended agent on an unknown".

        Run through `engine_auth.run_probe`, never a fresh `subprocess` call here, so the literal
        argv, the closed stdin, the pgid-at-spawn and the `gate`-around-the-spawn-only rules (#921)
        hold for this engine too.

        The consent half of the question is answered by the artifact rather than by the probe:
        measured 2026-09-21, opencode painted a ready prompt in a directory it had never seen, with
        no trust or consent gate — and `start_evidence` below would not report `found` for a screen
        that never created an instance anyway.

        `detail` names the branch only, never probe output: this module's rule is that a diagnostic
        can never leak a token that appeared in an error message.
        """
        from .. import engine_auth

        completed, out = engine_auth.run_probe(
            [self._bin(), "auth", "list"], cwd=cwd, probe=probe, gate=gate
        )
        if not completed:
            return (
                base.PREFLIGHT_UNKNOWN,
                "the credential probe did not complete within its timeout",
            )
        m = re.search(r"(\d+)\s+credential", out)
        if m is None:
            # Exited, but said nothing this code recognises. NOT a refusal — opencode never said
            # no — and not permission either.
            return base.PREFLIGHT_UNKNOWN, "the credential probe returned no recognisable count"
        if int(m.group(1)) <= 0:
            return base.PREFLIGHT_REFUSED, "opencode has no credentials configured on this host"
        return base.PREFLIGHT_OK, "opencode reported configured credentials"

    def start_evidence(self, launch):
        """Did an opencode agent actually START, before any byte is typed? (#1050)

        **Not the session store, and that is the whole point.** Measured 2026-09-21 on 1.18.31: a
        launched, painted, ready opencode wrote no ``session`` row for 45 s with nothing typed.
        That is #916's deadlock reproduced for this engine — the row waits on the turn, the turn
        waits on the brief, the brief waits on the row — so gating on the store could never
        dispatch anything.

        The artifact is the structured log, which gets this ~1.8 s in with nothing typed::

            timestamp=2026-09-21T12:16:01.812Z level=INFO run=287c3988 \
                message="creating instance" directory=/path/to/cwd

        A line is ours when its ``directory`` RESOLVES to this launch's cwd (so ``/tmp`` vs
        ``/private/tmp`` and a symlinked checkout do not read as different places, the rule
        `start_evidence` already applies for claude) and its timestamp is at or after this
        launch's own start.

        **Three answers, never two**, and ambiguity is `unreadable` rather than `absent`:

        * one match  → ``found``;
        * none yet   → ``absent`` — the caller polls until its deadline;
        * more than one → ``unreadable``. The log carries a ``run`` id but **no pid**, so two
          instances created in our directory since our launch cannot be told apart, and this gate
          decides whether to type a brief. Guessing here types into whichever screen is in front
          of us.
        * the log cannot be read, or cannot be read back as far as the launch → ``unreadable``.

        * one match, but this launch's own dtach master is dead or cannot be probed →
          ``unreadable``. `dtach` exits with its child, so a dead master means our opencode is
          gone and the line is somebody else's.

        **What this does NOT prove, stated rather than hidden** (review comment 72377 finding 4):

        * **That the instance is OURS.** The log carries a ``run`` id but no pid, so a single line
          in our directory since our launch could be an operator's opencode opened there while
          ours has not logged yet. The master-alive conjunct closes the "ours died" half only.
          The consequence is bounded: the brief is typed into OUR master's pty either way, behind
          the readiness gate, and the session is bound afterwards by the nonce proof, never by
          this line. Correlating the line with our own process is #1073.
        * **That an AGENT is ready.** ``creating instance`` means the process started; it cannot
          tell an agent from a blocking dialog. It is acceptable here because opencode was
          measured to show no trust or consent gate, and the readiness gate still protects the
          delivery — but it is a weaker artifact than claude's registry, not an equal one.
        """
        from .. import start_evidence as _se

        want = _resolved(str(launch.cwd))
        # A small tolerance for the log's own millisecond rounding — never a window wide enough to
        # admit a line written before this launch existed.
        floor = float(launch.launched_at or 0.0) - LOG_TIME_TOLERANCE_S

        try:
            lines = _log_tail_since(base._opencode_log(), floor)
        except _LogUnreadable as e:
            return _se.UNREADABLE, str(e)

        hits = 0
        for line in lines:
            if 'message="creating instance"' not in line:
                continue
            ts = _log_timestamp(line)
            if ts is None or ts < floor:
                continue
            directory = _log_directory(line)
            if directory is None or _resolved(directory) != want:
                continue
            hits += 1

        if hits == 1:
            # AND OUR OWN MASTER IS STILL RUNNING (review comment 72377 finding 4). `dtach` exits
            # with its child, so a dead master means OUR opencode is gone, and the one line in our
            # directory is somebody else's — an operator's own opencode opened there since. That is
            # not a start, and it is not "not yet" either.
            from .. import ptybridge

            master = _launch_master_state(launch)
            if master == ptybridge.ALIVE:
                return _se.FOUND, "opencode created an instance in this directory"
            if master == ptybridge.DEAD:
                return _se.UNREADABLE, (
                    "an opencode instance was created in this directory, but this launch's own "
                    "process is no longer running, so it was not this launch's"
                )
            return _se.UNREADABLE, (
                "an opencode instance was created in this directory, but whether this launch's own "
                "process is still running could not be established"
            )
        if hits == 0:
            return _se.ABSENT, "opencode has not created an instance in this directory yet"
        return _se.UNREADABLE, (
            f"{hits} opencode instances were created in this directory since the launch, and the "
            "log carries no pid to tell them apart"
        )

    def bind_session(self, launch):
        """Which opencode session did this launch become? By the nonce, never by "the one new id".

        `bind_by_nonce` is the shared implementation (#989); opencode qualifies for it because its
        transcript adapter preserves the pasted text verbatim, including the envelope's trailing
        ``[mission attempt <nonce>]`` line — verified across 40 real sessions on 2026-09-21.

        It lives in `launch_binding`, NOT in `start_evidence` — two modules that answer adjacent
        questions about the same launch and are easy to conflate. `start_evidence` owns
        FOUND/ABSENT/UNREADABLE (did an agent start), `launch_binding` owns the nonce proof and
        BIND_* (which session it became). Getting this wrong raises `AttributeError`, which
        `headless_dispatch._await_binding` turns into `BIND_UNREADABLE` and polls until the budget
        expires — so the dispatch fails only AFTER the brief has been typed into a live agent,
        which is the lost-record failure #989 exists to prevent. Hermes caught exactly that on
        review 4983.
        """
        from .. import launch_binding

        return launch_binding.bind_by_nonce(self, launch)

    def snapshot_session_ids(self, cwd: str) -> set[str] | None:
        """The set of top-level opencode ``ses_…`` ids currently in ``cwd`` (#127), or
        ``None`` if the DB read FAILED.

        Taken *before* launch so the post-launch diff can attribute the one new id to our
        placeholder. A failed read MUST NOT be confused with a genuinely empty one: if a
        transient sqlite lock/corrupt/schema error yielded an empty baseline while the cwd
        already had a ``ses_…`` row, the next successful poll would see that pre-existing row
        as "new" and misattribute it — a wrong attach, exactly what #127 must never do. So on
        read failure we return ``None`` and the caller skips reconciliation (stays on the
        placeholder). A missing DB file is a valid empty baseline (fresh opencode), not a
        failure. cwd-scoped so an unrelated new session elsewhere can't be mistaken for ours.
        """
        try:
            rows = self._query_rows()
        except sqlite3.Error:
            return None
        here = _in_dir(cwd)
        return {
            sid
            for sid, _parent, directory, _title, _tc, _tu, _ta in rows
            if isinstance(sid, str) and self.id_pattern.match(sid) and here(directory)
        }

    def reconcile_new_session(self, cwd: str, snapshot: set[str]) -> str | list[str] | None:
        """Find the opencode session id created in ``cwd`` since ``snapshot`` (#127).

        Returns:
          * the single new ``ses_…`` id — our session (unambiguous attribution), or
          * a ``list`` of ≥2 new ids — AMBIGUOUS (two new same-cwd sessions in the poll
            window): the caller must NOT guess (fail-safe — never attach to the wrong
            one), or
          * ``None`` — opencode hasn't written a new row yet (it may not until the first
            message): the caller keeps serving under the placeholder and polls again.

        Read-only to opencode.db; never mutates it.
        """
        here = _in_dir(cwd)
        new_ids = [
            sid
            for sid, _parent, directory, _title, _tc, _tu, _ta in self._query()
            if isinstance(sid, str)
            and self.id_pattern.match(sid)
            and here(directory)
            and sid not in snapshot
        ]
        if not new_ids:
            return None
        if len(new_ids) > 1:
            return new_ids  # ambiguous → caller fails safe
        return new_ids[0]

    def archive(self, native_id):
        # opencode.db stays read-only; record the archive flag in the engine-agnostic
        # sidecar (same place rename/sticky live). list_sessions ORs it into the row.
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id):
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)
