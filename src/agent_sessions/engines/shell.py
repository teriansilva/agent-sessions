"""Shell engine — "terminal as agent" (#636).

A plain interactive **login shell** with no agent behind it. It is a first-class engine so it
shows up in the new-session picker, the sidebar, the engine filter and the overview, and — the
whole point — it is AI-reviewed like every agent session. There is no transcript to read, so the
reviewer runs on the live terminal *screen* alone: ``review.gather_input`` already builds its
payload from the transcript AND ``scrollback.live_tail_text`` and only fails when both are empty,
and ``_plain_transcript`` fail-softs to "" when an engine registers no transcript adapter. Shell
registers none, so it reviews on screen — that IS "terminal as agent".

Two things make this a thin engine rather than a subsystem:

- **Pinned id.** A shell has no store that mints an id, so the client mints a UUID and we launch
  under ``shell:<uuid>`` — that key is final at launch (``new_session_reconciles`` is absent →
  falsey), so no reconcile dance.
- **Own record store.** With no native JSONL/SQLite to scan, the provider persists one tiny JSON
  record per session under ``base._shell_dir()`` (``on_new_session``) and ``scan`` reads them
  back. Archive rides the engine-agnostic metadata sidecar, exactly like gemini/codex/opencode.

Shell-free launcher contract (the repo's load-bearing security property): ``launch_argv`` returns
a **literal argv list** — the bash *binary* as argv[0] with a literal ``-l`` flag, never a command
string handed to an interpreter. cwd is applied by the pty bridge as the child's working dir, not
interpolated here. So none of the forbidden shell-layer patterns appear.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import time
from pathlib import Path

from .. import metadata as _metadata
from ..scanner import Session, fs_created_at
from . import base


class ShellProvider:
    """A bare ``bash -l`` login shell as a reviewable session (#636). No agent; no transcript;
    reviewed on its live screen. Pinned id, own record store, sidecar archive."""

    engine_id = "shell"
    id_pattern = base._SHELL_UUID_RE
    supports_new = True  # ws new-session via new_launch_argv; pinned id (no reconcile)
    # NEVER orchestrator-actuable (#726): this is a bare `bash -l` with no agent, so a
    # server-authored "continue" nudge would be EXECUTED as a shell command. The registry
    # predicate already default-denies; this is explicit so the reason is at the site.
    supports_orchestrator_input = False

    def is_present(self) -> bool:
        # Always usable where bash exists (effectively every Linux host); also present when the
        # record store has rows so archived shell sessions still list even on an odd host.
        return shutil.which("bash") is not None or base._shell_dir().is_dir()

    # --- record store ----------------------------------------------------------------------

    def _record_path(self, native_id: str) -> Path | None:
        # Guard the filename against anything but our own UUID shape (defense in depth — callers
        # pass a parse_key-validated id, but the store must never build a path from junk).
        if not self.id_pattern.match(native_id):
            return None
        return base._shell_dir() / f"{native_id}.json"

    def on_new_session(self, native_id: str, *, cwd: str) -> None:
        """Persist a record for a freshly-launched shell so ``scan`` lists it. Called from the
        pinned-id new-session path AFTER cwd validation. The caller wraps this best-effort (a
        sidecar write must never block the terminal); the write itself is atomic so a crash can't
        leave a half-written record."""
        path = self._record_path(native_id)
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"id": native_id, "cwd": cwd, "created_at": time.time()}
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(rec), encoding="utf-8")
        tmp.replace(path)

    def on_new_session_failed(self, native_id: str) -> None:
        """Drop the record written by ``on_new_session`` when the launch that followed it was
        rejected — so a failed new session leaves no phantom row. Best-effort."""
        path = self._record_path(native_id)
        if path is None:
            return
        with contextlib.suppress(OSError):
            path.unlink()

    def scan(self) -> list[Session]:
        root = base._shell_dir()
        out: list[Session] = []
        try:
            files = sorted(root.glob("*.json"))
        except OSError:
            return out
        for path in files:
            try:
                rec = json.loads(path.read_text(encoding="utf-8"))
                st = path.stat()
            except (OSError, json.JSONDecodeError):
                continue  # fail-soft per record: a bad file skips its row, never the whole list
            if not isinstance(rec, dict):
                continue
            sid, cwd = rec.get("id"), rec.get("cwd")
            if not (isinstance(sid, str) and self.id_pattern.match(sid)):
                continue
            if not (isinstance(cwd, str) and cwd):
                continue
            created = rec.get("created_at")
            usable = (
                isinstance(created, int | float) and not isinstance(created, bool) and created > 0
            )
            created_at = float(created) if usable else fs_created_at(st)
            out.append(
                Session(
                    engine=self.engine_id,
                    uuid=sid,
                    cwd=cwd,
                    last_mtime=st.st_mtime,
                    # No transcript, so no first message; the title comes from the AI review's
                    # ai_title (from the screen) or a manual rename via the sidecar.
                    first_user_message="",
                    # Sidecar override in the row builder decides the effective archive state.
                    archived=False,
                    created_at=created_at,
                )
            )
        return out

    # --- launch ----------------------------------------------------------------------------

    def launch_argv(self, native_id: str, *, cwd: str, bypass: bool) -> list[str]:
        # A plain interactive login shell as a LITERAL argv: the bash binary as argv[0] plus a
        # literal login flag — never a command string handed to an interpreter (the repo's
        # shell-free launcher contract). cwd is the pty bridge's job; `bypass` is meaningless for
        # a shell (no permission model) and ignored.
        return [base.BASH_BIN, "-l"]

    def new_launch_argv(self, native_id: str, *, cwd: str, bypass: bool) -> list[str]:
        # Pinned id: the caller's UUID is only our bookkeeping key; a fresh shell needs no id
        # passed to bash. Same literal argv as resume — a relaunch is a fresh shell (there is no
        # transcript to restore), which is the honest behaviour for a bare terminal.
        return self.launch_argv(native_id, cwd=cwd, bypass=bypass)

    # --- archive (engine-agnostic sidecar, like gemini/codex/opencode) ---------------------

    def archive(self, native_id: str) -> None:
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id: str) -> None:
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)
