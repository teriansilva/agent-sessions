"""opencode 2.x store kind (#1986): a separate agent beside opencode 1.x, never sharing its data.

opencode 2.x keeps the SAME default paths as 1.x (`~/.local/share/opencode/opencode.db`), with a
new schema (`session_v2`, `session_message`, `session_part`). Left to its defaults it would open
and migrate the 1.x database, which the `opencode` agent still reads. So this kind owns two rules:

* **Isolation (`agent_env`).** Every process BattleLab starts for this agent gets
  ``XDG_DATA_HOME`` / ``XDG_STATE_HOME`` / ``XDG_CACHE_HOME`` / ``XDG_CONFIG_HOME`` forced to an
  XDG tree under the agent's own store root, and the launch is REFUSED when that tree would resolve
  to (or inside, or around) a 1.x store. Every spawn seam asks `plugins.provider.forced_env`, and
  ``tests/test_opencode2.py`` pins that each one does.
* **Read-only.** BattleLab never writes the 2.x database: archive/rename/sticky are sidecar flags
  (``metadata.json``), exactly as for 1.x. Any sqlite failure degrades to "no rows" for listing.

* **Unattended (#2083).** Start evidence is 2.x's OWN log (`start_evidence`), the session is
  bound by the attempt's nonce (`bind_session`), a guarded launch runs under a BattleLab-owned
  restrictive agent (`unattended_launch`), and the preflight is a measured argv that starts no
  shared service (`unattended_preflight`). Every probe gets the forced XDG tree, last.

Shared with 1.x only where the code is truly the same (the ``ses_`` id pattern, the directory
match, the bounded log tail, the master probe, the per-launch agent name); there is no
dual-identity class, so the isolation invariant stays testable at this seam.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from .. import metadata as _metadata
from ..scanner import Session, is_ephemeral_cwd
from . import base
from .opencode import (
    _API_AGENT_RE,
    _UNATTENDED_AGENT_RE,
    OPENCODE_CONFIG_CONTENT_ENV,
    _in_dir,
    _launch_master_state,
    _log_tail_since,
    _LogUnreadable,
    _resolved,
    mint_unattended_agent,
)

# Columns this reader depends on, pinned so a schema rename fails loud in the fixture test rather
# than silently dropping rows (it would fail soft to no rows in production).
OPENCODE2_SCHEMA = (
    "id",
    "parent_id",
    "directory",
    "title",
    "time_created",
    "time_updated",
    "time_archived",
)

#: The XDG variables forced on every process, each to ``<store root>/<subdir>``. opencode 2.x
#: derives all of its paths (`opencode debug paths`: data, state, cache, config, log, db) from them.
XDG_DIRS = {
    "XDG_DATA_HOME": "data",
    "XDG_STATE_HOME": "state",
    "XDG_CACHE_HOME": "cache",
    "XDG_CONFIG_HOME": "config",
}
#: Where opencode puts its own directory under each XDG base (`<base>/opencode`).
VENDOR_DIR = "opencode"
#: The 1.x layout whose store this agent must never resolve into.
_ONE_X_LAYOUT = "opencode-sqlite"


def _real(p: Path) -> Path:
    return Path(os.path.realpath(p))


def _overlaps(a: Path, b: Path) -> bool:
    """Do two directories name the same place, or does one contain the other?"""
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _one_x_dirs(home: Path) -> list[Path]:
    """Every directory a 1.x store lives in on this host: the loaded `opencode` agent's store
    (wherever its manifest or an override puts it), plus 2.x's own default, which IS the 1.x
    default — the place 2.x would write with nothing forced."""
    out = [home / ".local/share" / VENDOR_DIR]
    from . import registry

    try:
        out.append(Path(registry.store_for_layout(_ONE_X_LAYOUT, "db", home)).parent)
    except base.EngineError:
        pass  # no 1.x agent loaded: its default above still applies
    return out


def isolation_env(root: Path, db: Path | None, home: Path) -> dict[str, str]:
    """The forced XDG environment for a 2.x process whose store root is ``root``, or raise.

    Refuses (`EngineError`) when:
      * the declared db is not where 2.x will put it under the forced data home (the manifest and
        the env would disagree about which database this agent owns), or
      * any XDG tree, the root or the db resolves to, inside, or around a 1.x store directory.
    Creates the private subdirectories (0700) so 2.x never falls back to a default.
    """
    root = Path(root)
    if not root.is_absolute():
        raise base.EngineError("opencode2: the store root must be absolute")
    expected_db = root / XDG_DIRS["XDG_DATA_HOME"] / VENDOR_DIR / "opencode.db"
    if db is None or Path(db) != expected_db:
        raise base.EngineError(
            "opencode2: the store's db must be <root>/data/opencode/opencode.db, where the forced "
            "data home puts it"
        )
    forbidden = [_real(d) for d in _one_x_dirs(home)]
    candidates = [_real(root), _real(expected_db.parent)]
    candidates += [_real(root / sub / VENDOR_DIR) for sub in XDG_DIRS.values()]
    for c in candidates:
        for f in forbidden:
            if _overlaps(c, f):
                raise base.EngineError(
                    f"opencode2: refusing to run — {c} overlaps the opencode 1.x store {f}"
                )
    env: dict[str, str] = {}
    for var, sub in XDG_DIRS.items():
        d = root / sub
        d.mkdir(mode=0o700, parents=True, exist_ok=True)
        env[var] = str(d)
    return env


# --- the 2.x log, as START evidence (#2083) ---------------------------------------------------
#
# `<data>/opencode/log/opencode.log`, appended by every 2.x process BattleLab starts. Measured on
# 2.2.7: one `key=value` record per line; a value is bare when it matches `^[^\s="\\]+$` and is a
# JSON string otherwise (the binary's own formatter), so a quote, a backslash, a space, an `=` or a
# tab in a directory arrives JSON-escaped. The server child writes, before the first message:
#
#     timestamp=… level=INFO run=<id> message="location services booted" directory=<cwd> \
#         workspaceID=undefined durationMs=… http.span=… role=server
#
# `run=` precedes `message=` and `directory=` is NOT last, so every field is read BY NAME.

#: The one record that says a 2.x server booted in a directory.
LOG_BOOT_MESSAGE = "location services booted"
#: Every process's first record: a log whose beginning is newer than the launch must start here,
#: or its beginning was cut away (truncated or replaced) and a line we needed may have gone with it.
LOG_FIRST_MESSAGE = "cli starting"

_LOG2_FIELD = re.compile(r'([^\s="\\]+)=("(?:[^"\\]|\\.)*"|[^\s="\\]*)')
#: One model line of `opencode models` (`provider/model`), as 2.2.7 prints them.
_MODEL_LINE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*/[^\s/][^\s]*\Z")


def _log2_fields(line: str) -> dict[str, str] | None:
    """One 2.x log record as ``{key: value}``, or ``None`` when it is not exactly that shape.

    Strict: single-space separated ``key=value`` pairs to the end of the line, quoted values
    decoded as the JSON strings they are, no key twice. Anything else — a stray character, an
    unterminated quote, an escape JSON refuses — is ``None``, never a guess at what was meant.
    """
    out: dict[str, str] = {}
    pos, n = 0, len(line)
    while True:
        m = _LOG2_FIELD.match(line, pos)
        if m is None:
            return None
        key, raw = m.group(1), m.group(2)
        if raw.startswith('"'):
            try:
                value = json.loads(raw)
            except ValueError:
                return None
            if not isinstance(value, str):
                return None
        else:
            value = raw
        if key in out:
            return None
        out[key] = value
        pos = m.end()
        if pos == n:
            return out
        if line[pos] != " " or pos + 1 == n:
            return None
        pos += 1


def _log2_time(fields: dict[str, str]) -> float | None:
    raw = fields.get("timestamp")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _guarded_config_content(existing: str | None, agent: str) -> str:
    """The `OPENCODE_CONFIG_CONTENT` that holds a guarded unattended 2.x launch to
    `UNATTENDED_PERMISSIONS`, in 2.x's own shape — the analogue of 1.x's `_ask_config_content`.

    2.x evaluates a tool call by the LAST matching rule (``findLast`` over the agent's rules, then
    the session's; none → ``ask``), and its config takes ``permissions`` as ordered lists of
    ``{action, resource, effect}``. Measured on 2.2.7 (#2083) under a permissive global config AND
    a permissive project ``opencode.json`` — each tried in both the 1.x and the 2.x shape, each
    naming its own allow-all ``default_agent``:

    * the agent named here is the session's agent (the env's ``default_agent`` wins over both);
    * a real ``shell`` call (``touch``) and a real ``write`` were held at "Permission required"
      and never ran, while the same configs without this block let both run.

    So, as for 1.x: a primary agent **named per launch** (`mint_unattended_agent`, so nothing could
    have declared it before), made the default, whose rules are ours alone and in our order; the
    config-level list replaced by a single ``ask`` (belt and braces: it does not decide) and the
    1.x-shaped config-level ``permission`` dropped for the same reason. An operator's own
    `OPENCODE_CONFIG_CONTENT` is kept otherwise — it may carry providers — and one that is not a
    JSON object is a `ValueError`: the launch is refused rather than run on an unknown policy.

    Out of reach, as for 1.x: a project plugin or MCP server runs code when 2.x starts.
    """
    if not isinstance(agent, str) or not _UNATTENDED_AGENT_RE.match(agent):
        raise ValueError("malformed unattended agent name")
    return _agent_config(
        existing,
        agent,
        UNATTENDED_PERMISSIONS,
        "BattleLab unattended mission: reads go through, everything else asks",
    )


def _agent_config(
    existing: str | None,
    agent: str,
    rules: list[dict[str, str]],
    description: str,
    *,
    config_level: str = "ask",
) -> str:
    """One freshly named primary agent holding ``rules`` (in 2.x's ordered list shape), made the
    ``default_agent``, with the config-level list replaced by one catch-all ``config_level`` rule
    — the shape `_guarded_config_content` documents, shared by the unattended launch and the API
    client (#2084)."""
    base_cfg: dict = {}
    if existing is not None and existing.strip():
        try:
            base_cfg = json.loads(existing)
        except ValueError:
            raise ValueError(
                f"{OPENCODE_CONFIG_CONTENT_ENV} in the app's environment is not valid JSON, so "
                "the permission policy for an unattended opencode 2 cannot be applied over it"
            ) from None
        if not isinstance(base_cfg, dict):
            raise ValueError(
                f"{OPENCODE_CONFIG_CONTENT_ENV} in the app's environment is not a JSON object, so "
                "the permission policy for an unattended opencode 2 cannot be applied over it"
            )
    base_cfg = {k: v for k, v in base_cfg.items() if k != "permission"}
    agents = base_cfg.get("agents")
    agents = dict(agents) if isinstance(agents, dict) else {}
    agents[agent] = {
        "description": description,
        "mode": "primary",
        "permissions": json.loads(json.dumps(rules)),
    }
    return json.dumps(
        {
            **base_cfg,
            "permissions": [{"action": "*", "resource": "*", "effect": config_level}],
            "default_agent": agent,
            "agents": agents,
        }
    )


def _rule(action: str, resource: str, effect: str) -> dict[str, str]:
    return {"action": action, "resource": resource, "effect": effect}


#: The guarded agent's rules — ORDER IS THE POLICY (last match wins), 1.x's
#: `UNATTENDED_PERMISSION` in 2.x's list shape: the catch-all ``ask`` first, then only read-only
#: tools allowed after it. ``read`` keeps the ``.env`` guard (2.x's own default has the same three
#: rules); ``grep`` asks, as for 1.x, because it keys on a regex and can print an ignored ``.env``.
#: ``shell``, ``edit`` (write/patch), ``webfetch``, ``websearch``, ``subagent``, ``skill``,
#: ``external_directory``, MCP tools and anything a later 2.x adds match only the catch-all.
UNATTENDED_PERMISSIONS: list[dict[str, str]] = [
    _rule("*", "*", "ask"),
    _rule("read", "*", "allow"),
    _rule("read", "*.env", "ask"),
    _rule("read", "*.env.*", "ask"),
    _rule("read", "*.env.example", "allow"),
    _rule("glob", "*", "allow"),
    _rule("list", "*", "allow"),
    _rule("todowrite", "*", "allow"),
]


#: The API client's guarded agent (#2084): 1.x's API decision (#1312, `API_GUARDED_PERMISSION`)
#: in 2.x's list shape — EVERY tool call asks, reads included, and a subagent is refused (its
#: child session's requests are not shown to the operator, so an ask there could never be
#: answered). Measured on 2.2.7 over ACP under a permissive global AND project config, with the
#: project pre-declaring the same agent name allow-all: real `shell` and `write` calls arrived as
#: `session/request_permission`, waited, and a reject left no effect; a `session/load` by the next
#: generation, pinned to ITS agent, asked again.
API_GUARDED_PERMISSIONS: list[dict[str, str]] = [
    _rule("*", "*", "ask"),
    _rule("subagent", "*", "deny"),
]


def api_config_content(agent: str, *, bypass: bool = False) -> str:
    """`OPENCODE_CONFIG_CONTENT` for an `opencode2-api` worker's child: BattleLab's own guarded
    agent (guarded only until #2108's approval: a bypass request is refused), named per generation
    (`opencode.mint_api_agent`), made the default and pinned by the worker after every
    `session/new`/`session/load`. Built by the same helper as the unattended launch's agent.
    The app's own `OPENCODE_CONFIG_CONTENT` is not merged in: the worker's environment is
    sanitized, as for 1.x. No `server` block: 2.x's ACP client decides its listener itself
    (loopback, random port, its own password)."""
    if not isinstance(agent, str) or not _API_AGENT_RE.match(agent):
        raise ValueError("malformed API agent name")
    if bypass is not False:
        # Guarded only (#2108): an allow-all agent is a new blanket grant that waits for the
        # operator's own approval. `kinds.API_BYPASS_KINDS` already refuses it at create; this is
        # the second fence, so no path can build one.
        raise ValueError("opencode2-api runs guarded only; skip permission prompts is not enabled")
    return _agent_config(
        None,
        agent,
        API_GUARDED_PERMISSIONS,
        "BattleLab API client: every tool call asks the operator; subagents are refused",
    )


class OpenCode2Provider:
    """opencode 2.x: top-level sessions in ``session_v2`` of its OWN database
    (``<root>/data/opencode/opencode.db``), resumed via ``opencode --standalone <dir> --session
    <id>``. Read-only and fail-soft, like the 1.x reader."""

    engine_id = "opencode2"
    id_pattern = base._SES_RE

    # --- the database -------------------------------------------------------------------------

    def _db(self) -> Path:
        return Path(base._opencode2_db())

    def _query_rows(self) -> list:
        """Top-level sessions, RAISING on a real read failure. An absent db is a valid empty."""
        db = self._db()
        try:
            os.stat(db)
        except FileNotFoundError:
            return []
        cols = ", ".join(OPENCODE2_SCHEMA)
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
        try:
            con.execute("PRAGMA busy_timeout=500")
            return con.execute(
                f"SELECT {cols} FROM session_v2 WHERE parent_id IS NULL"  # noqa: S608 fixed cols
            ).fetchall()
        finally:
            con.close()

    def _query(self) -> list:
        try:
            return self._query_rows()
        except (sqlite3.Error, OSError):
            return []

    def _db_readable(self) -> bool:
        db = self._db()
        if not db.exists():
            return False
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
            try:
                con.execute("SELECT 1 FROM session_v2 LIMIT 1")
                return True
            finally:
                con.close()
        except sqlite3.Error:
            return False

    def store_present(self) -> bool:
        return self._db_readable()

    def is_present(self) -> bool:
        return self.store_present()

    # --- isolation ----------------------------------------------------------------------------

    def agent_env(self) -> dict[str, str]:
        """The environment every process for this agent MUST run with (see module docstring)."""
        owner = getattr(self, "owner", None)
        if owner is None:
            raise base.EngineError(f"{self.engine_id}: the store kind is not attached")
        root = owner.store_root()
        if root is None:
            raise base.EngineError(f"{self.engine_id}: declares no store root")
        return isolation_env(root, owner.store_path("db"), owner.home)

    def _bin(self) -> str:
        """argv[0], through the owning provider's provenance-checked entrypoint (#853 §2b)."""
        owner = getattr(self, "owner", None)
        if owner is None:
            raise base.EngineError(
                f"{self.engine_id}: no entrypoint — the store kind is not attached"
            )
        return owner.entrypoint_path()

    # --- unattended launch (#2083, the #989 contract) -----------------------------------------

    def unattended_launch(self, native_id, *, cwd, bypass, env):
        """``(argv, env_overrides)`` for an unattended 2.x launch.

        * ``--standalone``: a private server, never the shared background service.
        * ``"."``, never the cwd's name: the dispatcher spawns in the directory it opened as a
          descriptor, and a path argument would reopen the operator's folder BY NAME after every
          check that pinned it (1.x's rule, `OpenCodeProvider.unattended_launch`).
        * ``--auto`` only on the operator's bypass choice. Without it a guarded launch is ENFORCED
          by `_guarded_config_content`, measured on 2.2.7 to hold under permissive project and
          global configuration; a `ValueError` there refuses the launch.

        The forced XDG tree is not returned here: the dispatcher lays `forced_env` over the whole
        spawn environment, last, as every seam does. Shell-free: the policy rides in the env.
        """
        overrides: dict[str, str] = {}
        if not bypass:
            overrides[OPENCODE_CONFIG_CONTENT_ENV] = _guarded_config_content(
                env.get(OPENCODE_CONFIG_CONTENT_ENV), mint_unattended_agent()
            )
        return [self._bin(), "--standalone", ".", *(["--auto"] if bypass else [])], overrides

    def unattended_preflight(self, *, cwd, probe=None, gate=None):
        """Will 2.x come up able to act, in this directory, with nobody watching?

        ``opencode models --standalone`` under the forced XDG tree. Measured on 2.2.7 (#2083):

        * it runs a private ``serve --stdio --port 0`` and leaves nothing behind;
        * 1.x's ``auth list`` WITHOUT ``--standalone`` starts the shared ``serve --service`` and
          leaves it running, so that argv is never used; with ``--standalone`` it is safe but
          cannot refuse: 2.x's free default model works with ZERO credentials, so a credential
          count says nothing about whether the agent can act;
        * ``models`` lists the models 2.x can run (13 free ``opencode/*`` ones with no
          credentials), and prints nothing when every provider is unavailable.

        So: one or more lines, every one ``provider/model`` → ``ok``; nothing → ``refused`` (no
        model to run on); anything else, or a probe that does not complete → ``unknown``, which
        the dispatcher refuses. `detail` never carries probe output. The probe's own boot line in
        this directory predates the launch floor, so `start_evidence` never counts it.
        """
        from .. import engine_auth
        from ..plugins.provider import forced_env

        try:
            # Forced LAST, over the app's environment: the probe is a 2.x process like any other.
            env = {**os.environ, **forced_env(getattr(self, "owner", None))}
        except base.EngineError as e:
            return base.PREFLIGHT_REFUSED, str(e)
        completed, out = engine_auth.run_probe(
            [self._bin(), "models", "--standalone"], cwd=cwd, env=env, probe=probe, gate=gate
        )
        if not completed:
            return base.PREFLIGHT_UNKNOWN, "the model probe did not complete within its timeout"
        lines = [x.strip() for x in out.splitlines() if x.strip()]
        if not lines:
            return base.PREFLIGHT_REFUSED, "opencode 2 lists no model it can run on this host"
        if all(_MODEL_LINE.match(x) for x in lines):
            return base.PREFLIGHT_OK, f"opencode 2 lists {len(lines)} model(s) it can run"
        return base.PREFLIGHT_UNKNOWN, "the model probe returned output this code cannot read"

    def start_evidence(self, launch):
        """Did a 2.x agent START in this launch's directory, before any byte is typed?

        Not the session store: 2.x writes no ``session_v2`` row before the first message (the
        #916 deadlock, as for 1.x). The artifact is the server's ``location services booted``
        record in 2.x's own log, written on boot (see the block comment above). 1.x's answers and
        guarantees, field-keyed for 2.x's line:

        * one record at or after the launch whose ``directory`` RESOLVES to the launch cwd, and
          this launch's own dtach master alive → ``found``; master dead or unprobeable →
          ``unreadable``;
        * none yet → ``absent`` (the caller polls); two or more → ``unreadable`` (the log carries
          no pid to tell launches apart);
        * the bounded tail cannot reach back to the launch, the file is truncated or replaced, a
          complete line in the window does not parse, or a log newer than the launch does not
          begin with a process's first record → ``unreadable``.

        **The floor has no backward tolerance**, unlike 1.x's 2 s: `launched_at` is taken before
        the spawn and the server boots ≥ 1 s after it, so a record stamped (ms, truncated) before
        `launched_at` is never this launch's — and the preflight's own server boots in the SAME
        directory moments before (`unattended_preflight`), which a backward window would count.

        Not proved, as for 1.x: that the single record is OURS rather than an operator's 2.x
        opened in the same folder meanwhile — but BattleLab's 2.x store is its own, written only
        by processes it starts, and the session is bound by the nonce proof, never by this line.
        """
        from .. import ptybridge
        from .. import start_evidence as _se

        want = _resolved(str(launch.cwd))
        floor = math.floor(float(launch.launched_at or 0.0) * 1000) / 1000
        try:
            lines = _log_tail_since(Path(base._opencode2_log()), floor)
        except _LogUnreadable as e:
            return _se.UNREADABLE, f"opencode 2: {e}"
        # The last element follows the final newline: empty, or a record still being written.
        records = []
        for line in lines[:-1]:
            if not line.strip():
                continue
            fields = _log2_fields(line)
            ts = _log2_time(fields) if fields is not None else None
            if fields is None or ts is None:
                return _se.UNREADABLE, "a line in opencode 2's log could not be parsed"
            records.append((ts, fields))
        if records and min(ts for ts, _ in records) >= floor:
            # The window reached the start of the file (the tail stops growing only once it holds
            # a line older than the floor) and the whole file is newer than the launch.
            if records[0][1].get("message") != LOG_FIRST_MESSAGE:
                return _se.UNREADABLE, (
                    "opencode 2's log begins after this launch, and not with a process's first "
                    "record, so it was truncated or replaced and may have lost this launch's line"
                )

        hits = 0
        for ts, fields in records:
            if fields.get("message") != LOG_BOOT_MESSAGE or ts < floor:
                continue
            directory = fields.get("directory")
            if not directory:
                return _se.UNREADABLE, "a boot record in opencode 2's log names no directory"
            if _resolved(directory) == want:
                hits += 1

        if hits == 1:
            master = _launch_master_state(launch)
            if master == ptybridge.ALIVE:
                return _se.FOUND, "opencode 2 booted a server in this directory"
            if master == ptybridge.DEAD:
                return _se.UNREADABLE, (
                    "an opencode 2 server booted in this directory, but this launch's own "
                    "process is no longer running, so it was not this launch's"
                )
            return _se.UNREADABLE, (
                "an opencode 2 server booted in this directory, but whether this launch's own "
                "process is still running could not be established"
            )
        if hits == 0:
            return _se.ABSENT, "opencode 2 has not booted a server in this directory yet"
        return _se.UNREADABLE, (
            f"{hits} opencode 2 servers booted in this directory since the launch, and the log "
            "carries no pid to tell them apart"
        )

    def bind_session(self, launch):
        """Which 2.x session this launch became: by the nonce proof (`launch_binding`), read
        through the strict 2.x transcript reader. Measured on 2.2.7 (#2083): a bracketed paste of
        the envelope is stored verbatim (plus one trailing space, which the reader strips), its
        last line the attempt's ``[mission attempt <nonce>]``. Never "the one new row"."""
        from .. import launch_binding

        return launch_binding.bind_by_nonce(self, launch)

    # --- rows ---------------------------------------------------------------------------------

    def _row(self, record) -> Session | None:
        sid, _parent, directory, title, time_created, time_updated, time_archived = record
        if not isinstance(sid, str) or not self.id_pattern.match(sid):
            return None
        if is_ephemeral_cwd(directory or ""):
            return None
        return Session(
            engine=self.engine_id,
            uuid=sid,
            cwd=directory or "",
            last_mtime=(time_updated or 0) / 1000.0,  # epoch ms, like 1.x
            first_user_message=title or "",
            archived=time_archived is not None,
            created_at=(time_created or time_updated or 0) / 1000.0,
        )

    def _build(self, records) -> list[Session]:
        rows = (self._row(record) for record in records)
        return [row for row in rows if row is not None]

    def scan(self) -> list[Session]:
        return self._build(self._query())

    def scan_checked(self) -> tuple[list[Session], list[str]]:
        try:
            rows = self._query_rows()
        except (sqlite3.Error, OSError) as e:
            return [], [f"{self.engine_id}: its database could not be read ({type(e).__name__})"]
        return self._build(rows), []

    def lookup(self, native_id: str) -> Session | None:
        if not self.id_pattern.match(native_id or ""):
            return None
        db = self._db()
        if not db.exists():
            return None
        cols = ", ".join(OPENCODE2_SCHEMA)
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=0.5)
            try:
                con.execute("PRAGMA busy_timeout=500")
                record = con.execute(
                    f"SELECT {cols} FROM session_v2 WHERE id = ? AND parent_id IS NULL",  # noqa: S608 fixed cols
                    (native_id,),
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            return None
        return self._row(record) if record is not None else None

    # --- new-session adoption (the engine mints its own `ses_` id) ----------------------------

    def snapshot_session_ids(self, cwd: str) -> set[str] | None:
        """Top-level ids in ``cwd`` before a launch, or ``None`` when the read FAILED (a failed
        read must never look like an empty baseline — the 1.x rule, #127)."""
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
        """The one new id in ``cwd`` since ``snapshot``; a list when ambiguous; None if none yet."""
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
            return new_ids
        return new_ids[0]

    # --- sidecar-only mutations ---------------------------------------------------------------

    def archive(self, native_id):
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id):
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)
