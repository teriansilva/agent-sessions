# Engines

BattleLab does not implement agents. It **organizes** them: six AI coding CLIs plus a plain shell,
each behind a small provider interface, all presented through one session list and one terminal.

## The rule that shapes everything

Every engine handles its own conversation persistence. BattleLab reads each tool's own on-disk
history **read-only** for the sidebar, and "resume" always means launching *that tool's own resume
command* under BattleLab's `dtach` PTY. Nothing is re-implemented and, with one exception, nothing
is written back.

The exception is Claude Code, whose archive moves its JSONL between `projects/` and
`projects-archive/`. Every other engine's archive is a flag in BattleLab's own sidecar — opencode's
SQLite database in particular is opened read-only and never written, which is why renaming,
favouriting and archiving an opencode session change nothing inside opencode.

| Engine | Sessions read from | Resume command | New session | Archive |
|---|---|---|---|---|
| **Claude Code** | `~/.claude/projects/**/*.jsonl` | `claude --resume <uuid>` | ✓ pins a caller id | moves the JSONL + sidecar flag |
| **Codex** | `~/.codex/sessions/**/rollout-*.jsonl` | `codex resume <uuid>` | ✓ launch, then adopt codex's id | sidecar only |
| **opencode** | `~/.local/share/opencode/opencode.db` (read-only) | `opencode <dir> --session <ses_id>` | ✓ launch, then adopt | sidecar only |
| **Gemini CLI** | `~/.gemini/tmp/<project>/chats/session-*.jsonl` | `gemini --resume <uuid>` | ✓ pinned id | sidecar only |
| **Antigravity** (`agy`) | `~/.gemini/antigravity-cli/` (read-only) | `agy --conversation <uuid>` | ✓ | sidecar only |
| **Kimi Code** | `~/.kimi-code/` (read-only) | `kimi -S session_<uuid>` | ✓ launch, then adopt | sidecar only |
| **Plain shell** | BattleLab's own per-session record | reattach to the live PTY | ✓ | sidecar |

Every store location is overridable: `AGENT_SESSIONS_CODEX_SESSIONS_DIR`,
`AGENT_SESSIONS_OPENCODE_DB`, `AGENT_SESSIONS_GEMINI_TMP_DIR`, `AGENT_SESSIONS_KIMI_DIR`. The same
path drives both the sidebar row and the scroll-up transcript, so pointing one at a different
location moves both together.

## Two ways a new session gets its id

This is the one genuine asymmetry between engines, and it explains a behaviour you will otherwise
find puzzling.

**Pinned id.** Claude Code and Gemini accept a caller-supplied session id (`--session-id`), so
BattleLab mints a UUID and launches straight into it. The row exists with its final identity from
the first frame.

**Launch then reconcile.** Codex, opencode, Antigravity and Kimi mint their own id and will not be
told otherwise. BattleLab snapshots the engine's store *before* launch, starts the session under a
temporary `new-<uuid>` placeholder, then diffs the store afterwards and adopts whichever id
appeared. A reconciling engine's row therefore carries a placeholder id for the first moments of
its life.

## The shell engine: a terminal with no agent

`shell` is a plain `bash -l` login shell. It exists because a session organizer that can only hold
agents forces you out to a separate terminal for everything else.

Two consequences worth knowing:

- **No reboot survival.** Agent engines survive a reboot because *their own* resume command
  restores the conversation from their own store. A shell has no saved conversation, so it
  reattaches only while its PTY lives. Scroll-up is the scrollback ring alone, with no transcript
  renderer behind it.
- **AI review still works.** The reviewer builds its input from the transcript *and* the live
  screen, and errors only when both are empty — so a shell is reviewed on its terminal screen
  alone, with no special-casing anywhere in the review code.

## Which engines are present

`GET /api/engines` lists every provider with `present`, `supports_new` and the resolved binary
path. Presence is discovered at install time by `agent-sessions doctor`, which runs on every
install and records resolved paths into the env file. To pin one explicitly, set
`AGENT_SESSIONS_{CLAUDE,CODEX,OPENCODE,GEMINI,KIMI}_BIN`.

Engines that are not installed do not appear. There is no configuration to disable one — absence is
the off switch.

## Identity

A session's app-facing id is **engine-qualified**: `<engine>:<native_id>`, e.g.
`claude:3f2a…`. That id is what the sidebar sends on every route call and what keys the metadata
sidecar. A single gate resolves an id to its provider and validates the native shape *before* any
dispatch, so an id that does not match its engine's pattern is rejected rather than passed along.

::: info Verified against
Commit `218cf3a` — `README.md § Per-engine support`; `docs/reference.md § Engine providers`; `src/agent_sessions/engines/`.
:::
