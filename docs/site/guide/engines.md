# Engines

BattleLab does not implement agents. It **organizes** them: six AI coding CLIs, a plain shell and
an API agent that talks to a model endpoint you configure, all presented through one session list. Each one is a **plugin**, described by a
single declarative manifest (`plugin.toml`) that tells BattleLab everything it needs: the binary,
the shape of a session id, where the engine keeps its sessions, how to resume and start one, what
the app may do with it, and how to show it.

The roster is the set of manifests. Nothing else in the app keeps a list of engines, so the
sidebar, the map, the new-session picker, handoff, missions, usage and Settings → **Agents** all
show the same engines in the same order. [Write a manifest](./plugins) documents the format.

## The rule that shapes everything

Every engine handles its own conversation persistence. BattleLab reads each tool's own on-disk
history for the sidebar, and "resume" always means launching *that tool's own resume command*
under BattleLab's `dtach` PTY. Nothing is re-implemented and, with one exception, nothing is
written back.

The API agent is the one engine with no tool of its own, so BattleLab keeps its transcript itself
(see [The API agent](#the-api-agent-a-model-endpoint-with-no-process)).

The other exception is Claude Code, whose archive moves its JSONL between `projects/` and
`projects-archive/`. Every other engine's archive is a flag in BattleLab's own sidecar. opencode's
SQLite database in particular is opened read-only and never written, which is why renaming,
favouriting and archiving an opencode session change nothing inside opencode.

## The roster

This table is generated from the in-tree manifests, so it cannot drift from what the app runs.
The commands are the argv the launcher builds (`<id>` is the session id, `<dir>` its working
directory), shown with the binary's name instead of its resolved path.

<!-- BEGIN generated:engine-table -->
| Engine | Binary · override | Sessions read from · override | Resume | New session | Permission bypass |
|---|---|---|---|---|---|
| **Claude Code** (`claude`) | `claude` · `AGENT_SESSIONS_CLAUDE_BIN` | `~/.claude` | `claude --resume <id>` | `claude --session-id <id>` — pinned id | `--dangerously-skip-permissions` |
| **opencode** (`opencode`) | `opencode` · `AGENT_SESSIONS_OPENCODE_BIN` | `~/.local/share/opencode` · `AGENT_SESSIONS_OPENCODE_DB`, `AGENT_SESSIONS_OPENCODE_LOG` | `opencode <dir> --session <id>` | `opencode <dir>`, then adopt the id it mints | — |
| **Codex** (`codex`) | `codex` · `AGENT_SESSIONS_CODEX_BIN` | `~/.codex/sessions` · `AGENT_SESSIONS_CODEX_SESSIONS_DIR` | `codex resume <id>` | `codex --cd <dir>`, then adopt the id it mints | `--dangerously-bypass-approvals-and-sandbox` (new sessions only) |
| **Gemini CLI** (`gemini`) | `gemini` · `AGENT_SESSIONS_GEMINI_BIN` | `~/.gemini/tmp` · `AGENT_SESSIONS_GEMINI_TMP_DIR` | `gemini --resume <id>` | `gemini --session-id <id>` — pinned id | `--yolo` `--skip-trust` |
| **Antigravity** (`antigravity`) | `agy` · `AGENT_SESSIONS_AGY_BIN` | `~/.gemini/antigravity-cli` · `AGENT_SESSIONS_ANTIGRAVITY_DIR` | `agy --conversation <id>` | `agy`, then adopt the id it mints | `--dangerously-skip-permissions` |
| **Kimi Code** (`kimi`) | `kimi` · `AGENT_SESSIONS_KIMI_BIN` | `~/.kimi-code` · `AGENT_SESSIONS_KIMI_DIR` | `kimi -S <id>` | `kimi`, then adopt the id it mints | `-y` |
| **API agent** (`apichat`) | — | `~/.local/share/agent-sessions/chat` | no process — runtime `chat`, endpoint `openai-chat` | new conversation | — |
| **Shell** (`shell`) | `bash` · `AGENT_SESSIONS_BASH_BIN` | `~/.claude/shell-sessions` · `AGENT_SESSIONS_SHELL_DIR` | a fresh process — nothing to resume | `bash -l` — pinned id | — |
<!-- END generated:engine-table -->

Every override is read from the app's env file. The same store path drives both the sidebar row
and the scroll-up transcript, so pointing one at a different location moves both together:

<!-- BEGIN generated:engine-env -->
| Variable | Engine | Overrides |
|---|---|---|
| `AGENT_SESSIONS_CLAUDE_BIN` | Claude Code | the binary |
| `AGENT_SESSIONS_OPENCODE_BIN` | opencode | the binary |
| `AGENT_SESSIONS_OPENCODE_DB` | opencode | the store's `db` (`~/.local/share/opencode/opencode.db`) |
| `AGENT_SESSIONS_OPENCODE_LOG` | opencode | the store's `log` (`~/.local/share/opencode/log/opencode.log`) |
| `AGENT_SESSIONS_CODEX_BIN` | Codex | the binary |
| `AGENT_SESSIONS_CODEX_SESSIONS_DIR` | Codex | the store root (`~/.codex/sessions`) |
| `AGENT_SESSIONS_GEMINI_BIN` | Gemini CLI | the binary |
| `AGENT_SESSIONS_GEMINI_TMP_DIR` | Gemini CLI | the store root (`~/.gemini/tmp`) |
| `AGENT_SESSIONS_AGY_BIN` | Antigravity | the binary |
| `AGENT_SESSIONS_ANTIGRAVITY_DIR` | Antigravity | the store root (`~/.gemini/antigravity-cli`) |
| `AGENT_SESSIONS_KIMI_BIN` | Kimi Code | the binary |
| `AGENT_SESSIONS_KIMI_DIR` | Kimi Code | the store root (`~/.kimi-code`) |
| `AGENT_SESSIONS_CHAT_DIR` | API agent | the store root (`~/.local/share/agent-sessions/chat`) |
| `AGENT_SESSIONS_BASH_BIN` | Shell | the binary |
| `AGENT_SESSIONS_SHELL_DIR` | Shell | the store root (`~/.claude/shell-sessions`) |
<!-- END generated:engine-env -->

## Two ways a new session gets its id

This is the one genuine asymmetry between engines, and it explains a behaviour you will otherwise
find puzzling. A manifest declares it as `session_id.mint`.

**Pinned id** (`pinned`). The engine accepts a caller-supplied session id, so BattleLab mints a
UUID and launches straight into it. The row exists with its final identity from the first frame.

**Launch then reconcile** (`adopt`). The engine mints its own id and will not be told otherwise.
BattleLab snapshots the engine's store *before* launch, starts the session under a temporary
`new-<uuid>` placeholder, then diffs the store afterwards and adopts whichever id appeared. A
reconciling engine's row therefore carries a placeholder id for the first moments of its life.

## Choosing a model, and the instruction files an engine reads

The new-session form offers a **Model** select for an engine whose manifest declares
`launch.model`. `default` is always first and selected: it adds no flag, which is exactly how a
session started before the picker existed. Any other choice is one of the manifest's models (an
alias such as `opus` is replaced by its id before anything is stored or launched) or an id you
added under **Settings → Agents → *engine***, for a model released after this build.

- A choice that is no longer offered — an added id you have since removed, a model from another
  engine — is **refused before anything starts**, never quietly replaced by `default`.
- An engine whose model is set in its own configuration (opencode, Kimi Code, Antigravity) or, for
  the API agent, in its endpoint settings, offers `default` only.
- **A resume keeps its model.** Where the engine takes the flag on resume (Claude Code), a session
  started on a model resumes on it; if that model is no longer offered, the resume is refused
  rather than quietly using the default. Asking a Codex or Gemini session to resume on a
  different model is refused, and you start a new session instead. Attaching to a session that is
  already running never changes its model.
- The session records the model it **asked for**. What it actually ran on is read only from the
  engine's own transcript (Claude Code and Codex write it); anywhere else it is shown as unknown.

<!-- BEGIN generated:engine-models -->
| Engine | Model at launch | Models offered | Instruction files |
|---|---|---|---|
| **Claude Code** (`claude`) | `--model <id>` on new sessions and on resume | `claude-opus-5` (`opus`), `claude-sonnet-5` (`sonnet`), `claude-haiku-4-5` (`haiku`), `claude-fable-5-1`, `claude-opus-4-8`, `claude-sonnet-4-6` | `CLAUDE.md` |
| **opencode** (`opencode`) | set in the agent's own configuration | `default` only | `AGENTS.md` |
| **Codex** (`codex`) | `--model <id>` on new sessions (a resume keeps its model) | `gpt-5-codex`, `gpt-5` | `AGENTS.md` |
| **Gemini CLI** (`gemini`) | `--model <id>` on new sessions (a resume keeps its model) | `gemini-2.5-pro`, `gemini-2.5-flash` | `GEMINI.md` |
| **Antigravity** (`antigravity`) | set in the agent's own configuration | `default` only | `GEMINI.md` |
| **Kimi Code** (`kimi`) | set in the agent's own configuration | `default` only | `AGENTS.md` |
| **API agent** (`apichat`) | set in the agent's own configuration | `default` only | — |
| **Shell** (`shell`) | — | — | — |
<!-- END generated:engine-models -->

## The shell engine: a terminal with no agent

`shell` is a plain `bash -l` login shell, declared with `identity.kind = "terminal"`. It exists
because a session organizer that can only hold agents forces you out to a separate terminal for
everything else.

Three consequences worth knowing:

- **No reboot survival.** Agent engines survive a reboot because *their own* resume command
  restores the conversation from their own store. A shell has no saved conversation, so it
  reattaches only while its PTY lives. Scroll-up is the scrollback ring alone, with no transcript
  renderer behind it.
- **Nothing types into it for you.** A `terminal` plugin may not declare seeding, orchestrator
  input or raw-TTY repair — text the app typed into a bare shell would run as a command — so it is
  never a handoff target and never driven by a mission.
- **AI review still works.** The reviewer builds its input from the transcript *and* the live
  screen, and errors only when both are empty — so a shell is reviewed on its terminal screen
  alone, with no special-casing anywhere in the review code.

## The API agent: a model endpoint with no process

`apichat` declares `runtime.kind = "chat"`: there is no binary and no terminal. BattleLab sends the
conversation to an OpenAI-compatible chat endpoint and shows the replies in a chat pane where the
terminal would be. Set it up in Settings → **Agents** → **API agent** → **Endpoint**: a base URL, an
API key and a model. **Test** checks the draft (it lists the endpoint's models) and saves nothing;
**Save** stores it, and the agent appears in the new-session picker at once.

What to know:

- **By default it has no tools.** It can only talk. Replies are shown as text, never executed and
  never typed into a terminal, so it is never a handoff target and never driven by a mission. You
  can let it **read** files (below); it can never write, delete or run anything.
- **Your text leaves the host.** Every message, with the earlier turns that fit, goes to the
  endpoint you configured. Template secrets are redacted on the way out, the same as AI review.
- **The key stays with its endpoint.** It is encrypted at rest, never shown again, and bound to
  the URL's origin: changing the URL to another host needs a new key (or removing the key).
- **Long conversations are trimmed.** Each request carries as many recent turns as fit the model's
  context window minus the reply reserve; older turns are left out, and the pane says how many. A
  single message too long for the window is refused.
- **A failed turn keeps your message.** Retry sends that same turn again. A timeout means the
  endpoint may already have processed — and billed — the request, and the pane says so.
- **Transcripts are BattleLab's.** Each conversation is a JSONL file under
  `~/.local/share/agent-sessions/chat` (`AGENT_SESSIONS_CHAT_DIR`), mode 0600. Removing the
  endpoint keeps them readable.

### Tools: reading files in the conversation's folder

The Endpoint card's **Tools** setting is **None** by default. **Read files** gives the agent two
read-only tools, `list_files` and `read_file`, confined to the folder the conversation was started
in:

- **What leaves the host.** Any file it opens is sent to the endpoint you configured, with the
  conversation. The agent's replies can quote those files, and replies are kept in the transcript
  like any other. The raw tool output is never stored or shown: the pane lists each call as one
  line (*Listed*, *Read* with the line span, *Refused* with the reason).
- **What is refused.** Paths outside the folder; symbolic links as the final path component;
  hidden paths (`.env`, `.git/`, `.ssh/`, …); key and credential file names (`*.pem`, `*.key`,
  `id_rsa*`, `credentials*`, `secrets*`, …) — also when reached through a link; binary files;
  and anything under your excluded folders or outside your project roots. The boundary is the
  file's *path*: a hard link placed inside the folder reads its target like any other file there.
  An ordinary file can still hold a secret, so turn this on only for folders you are content to
  share with the endpoint.
- **Checked every time.** The setting and your folder rules are read again before every call and
  before every request. Turning Tools off, or excluding a folder, mid-reply stops the next call —
  and anything already read that the new rules no longer allow is withdrawn before it is sent.
- **Bounded.** At most 8 rounds of tool calls per message and 16 calls per round; a read returns
  at most 2000 lines or 64 KiB, a listing at most 500 entries; everything counts against the
  context window. After the last round the endpoint must answer. An endpoint or model without
  tool support fails the message with that reason — turn Tools off for it.

## Which engines are present

Settings → **Agents** lists every engine with its state, and each engine has its own page showing
everything its manifest declares. The same data is `GET /api/engines` (the roster, with `present`,
`supports_new`, the resolved binary, `display`, `capabilities`, `runtime` and `status`) and
`GET /api/engines/{id}` (one engine's full manifest and binary provenance).

Presence is decided by the binary. `agent-sessions doctor` runs on every install and resolves each
engine's binary — its `*_BIN` override above if that still executes, else your `PATH`, else the
manifest's `binary.search_paths` — and records the result into the env file. There is no `PATH`
lookup at launch: the launcher execs the
absolute path it resolved, after checking that nobody but you or root can replace that file or any
directory above it. A binary you installed yourself is shown as **adopted** — BattleLab found it
rather than installed it.

The API agent has no binary: it is present once its endpoint has a URL, a key and a model.

An engine without a runnable binary cannot start or resume a session. If its store is still on
disk, its existing sessions stay listed, so you can find and archive them, but resuming one fails
until the binary is back. There is no switch to disable an engine — absence is the off switch.

## Removing an engine

The roster is read when the app starts. If an engine's manifest is gone after an update, its
running sessions are **not** killed: they stay attachable, marked *retiring*, until they exit. No
new session, handoff or mission dispatch can start on it — those are refused as "agent removed".
Nothing stored is lost: its sessions stay on disk, and a budget or a default that names it stays in
your preferences, unused, until the engine comes back.

## Identity

A session's app-facing id is **engine-qualified**: `<engine>:<native_id>`, e.g.
`claude:3f2a…`. That id is what the sidebar sends on every route call and what keys the metadata
sidecar. A single gate resolves an id to its engine and checks the native part against that
engine's `session_id.pattern` *before* any dispatch, so an id that does not match is rejected
rather than passed along.

::: info Verified against
Commit `b48d0c4` — `src/agent_sessions/plugins/first_party/*/plugin.toml`; `src/agent_sessions/plugins/`; `src/agent_sessions/routes/system.py § /api/engines`. The tables are generated by `scripts/gen-engine-docs`.
:::
