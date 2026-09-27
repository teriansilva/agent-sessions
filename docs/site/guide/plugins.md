# Write a manifest

Every engine BattleLab runs is a **plugin**: one `plugin.toml` (or `plugin.json`) that declares
everything the app needs to integrate the agent. This page is the reference for that file.

A manifest is **data, not code**. Every block selects a *built-in kind* — reviewed code inside
BattleLab — and supplies parameters that kind validates. A manifest can never carry an adapter, a
shell string, an arbitrary flag, a path glob, a regular expression outside a small grammar, a URL
outside an allowlist, or a command to run. What it may say is a closed set of values, listed in
full [below](#allowed-values).

## What adding an agent means today

The manifest format is complete, but only **in-tree** manifests run. They live in
`src/agent_sessions/plugins/first_party/<id>/plugin.toml`, are reviewed like any other code, and
ship with the app, so adding one is a pull request that touches nothing but that directory. This is
true only when every kind the new agent needs already exists. An agent whose store, transcript,
launch shape or usage source nothing in BattleLab can handle yet needs a new kind first, and that
kind is reviewed code.

The app does **not** read manifests from your own plugins directory yet. The loader and validator
for them exist (they are held to stricter rules than in-tree ones), but nothing adds them to the
roster. Running a plugin BattleLab did not
ship needs two things that do not exist yet: an install step that verifies what it downloads, and
your explicit confirmation of the exact binary. A plugin that runs on nothing more than a file
dropped into a directory would be an unreviewed program launched with permission bypass.

## A complete example

Codex's manifest, exactly as shipped:

<<< @/../../src/agent_sessions/plugins/first_party/codex/plugin.toml{toml}

## The blocks

Unknown fields are **rejected** at every level, not ignored. A field added by a later contract may
be a restriction, and a reader that skipped it would grant what the author meant to withhold.

| Block | Required | What it declares |
|---|---|---|
| `contract` | yes | The manifest format version (an integer). A higher contract than this build reads is refused as "needs a newer BattleLab", and rolling the app back disables such a plugin instead of misreading it. |
| `[identity]` | yes | `id` (lowercase, 2–24 characters: the `<engine>` in every session id), `label`, `publisher`, `version`, and `kind`: `agent`, or `terminal` for a plugin with no agent behind it. |
| `[runtime]` | no | `kind`. Absent means `pty`: a binary under `dtach`, shown in the terminal. |
| `[binary]` | yes | `name` (the plugin id or one of `aliases`), `env_var` (its `AGENT_SESSIONS_*_BIN` override), `search_paths` (absolute or `~/` directories, no globs, no `..`), `version_flag`, and `search_npm_global` for CLIs installed with `npm i -g`. |
| `[session_id]` | yes | `pattern`, the native-id shape (grammar below); `mint` (`pinned` or `adopt`, see [Engines](./engines#two-ways-a-new-session-gets-its-id)); `legacy_bare_id` (claimed by at most one in-tree manifest). |
| `[store]` | no | Where the engine keeps its sessions: `root`, `env_override`, `layout` (the built-in reader), and named auxiliary `paths` (`db`, `log`, …) relative to the root, each with an optional override in `path_env`. |
| `[launch]` | yes | `resume` and `new`, each a kind plus the flag or subcommand it takes; `base_args`; `bypass` (the permission-bypass flags) and `bypass_on`; `admission`. The kind assembles the argv — the manifest never writes one. |
| `[capabilities]` | no | Booleans, **all off unless declared**: `resume`, `new`, `archive`, `handoff_target`, `seed_start`, `orchestrator_input`, `raw_tty`, `owns_transcript`. A `terminal` plugin may not declare `seed_start`, `orchestrator_input`, `raw_tty` or `owns_transcript`. |
| `[transcript]` | no | `kind`, the built-in adapter that renders the conversation for scroll-up and AI review; `strict`. Absent means none. |
| `[usage]` | no | `source`: `plan` (a subscription window), `tokens`, `manual` (you enter it), or `none`. `plan` and `tokens` need a `kind` naming the built-in reporter; a CLI probe also names its `probe_token`. `access` names the check that notices when the vendor refuses the account. |
| `[terminal]` | no | `repaint` (`wipe` for a TUI that redraws its scrollback), `ready` (the first-paint rule), `menu` (the numbered-menu parser) and `menu_digit_submits`. |
| `[unattended]` | no | `start_evidence`: the artifact that proves an agent started. Absent means missions will not dispatch to it unattended. |
| `maintenance` | no | Store maintenance the plugin admits, e.g. `sqlite-vacuum`. |
| `[models]` | no | `list` of `{ id, context_window, aliases }`, and `configured_elsewhere`. |
| `[display]` | yes | `name`, `badge` (2–3 lowercase letters), `accent` (a colour **token**, never a hex), `id_prefix` (stripped when an id is shown), and `order` in the roster. |
| `[install]` | no | `kind`, `authority`, `package`, `version`, `digest` (`sha256:…`) and `entrypoint`. Validated today and executed by nothing: there is no install step yet. |
| `[signin]` | no | `kind` and the login `subcommand`, chosen from a fixed set. Validated today and executed by nothing. |
| `verify` | no | The subset of the fixed check list a verification run would perform. Validated today and executed by nothing. |

The validator also checks the blocks against each other. For example: `capabilities.new` needs
`launch.new`; a `pin-flag` new session needs `mint = "pinned"`; `owns_transcript` needs a
transcript kind; `sqlite-vacuum` and store admission need `store.paths.db`; and an `install` block
needs `binary.env_var`.

## The session-id pattern

`session_id.pattern` reaches the gate every route call passes through, so it is written in a
grammar too small to be sloppy: `^` … `$` anchors, literal characters `[A-Za-z0-9_-]`, character
classes of those (ranges stay within `0-9`, `a-z` or `A-Z`), and `{n}`, `{n,m}` or `+` counts.
There is no `.`, no `*`, no `?`, no groups and no alternation. No atom may match the empty
string, and a variable-length run may not share a character with the atom after it, so matching
never backtracks. Native ids are capped at 128 characters and may never start with `-`.

## Which binary runs

Launch argv is built from a closed vocabulary, but that only helps if `argv[0]` is the right file.
The app records every binary in one of two states:

- **managed**: installed by BattleLab into the plugin's own directory, bound to the digest it was
  verified against. (No install step exists yet, so nothing is managed today.)
- **adopted**: a CLI you installed yourself, found through the `*_BIN` override or
  `binary.search_paths`. An in-tree manifest's adopted binary runs on the manifest's own authority.
  A plugin you supplied yourself would run only after you confirmed that exact file by path and
  sha256.

In both states, BattleLab never looks the binary up on `PATH` at launch, and the file and every
directory above it must be writable by nobody but you or root. What happens before each launch
depends on who vouches for the file:

- **Bound to a digest** (a managed binary, or an adopted one you confirmed for a plugin you
  supplied): the file's identity (device, inode, size, mtime, ctime) is re-checked before
  **every** launch. Whenever that identity changed, the file is re-hashed and must still match
  the recorded digest, or the launch is refused.
- **An in-tree manifest's adopted binary**: there is no digest to hold it to. It is resolved again
  before every launch, applying the same location and ownership rules, so a vendor's own
  auto-update is picked up rather than refused. It is never hashed.

A plugin you supplied may not name, alias or resolve to a shell, interpreter or privilege
tool (`bash`, `env`, `sudo`, `python`, `node`, …). The check covers the entrypoint **file**: when
it is a script, the interpreter on its `#!` line and anything it imports are outside it.

## Allowed values

Every value a manifest may choose, generated from the kinds this build ships:

<!-- BEGIN generated:manifest-vocab -->
| Field | Allowed values |
|---|---|
| `contract` | `1` |
| `identity.kind` | `agent`, `terminal` |
| `runtime.kind` | `pty` |
| `binary.version_flag` | `--version`, `-V`, `-v`, `version` |
| `session_id.mint` | `adopt`, `pinned` |
| `store.layout` | `antigravity-cli`, `claude-projects`, `codex-rollouts`, `gemini-tmp`, `kimi-code`, `opencode-sqlite`, `shell-records` |
| `store.paths` names | `archive`, `db`, `index`, `log`, `sessions` |
| `launch.resume.kind` | `flag`, `fresh`, `positional-dir`, `subcommand` |
| `launch.new.kind` | `bare`, `cwd-flag`, `pin-flag`, `positional-dir` |
| resume `flag` | `--conversation`, `--resume`, `--session`, `-S` |
| resume `subcommand` | `resume` |
| new `pin-flag` flag | `--session-id` |
| new `cwd-flag` flag | `--cd` |
| `launch.base_args` | `-l` |
| `launch.bypass` | `--dangerously-bypass-approvals-and-sandbox`, `--dangerously-skip-permissions`, `--skip-trust`, `--yolo`, `-y` |
| `launch.bypass_on` | `both`, `new` |
| `launch.admission` | `none`, `sqlite-store-shared` |
| `capabilities` | `archive`, `handoff_target`, `new`, `orchestrator_input`, `owns_transcript`, `raw_tty`, `resume`, `seed_start` |
| `transcript.kind` | `antigravity-brain`, `claude-jsonl`, `codex-rollout`, `gemini-chat`, `kimi-wire`, `none`, `opencode-sqlite` |
| `usage.source` | `manual`, `none`, `plan`, `tokens` |
| `usage.kind` | `agy-cli-probe`, `claude-cli-probe`, `codex-app-server-probe`, `codex-rollout-field`, `opencode-store-query` |
| `usage.probe_token` | `/usage` |
| `usage.access` | `gemini-acp-auth`, `kimi-wire-auth-error` |
| `terminal.repaint` | `none`, `wipe` |
| `terminal.ready` | `bytes`, `claude-first-paint` |
| `terminal.menu` | `claude-numbered`, `none` |
| `unattended.start_evidence` | `claude-sessions`, `none`, `opencode-log` |
| `maintenance` | `sqlite-vacuum` |
| `display.accent` | `amber`, `blue`, `green`, `lime`, `magenta`, `slate`, `teal` |
| `install.kind` → `authority` | `npm-prefix` → `registry.npmjs.org`; `tarball` → `github.com` |
| `signin.kind` → `subcommand` | `cli-subcommand` → `auth`, `login`; `none` → none |
| `verify` | `binary`, `new`, `resume`, `store`, `transcript`, `usage`, `version` |
<!-- END generated:manifest-vocab -->

A value missing from this table is refused. Widening it is a code change, reviewed like any other.

::: info Verified against
Commit `b48d0c4` — `src/agent_sessions/plugins/manifest.py`; `src/agent_sessions/plugins/kinds.py`; `src/agent_sessions/plugins/provenance.py`; `src/agent_sessions/plugins/__init__.py`. The allowed-values table is generated by `scripts/gen-engine-docs`.
:::
