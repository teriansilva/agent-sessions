"""codex engine provider (split out of the single-file ``engines.py``, #265 S1)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .. import metadata as _metadata
from ..scanner import Session, derive_created_at
from . import base

# rollout-<iso-ts>-<uuid>.jsonl  →  capture the trailing uuid
_CODEX_ROLLOUT_RE = re.compile(
    r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)

# Machine-context markers (#670): codex injects context as plain ``role:"user"``
# response_items — ``<environment_context>`` / ``<user_instructions>`` (observed ≤ 0.128)
# and the ``# AGENTS.md instructions for <cwd>`` preamble (≥ 0.142.5). ONE predicate,
# shared with ``transcript._codex_turns_from_records``, so a future marker can never be
# filtered from titles while still polluting the AI-review / recap input (or vice versa).
_INJECTED_CONTEXT_PREFIXES = (
    "<environment_context",
    "<user_instructions",
    "# AGENTS.md instructions",
)


def is_injected_context(text: str) -> bool:
    """True when a codex user-message text is injected machine context, not a human prompt."""
    return text.startswith(_INJECTED_CONTEXT_PREFIXES)


def _codex_text(content) -> str:
    """First text chunk of a codex message ``content`` (str or list of parts)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("text"):
                return str(item["text"]).strip()
    return ""


