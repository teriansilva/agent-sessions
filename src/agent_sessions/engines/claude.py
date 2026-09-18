"""Claude Code engine provider (split out of the single-file ``engines.py``, #265 S1)."""

from __future__ import annotations

import shutil
from pathlib import Path

from .. import archive as _archive
from .. import metadata as _metadata
from .. import scanner
from ..scanner import Session
from . import base


class ClaudeProvider:
    """Claude Code: sessions under ``~/.claude/projects``, resumed via ``claude --resume``.

    Delegates to the existing ``scanner`` / ``archive`` modules — this provider is a
    thin adapter, so the Claude behavior is byte-for-byte what it was before the
    abstraction.
    """

    engine_id = "claude"
    id_pattern = base._CLAUDE_UUID_RE
    supports_new = True  # ws new-session via new_launch_argv
    supports_orchestrator_input = True  # a TUI agent that reads a prompt (#726)
    expects_raw_tty = True  # ratatui/Ink TUI: its PTY must stay raw (#804)
    # Cross-engine handoff target (#597): a fresh claude TUI accepts the seed as a bracketed
    # paste on its PTY input (never argv — the shell-free/no-argv-seed contract).
    supports_seed_start = True

    def is_present(self) -> bool:
        return (Path.home() / ".claude" / "projects").is_dir() or shutil.which("claude") is not None

    def scan(self) -> list[Session]:
        # scanner is Claude-only today; filter defensively so this stays correct
        # if a future scanner ever yields more than one engine.
        return [s for s in scanner.scan() if s.engine == self.engine_id]

    def scan_checked(self) -> tuple[list[Session], list[str]]:
        """``scan()``'s rows plus unreadable-transcript problems, through the scanner's checked
        walk (#993). The fail-soft ``scan`` above is untouched — the sidebar keeps it."""
        rows, problems = scanner.scan_checked()
        return [s for s in rows if s.engine == self.engine_id], problems

    def lookup(self, native_id: str) -> Session | None:
        """This one session, read fresh (#991) — exactly the row ``scan`` would return for it,
        through the scanner's own per-file builder, or ``None``."""
        row = scanner.lookup(native_id)
        return row if row is not None and row.engine == self.engine_id else None

    def archive_state(self, native_id: str) -> str:
        """`archived` · `not-archived` · `unreadable` (#896 review 24, finding 1).

        Claude is the one engine that also MOVES the transcript on archive, so its tree can hold
        the fact the sidecar does not. `scan()` cannot answer the NEGATIVE half of that question:
        `scanner._walk` swallows a per-file `stat` failure and a whole unreadable tree raises past
        it, so "no archived row" and "could not look" arrive identically.

        Deliberately NOT an existence check. This asks only whether the archive tree holds a
        transcript for this id, and it uses the scanner's own semantic read to decide — a
        `claude -p` one-shot (`entrypoint: "sdk-cli"`) is not a session anyone can attach to, so
        one sitting in the tree is not an archived session either.

        `stat`, not `glob` or `is_file`: both of those swallow a permission error and answer "no
        such file", which is the conflation this method exists to remove. Only `ENOENT` means
        "not here".
        """
        if not self.id_pattern.match(native_id or ""):
            return "not-archived"
        root = Path.home() / ".claude" / "projects-archive"
        try:
            if not root.is_dir():
                return "not-archived"
            dirs = list(root.iterdir())
        except OSError:
            return "unreadable"
        for project_dir in dirs:
            jsonl = project_dir / f"{native_id}.jsonl"
            try:
                jsonl.stat()
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError:
                return "unreadable"
            try:
                _cwd, _first, headless = scanner._read_session_meta(jsonl)
            except OSError:
                return "unreadable"
            # A one-shot transcript is not a session, so it is not an ARCHIVED session either.
            return "not-archived" if headless else "archived"
        return "not-archived"

    def launch_argv(self, native_id, *, cwd, bypass):
        # Resume command for the per-session PTY bridge (issue #49); cwd is set by the
        # launcher, not an argv arg here.
        argv = [base.CLAUDE_BIN, "--resume", native_id]
        if bypass:
            argv.append("--dangerously-skip-permissions")
        return argv

    def new_launch_argv(self, native_id, *, cwd, bypass):
        # Start a *new* claude session with our pre-generated id (`--session-id`),
        # so the bridge can key it before claude has written its JSONL.
        argv = [base.CLAUDE_BIN, "--session-id", native_id]
        if bypass:
            argv.append("--dangerously-skip-permissions")
        return argv

    def archive(self, native_id):
        # Move the JSONL into the archive tree AND record the archived flag in the
        # engine-agnostic sidecar (#194). The file move alone is not durable: a still-running
        # ``claude`` process recreates its JSONL under ``projects/`` on its next write, which
        # the scanner would then report as live. ``_row`` lets the sidecar override win over
        # the on-disk tree, so the session stays archived regardless. Mirrors OpenCodeProvider.
        _archive.archive(native_id)
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id):
        # Move back to the live tree and clear the sidecar flag so the effective state is
        # "live" again (else the sticky archived override from archive() would keep hiding it).
        _archive.unarchive(native_id)
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)
