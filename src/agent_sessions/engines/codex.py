"""codex engine provider (split out of the single-file ``engines.py``, #265 S1)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from .. import metadata as _metadata
from .. import scancache
from ..scanner import Session, derive_created_at
from . import base

# rollout-<iso-ts>-<uuid>.jsonl  →  capture the trailing uuid
#: How far into a rollout ``_meta`` will look for the first real ``user_message`` (#1048).
#:
#: Measured across all 170 rollouts on the author's install: of the 146 that contain a
#: ``user_message`` at all, the **latest** one appeared at record 11 (p50 = 10, max = 11) — codex
#: writes ``session_meta`` first and the operator's turn immediately after. The other 24 contain
#: none, and those were read **cover to cover on every walk**: 77 MB, 50 MB, 32 MB files parsed in
#: full to conclude "no title here", because the ``role:"user"`` fallback below never breaks the
#: loop. That was 2,256 ms of a 2,433 ms codex scan.
#:
#: 512 is ~47x the observed maximum, so it cannot plausibly change any title on a real rollout,
#: and it turns the pathological case from "the whole file" into a bounded read. The fallback is
#: unchanged: it is still accepted when the scan ends without a ``user_message`` event — the only
#: difference is that the scan can now end at the bound as well as at EOF.
FIRST_USER_SCAN_RECORDS = 512

_CODEX_ROLLOUT_RE = re.compile(
    r"rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)

# Machine-context markers (#670): codex injects context as plain ``role:"user"``
# response_items — ``<environment_context>`` / ``<user_instructions>`` (observed ≤ 0.128)
# and the ``# AGENTS.md instructions for <cwd>`` preamble (≥ 0.142.5). This prefix rule is now
# only the ``user_message`` path's (see :func:`_is_injected_user_message`); response_items go
# through :func:`is_injected_context`, which is shared with
# ``transcript._codex_turns_from_records`` so a marker can never be filtered from titles while
# still polluting the AI-review / recap input (or vice versa).
_INJECTED_CONTEXT_PREFIXES = (
    "<environment_context",
    "<user_instructions",
    "# AGENTS.md instructions",
)

#: Every tagged block codex is KNOWN to inject as a ``role:"user"`` response_item (#1051).
#:
#: Enumerated from every rollout on the author's install (170 rollouts, 5.6 GB, read-only): the
#: blocks that open a ``response_item`` user message are exactly ``environment_context`` (807),
#: the ``# AGENTS.md instructions`` preamble (172, always followed by one ``<INSTRUCTIONS>``
#: block), ``recommended_plugins`` (45) and ``turn_aborted`` (6). ``user_instructions`` is kept
#: from #670 (codex ≤ 0.128). Nothing else occurs.
#:
#: A closed list on purpose. A rule that accepted ANY tag name hid ``<task>…</task>``,
#: ``<div><p>…</p></div>``, ``<b>fix it</b>`` and an image-only ``<image …> </image>`` turn —
#: operator input, dropped from the transcript the AI review, recap and pulse read. A new codex
#: tag costs one line here; a false positive silently loses what the operator said.
_MACHINE_CONTEXT_TAGS = frozenset(
    {"environment_context", "user_instructions", "recommended_plugins", "turn_aborted"}
)

#: The AGENTS.md preamble, as codex writes it: this header line (bare, or ``… for <path>``), then
#: whitespace, then ONE ``<INSTRUCTIONS>…</INSTRUCTIONS>`` block. Measured: 167 carry ``for``, 5
#: are bare, all 172 continue into exactly one ``<INSTRUCTIONS>`` block.
_AGENTS_MD_HEADER = "# AGENTS.md instructions"
_AGENTS_MD_BODY_OPEN = "<INSTRUCTIONS>"
_AGENTS_MD_BODY_CLOSE = "</INSTRUCTIONS>"


def _skip_space(s: str, i: int) -> int:
    n = len(s)
    while i < n and s[i].isspace():
        i += 1
    return i


def _known_tag_block_end(s: str, i: int) -> int:
    """End index of a known machine block opening at ``s[i]``, or -1."""
    for name in _MACHINE_CONTEXT_TAGS:
        opener = f"<{name}>"
        if s.startswith(opener, i):
            close = f"</{name}>"
            end = s.find(close, i + len(opener))
            return -1 if end < 0 else end + len(close)
    return -1


def _agents_md_block_end(s: str, i: int) -> int:
    """End index of an AGENTS.md preamble opening at ``s[i]``, or -1.

    Prose that merely starts with the header words — ``# AGENTS.md instructions are wrong`` — is
    not the preamble: the header must be the WHOLE line (optionally ``… for <path>``), and the
    next non-blank text must be the ``<INSTRUCTIONS>`` block, closed.
    """
    if not s.startswith(_AGENTS_MD_HEADER, i):
        return -1
    eol = s.find("\n", i)
    if eol < 0:
        return -1
    rest = s[i + len(_AGENTS_MD_HEADER) : eol].rstrip()
    if rest and not (rest.startswith(" for ") and rest[5:].strip()):
        return -1
    j = _skip_space(s, eol)
    if not s.startswith(_AGENTS_MD_BODY_OPEN, j):
        return -1
    end = s.find(_AGENTS_MD_BODY_CLOSE, j + len(_AGENTS_MD_BODY_OPEN))
    return -1 if end < 0 else end + len(_AGENTS_MD_BODY_CLOSE)


def _is_only_machine_sections(text: str) -> bool:
    """True when ``text`` is NOTHING BUT known machine-context blocks and whitespace (#1051).

    The old prefix list is ordering-sensitive by construction — it only sees whichever marker
    codex puts first — and codex has reordered this block twice. Measured on the author's install:
    a newer CLI emits ``<recommended_plugins>`` *ahead of* ``<environment_context>`` (45 records),
    and ``<turn_aborted>`` (6 records) contains no old marker at all. So the check is
    order-independent: every section must be one of :data:`_MACHINE_CONTEXT_TAGS` or the
    AGENTS.md preamble, in any order, and the real payloads interleave them — the transcript
    adapter joins a record's content blocks into one string, giving ``<recommended_plugins>…
    </recommended_plugins> # AGENTS.md instructions … <INSTRUCTIONS>…</INSTRUCTIONS>
    <environment_context>…</environment_context>``.

    Anything else anywhere — prose, an unknown tag, an unclosed known tag — makes it a human turn.

    A linear scan, not a regex: these payloads run to tens of kilobytes and a backtracking pattern
    over them is the cost #1048 removed. Each step either jumps past a closed block or returns, so
    no character is scanned for a closing tag twice.

    **Cost of a false positive** — it is not just a title. ``is_injected_context`` also filters
    ``transcript._codex_turns_from_records``, so a turn this accepts disappears from the transcript
    viewer, the AI review input (``review.gather_input``), the recap, pulse chat and handoff. That
    is why the tag set is closed rather than "any tag-shaped block". A human turn is lost only if
    it is made SOLELY of these exact codex blocks.
    """
    s = text.strip()
    if not s:
        return False
    i, n = 0, len(s)
    while i < n:
        end = _known_tag_block_end(s, i) if s[i] == "<" else _agents_md_block_end(s, i)
        if end < 0:
            return False
        i = _skip_space(s, end)
    return True


def is_injected_context(text: str) -> bool:
    """True when a codex ``role:"user"`` response_item text is injected machine context.

    Shared by the title fallback and ``transcript._codex_turns_from_records``. See
    :func:`_is_only_machine_sections` for the rule and what a false positive costs.
    """
    return _is_only_machine_sections(text)


def _is_injected_user_message(text: str) -> bool:
    """The ``user_message`` event path's rule — the pre-#1051 prefix match, unchanged.

    ``user_message`` events are what the operator typed: of 3,501 in the author's corpus, none is
    machine context under any rule; codex delivers its context only as response_items. Giving this
    path the broader shape rule could only ever hide real input and silently promote a later
    prompt to the title, so it keeps the rule it always had.
    """
    return text.startswith(_INJECTED_CONTEXT_PREFIXES)


def is_subagent_meta(payload: dict) -> bool:
    """True when a ``session_meta`` payload describes a SPAWNED SUBAGENT thread (#821).

    codex ≥ 0.145 (``multi_agent_version: v2``) gives every subagent its own rollout file,
    whose meta carries the **parent's** ``cwd`` *and* the parent's conversation head — so an
    unfiltered scan renders one real session as N near-identical sidebar rows. Two independent
    markers, both observed on 0.145.0 and 0.147.0::

        "source": {"subagent": {"thread_spawn": {…, "agent_nickname": "Galileo"}}}
        "thread_source": "subagent"

    Either alone is enough, so renaming one in a future codex can't quietly resurrect the rows.
    A top-level session instead has a **string** ``source`` (``cli`` / ``exec`` / ``vscode``);
    rollouts predating the fields have neither, and absent means top-level.

    Deliberately NOT keyed on ``parent_thread_id`` / ``forked_from_id``: a plain fork or a
    compaction of a REAL session sets those too, and hiding one of those would lose a session.
    """
    if payload.get("thread_source") == "subagent":
        return True
    source = payload.get("source")
    return isinstance(source, dict) and "subagent" in source


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
    expects_raw_tty = True  # ratatui/Ink TUI: its PTY must stay raw (#804)
    # codex (like opencode) mints its OWN session id at launch — there is no caller-chosen
    # ``--session-id`` flag — so new-session launches under a ``new-<uuid>`` placeholder and
    # reconciles to the real rollout uuid afterwards, rather than pinning the id like claude.
    new_session_reconciles = True
    # Cross-engine handoff target (#597): the fresh codex TUI accepts the seed as a bracketed
    # paste on its PTY input (never argv).
    supports_seed_start = True

    def is_present(self) -> bool:
        return base._codex_sessions_dir().is_dir() or shutil.which("codex") is not None

    def _meta(self, path: Path, *, checked: bool = False) -> tuple[str, str] | None:
        """``(cwd, first_user_message)`` from one rollout file. Single pass, best-effort.

        The prompt comes from the first ``user_message`` EVENT payload — the record codex
        emits only for real user input (stable across every observed version, 0.128 →
        0.144). Plain ``role:"user"`` response_items open with injected machine context
        (#670: the AGENTS.md / environment preamble), so the first non-injected one is
        only a FALLBACK candidate: it never stops the scan, and is used at EOF when the
        rollout carries no user_message event. The message is returned RAW — it feeds the
        ``/api/sessions`` search haystack; ``metadata.display_title`` normalizes it into
        the bounded sidebar title (Hermes on PR #672).

        ``None`` also means "this rollout is not a listable session" — which is how the
        subagent exclusion (#821) reaches BOTH readers at once: ``scan`` (no row) and
        ``_rollout_uuids_in_cwd`` (never adopted as a new session's id), with no second
        call site to drift.
        """
        cwd = first_user = fallback = ""
        scanned = 0
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    scanned += 1
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    payload = record.get("payload") or {}
                    if not isinstance(payload, dict):
                        continue
                    # A spawned subagent thread is not a session (#821). Checked on the
                    # session_meta record only — that's where codex writes the markers —
                    # and before the cwd capture, since the two live on the same record.
                    if record.get("type") == "session_meta" and is_subagent_meta(payload):
                        return None
                    if not cwd and payload.get("cwd"):
                        cwd = str(payload["cwd"])
                    if not first_user and payload.get("type") == "user_message":
                        text = _codex_text(payload.get("message"))
                        if text and not _is_injected_user_message(text):
                            first_user = text
                    elif not fallback and payload.get("role") == "user":
                        text = _codex_text(payload.get("content"))
                        if text and not is_injected_context(text):
                            fallback = text
                    if cwd and first_user:
                        break
                    # A rollout with no ``user_message`` event used to be read to EOF (#1048). The
                    # `cwd` is on record 0 and a real first turn is within 11, so past the bound
                    # there is nothing left to find — only bytes to parse.
                    if scanned >= FIRST_USER_SCAN_RECORDS:
                        break
        except OSError:
            # `checked` selects the failure policy, never the parse: a rollout a measurement
            # cannot READ must be named rather than counted as "not a listable session"
            # (review 4951, P2). Malformed lines are skipped under both, as above.
            if checked:
                raise
            return None
        # cwd is the one required field: it's the launch dir + the open-path
        # allowlist key. A rollout with no usable cwd (corrupt-only, or not a real
        # session) yields no row rather than a bogus empty-cwd session.
        return (cwd, first_user or fallback) if cwd else None

    def _row_from_path(self, path: Path, *, checked: bool = False) -> Session | None:
        """One row from one rollout file, or ``None`` if it is not a listable session. Shared by
        ``scan`` and ``lookup`` (#991) so both apply the same filters (subagents, no cwd).

        ``checked`` propagates a failed READ — the rollout itself and its ``stat`` — instead of
        turning it into a missing row (review 4951, P2)."""
        m = _CODEX_ROLLOUT_RE.search(path.name)
        if not m:
            return None
        meta = self._meta(path, checked=checked)
        if meta is None:
            return None
        try:
            st = path.stat()
        except OSError:
            if checked:
                raise
            return None
        cwd, first_user = meta
        return Session(
            engine=self.engine_id,
            uuid=m.group(1),
            cwd=cwd,
            last_mtime=st.st_mtime,
            first_user_message=first_user,
            archived=False,
            created_at=derive_created_at(path, st),
        )

    def scan(self) -> list[Session]:
        root = base._codex_sessions_dir()
        out: list[Session] = []
        try:
            files = list(root.rglob("rollout-*.jsonl"))
        except OSError:
            return out
        for path in files:
            # Memoised per file (#1048): a rollout whose identity is unchanged cannot produce a
            # different row. `scan_checked` below stays off the memo — it preserves read failures.
            row = scancache.memoized(
                "codex",
                path,
                lambda p=path: self._row_from_path(p),
            )
            if row is not None:
                out.append(row)
        return out

    def scan_checked(self) -> tuple[list[Session], list[str]]:
        """``scan()``'s rows, plus a line for each rollout that could not be READ (#993).

        ``rglob`` above swallows a permission error on any directory in the tree and simply yields
        fewer files, which a measurement that authorises deletion would read as "nothing archived".
        The checked walk raises on an unreadable directory and names an unreadable file, while
        keeping every rollout that read cleanly.
        """
        paths = [
            Path(e.path)
            for e in base.scandir_checked(base._codex_sessions_dir(), recurse=True)
            if e.is_file(follow_symlinks=False) and _CODEX_ROLLOUT_RE.search(e.name)
        ]
        return base.checked_rows(
            sorted(paths),
            lambda p: self._row_from_path(p, checked=True),
            engine_id=self.engine_id,
        )

    def lookup(self, native_id: str) -> Session | None:
        """This one session, read fresh (#991), or ``None``.

        The rollout's file name carries the uuid, so this is a name match — the dated
        ``YYYY/MM/DD`` layout first, then, if none of those is a usable session, a name-only walk
        for a copy stored anywhere else, which ``scan``'s ``rglob`` would also have found. A dated
        match that is not listable (no cwd, a subagent) must not hide a valid copy elsewhere
        (#991 review). Only files whose name matches are read."""
        if not self.id_pattern.match(native_id or ""):
            return None
        root = base._codex_sessions_dir()
        pattern = f"rollout-*-{native_id}.jsonl"
        tried: set[Path] = set()
        try:
            for candidates in (
                lambda: sorted(root.glob(f"*/*/*/{pattern}")),
                lambda: sorted(root.rglob(pattern)),
            ):
                for path in candidates():
                    if path in tried:
                        continue
                    tried.add(path)
                    row = self._row_from_path(path)
                    if row is not None and row.uuid == native_id:
                        return row
        except OSError:
            return None
        return None

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
        is excluded, so it stays *pending* rather than being misattributed. So is a **subagent**
        rollout (#821) — a session that spawns one the moment it starts writes two rollouts into
        our cwd inside the poll window, and only one of them is the session we launched. A
        transient walk failure returns ``None`` so the caller skips reconciliation (never
        adopts on a bad read).
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
                continue  # subagent, or cwd not yet readable → excluded (never adopted)
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