class CodexProvider:
    """codex: JSONL rollout files under ``~/.codex/sessions/YYYY/MM/DD/
    rollout-<ts>-<uuid>.jsonl``, resumed via ``codex resume <uuid>``.

    File-based like Claude (not a DB like opencode), so discovery mirrors the Claude
    reader: walk the rollout files, take the uuid from the filename, read ``cwd`` +
    the first user message from the records, mtime from the file. **Read-only +
    fail-soft**: a parse/IO error skips that file, never the whole list. Archive is
    not a codex concept, so ``archive``/``unarchive`` raise (surfaced as a 4xx).
    """

    engine_id = "codex"
    id_pattern = base._CODEX_UUID_RE
    supports_new = True  # new-session via launch-then-reconcile (#315)
    supports_orchestrator_input = True  # a TUI agent that reads a prompt (#726)
    # codex (like opencode) mints its OWN session id at launch — there is no caller-chosen
    # ``--session-id`` flag — so new-session launches under a ``new-<uuid>`` placeholder and
    # reconciles to the real rollout uuid afterwards, rather than pinning the id like claude.
    new_session_reconciles = True
    # Cross-engine handoff target (#597): the fresh codex TUI accepts the seed as a bracketed
    # paste on its PTY input (never argv).
    supports_seed_start = True

    def is_present(self) -> bool:
        return base._codex_sessions_dir().is_dir() or shutil.which("codex") is not None

    def _meta(self, path: Path) -> tuple[str, str] | None:
        """``(cwd, first_user_message)`` from one rollout file. Single pass, best-effort.

        The prompt comes from the first ``user_message`` EVENT payload — the record codex
        emits only for real user input (stable across every observed version, 0.128 →
        0.144). Plain ``role:"user"`` response_items open with injected machine context
        (#670: the AGENTS.md / environment preamble), so the first non-injected one is
        only a FALLBACK candidate: it never stops the scan, and is used at EOF when the
        rollout carries no user_message event. The message is returned RAW — it feeds the
        ``/api/sessions`` search haystack; ``metadata.display_title`` normalizes it into
        the bounded sidebar title (Hermes on PR #672).
        """
        cwd = first_user = fallback = ""
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line).get("payload") or {}
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if not cwd and payload.get("cwd"):
                        cwd = str(payload["cwd"])
                    if not first_user and payload.get("type") == "user_message":
                        text = _codex_text(payload.get("message"))
                        if text and not is_injected_context(text):
                            first_user = text
                    elif not fallback and payload.get("role") == "user":
                        text = _codex_text(payload.get("content"))
                        if text and not is_injected_context(text):
                            fallback = text
                    if cwd and first_user:
                        break
        except OSError:
            return None
        # cwd is the one required field: it's the launch dir + the open-path
        # allowlist key. A rollout with no usable cwd (corrupt-only, or not a real
        # session) yields no row rather than a bogus empty-cwd session.
        return (cwd, first_user or fallback) if cwd else None

    def scan(self) -> list[Session]:
        root = base._codex_sessions_dir()
        out: list[Session] = []
        try:
            files = list(root.rglob("rollout-*.jsonl"))
        except OSError:
            return out
        for path in files:
            m = _CODEX_ROLLOUT_RE.search(path.name)
            if not m:
                continue
            meta = self._meta(path)
            if meta is None:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            cwd, first_user = meta
            out.append(
                Session(
                    engine=self.engine_id,
                    uuid=m.group(1),
                    cwd=cwd,
                    last_mtime=st.st_mtime,
                    first_user_message=first_user,
                    archived=False,
                    created_at=derive_created_at(path, st),
                )
            )
        return out

    def launch_argv(self, native_id, *, cwd, bypass):
        # codex resumes by uuid; cwd is set by the launcher. No documented per-launch
        # bypass flag (sandbox/approvals are config / -c driven), so none is added.
        return [base.CODEX_BIN, "resume", native_id]

    def new_launch_argv(self, native_id, *, cwd, bypass):
        # Start a *fresh* codex session in `cwd`. codex mints its own rollout uuid (no
        # ``--session-id``), which the reconcile step discovers afterwards by diffing the
        # rollout files (#315). `native_id` here is the client-minted ``new-<uuid>``
        # placeholder the bridge keys the socket/lock by; codex never sees it. `--cd` sets
        # codex's working dir (its rollout records that cwd, which the reconcile diff filters on).
        argv = [base.CODEX_BIN, "--cd", cwd]
        if bypass:
            # Honor the modal's permission-bypass choice (default on): run without the
            # approval/sandbox gate, matching the picker's "skip permission prompts".
            argv.append("--dangerously-bypass-approvals-and-sandbox")
        return argv

    def _rollout_uuids_in_cwd(self, cwd: str) -> set[str] | None:
        """The set of codex rollout uuids whose recorded ``cwd`` == ``cwd`` (#315), or
        ``None`` if walking the sessions dir FAILED.

        A missing sessions dir is a valid empty baseline (fresh codex) → ``set()``, NOT a
        failure. cwd-scoped so an unrelated new session elsewhere can't be mistaken for ours.
        A rollout whose ``cwd`` head isn't written yet / is malformed (``_meta`` → ``None``)
        is excluded, so it stays *pending* rather than being misattributed. A transient walk
        failure returns ``None`` so the caller skips reconciliation (never adopts on a bad read).
        """
        root = base._codex_sessions_dir()
        if not root.exists():
            return set()
        try:
            files = list(root.rglob("rollout-*.jsonl"))
        except OSError:
            return None
        out: set[str] = set()
        for path in files:
            m = _CODEX_ROLLOUT_RE.search(path.name)
            if not m:
                continue
            meta = self._meta(path)
            if meta is None:
                continue  # cwd not yet readable → excluded (stays pending)
            if meta[0] == cwd:
                out.add(m.group(1))
        return out

    def snapshot_session_ids(self, cwd: str) -> set[str] | None:
        """Rollout uuids already present in ``cwd`` BEFORE launch (#315), or ``None`` on a
        walk failure (the caller then skips reconciliation rather than risk misattributing a
        pre-existing rollout). See :meth:`_rollout_uuids_in_cwd`."""
        return self._rollout_uuids_in_cwd(cwd)

    def reconcile_new_session(self, cwd: str, snapshot: set[str]) -> str | list[str] | None:
        """The codex rollout uuid created in ``cwd`` since ``snapshot`` (#315). Returns:
          * the single new uuid — our session (unambiguous), or
          * a ``list`` of ≥2 new uuids — AMBIGUOUS (two new same-cwd sessions in the poll
            window): the caller must NOT guess (fail-safe — never the wrong session), or
          * ``None`` — codex hasn't written a matching rollout yet (it may not until first
            output): the caller keeps serving under the placeholder and polls again.

        Read-only to codex's rollout files; never mutates them.
        """
        current = self._rollout_uuids_in_cwd(cwd)
        if current is None:
            return None  # transient walk failure → stay on the placeholder
        new_ids = sorted(current - snapshot)
        if not new_ids:
            return None
        if len(new_ids) > 1:
            return new_ids  # ambiguous → caller fails safe
        return new_ids[0]

    def archive(self, native_id):
        # codex rollouts stay read-only; archive flag rides the engine-agnostic sidecar.
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=True)

    def unarchive(self, native_id):
        _metadata.patch(f"{self.engine_id}:{native_id}", archived=False)
