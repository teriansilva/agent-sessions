# Write a manifest

Every engine BattleLab runs is a **plugin**: one `plugin.toml` (or `plugin.json`) that declares
everything the app needs to integrate the agent. This page is the reference for that file.

A manifest is **data, not code**. Every block selects a *built-in kind* — reviewed code inside
BattleLab — and supplies parameters that kind validates. A manifest can never carry an adapter, a
shell string, an arbitrary flag, a path glob, a regular expression outside a small grammar, a URL
outside an allowlist, or a command to run. What it may say is a closed set of values, listed in
full [below](#allowed-values).

## What adding an agent means today

In-tree manifests live in `src/agent_sessions/plugins/first_party/<id>/plugin.toml` and ship
with the app. Additional agents enter through a reviewed installation, verification and explicit
activation. Dropping a file into a directory grants no execution. An agent whose store,
transcript or launch shape has no built-in kind still needs a reviewed code change first.

The installer accepts entries from an authenticated signed catalog, or a local manifest and
pinned recipe that you confirm for each install/update. A local source stays **untrusted** after
its functional checks pass. Local entries cannot claim a first-party identity. Existing binaries
can be adopted only with separate confirmation of their absolute path and SHA-256.

An installation stages a disabled candidate and keeps the current generation working. Sign-in,
verification, enable and roster reload are separate actions. Failed or interrupted work never
counts as successful verification, and old installed copies, vendor credentials, transcripts,
saved budgets and defaults are retained when an agent is disabled or removed.

### Add or update an agent

Open **Settings → Agents** to search the installed roster and signed catalog. The filters show
agents that are ready, need setup, have an update, or are disabled. Existing usage meters and
saved defaults stay on the same page. **Refresh catalog** checks the signed release feed; a
failed refresh shows its error and keeps the installed roster visible.

Choose **Add agent** or a card's **Set up agent** / **Review update** action. Select a signed
entry, or paste a local manifest-and-recipe JSON object. The review shows its publisher, source,
host access, pinned artifact URLs and digests. Local entries require a fresh confirmation;
adopting an existing executable requires a separate path-and-digest confirmation.

**Install** creates a disabled candidate. The following steps offer a temporary sign-in terminal
(or API endpoint configuration), explain verification's effects, show each check, then offer
**Enable agent** only after every required check passes. A local publisher remains untrusted
after verification. Enabling refreshes the roster; it does not change your saved default.

The setup URL identifies the exact operation or installed generation. Refreshing the page or
choosing **View operation** reads that saved state. If an install response is lost, use
**Check operation status**; returning to the wizard never starts a second install or sign-in.
Leaving an install/verification page lets its server job continue. Closing a sign-in terminal
interrupts it, and reconnecting requires an explicit new attempt.

**Disable** and **Remove** ask for confirmation, refuse new work and retain vendor data and
installed copies. A removed default remains saved and is shown as unavailable. Running sessions
remain attachable until they exit; neither action stops those sessions.

### Installer operations

The authenticated `/api/plugins` routes provide catalog refresh, review, install, sign-in,
verification, activation, disable/remove and roster reload. Every mutation requires the existing
CSRF token and matching origin. Operation IDs are caller-minted UUIDs: repeating the same ID and
payload reads its durable outcome, while changing the payload is refused. Reloading or returning
to setup reads status; it never resubmits a job automatically.

The fixed public release catalog is verified against the signer bundled with this installed
BattleLab release. Expired, replayed or altered feeds are refused, preserving the previous
accepted evidence. Recipes pin every artifact and dependency. Extraction is inert: no package
lifecycle scripts or network-resolving package installer runs.

Sign-in uses a separate temporary terminal with no BattleLab session registration, scrollback
capture, AI review or retained terminal bytes. The vendor may save its own credentials. Closing
the socket interrupts the attempt; start a new attempt explicitly. A systemd user manager is
required for these bounded terminal operations. There is no uncontained fallback.

Verification requires explicit confirmation of its effects. CLI checks run a fixed test
conversation in a private workspace, may create vendor history, contact the vendor and consume
quota, and never request permission bypass. A vendor trust/permission prompt can cause a check
to time out; it is not treated as success or answered with an automatic grant. API-agent checks
send a fixed message to the saved endpoint; an endpoint change invalidates that evidence.
All required checks must pass for the exact candidate before it can be enabled.

Sign-in and verification share one private workspace for each candidate. If a CLI asks whether
to trust that folder, answer it in the explicit sign-in terminal before verification. A retry
creates a fresh test conversation there and resumes that same identity; an older reply cannot
pass a new check. The workspace and vendor-owned probe history are retained.

A disconnected install/verify request leaves its one server job running. After an app restart,
interrupted operations are recorded, never replayed. A `cleanup_pending` result must be recovered
before another operation starts. Removal disables new work; existing sessions remain attachable
until they exit. No removal action deletes vendor data or retained generations.

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
| `[runtime]` | no | `kind`. Absent means `pty`: a binary under `dtach`, shown in the terminal. `chat` means no process at all: BattleLab sends the conversation to an HTTP model endpoint **you** configure and keeps the transcript itself. A `chat` manifest may not declare `binary`, `launch`, `terminal`, `unattended`, `install`, `signin`, `probe`, `verify` or `instructions`, nor any capability that presumes a terminal (`seed_start`, `orchestrator_input`, `raw_tty`, `handoff_target`, `owns_transcript`). |
| `[endpoint]` | for `chat` only | `kind`: the wire format (`openai-chat`). **Nothing else**: the URL, API key and model are your configuration, never the manifest's, so a manifest cannot point BattleLab at a server. By default a `chat` agent can only talk. The operator can opt into folder-scoped reads and per-file edit proposals; each proposed replacement needs a separate authenticated approval before saving ([Tools](./engines#tools-reading-files-in-the-conversation-s-folder)). |
| `[binary]` | for `pty` | `name` (the plugin id or one of `aliases`), `env_var` (its `AGENT_SESSIONS_*_BIN` override), `search_paths` (absolute or `~/` directories, no globs, no `..`), `version_flag`, and `search_npm_global` for CLIs installed with `npm i -g`. |
| `[session_id]` | yes | `pattern`, the native-id shape (grammar below); `mint` (`pinned` or `adopt`, see [Engines](./engines#two-ways-a-new-session-gets-its-id)); `legacy_bare_id` (claimed by at most one in-tree manifest). |
| `[store]` | no | Where the engine keeps its sessions: `root`, `env_override`, `layout` (the built-in reader), and named auxiliary `paths` (`db`, `log`, …) relative to the root, each with an optional override in `path_env`. |
| `[launch]` | for `pty` | `resume` and `new`, each a kind plus the flag or subcommand it takes; `base_args`; `bypass` (the permission-bypass flags) and `bypass_on`; `admission`; and `model` (`{ kind = "flag", flag, on_resume }`): the flag that takes a model id, and whether the engine honours it on a resume. The kind assembles the argv — the manifest never writes one, and never names the model value: that is a model the launch resolved against `[models]` and your added ids. |
| `[capabilities]` | no | Booleans, **all off unless declared**: `resume`, `new`, `archive`, `handoff_target`, `seed_start`, `orchestrator_input`, `raw_tty`, `owns_transcript`. A `terminal` plugin may not declare `seed_start`, `orchestrator_input`, `raw_tty` or `owns_transcript`. |
| `[transcript]` | no | `kind`, the built-in adapter that renders the conversation for scroll-up and AI review; `strict`. Absent means none. |
| `[usage]` | no | `source`: `plan` (a subscription window), `tokens`, `manual` (you enter it), or `none`. `plan` and `tokens` need a `kind` naming the built-in reporter; a CLI probe also names its `probe_token`. `access` names the check that notices when the vendor refuses the account. |
| `[terminal]` | no | `repaint` (`wipe` for a TUI that redraws its scrollback), `ready` (the first-paint rule), `menu` (the numbered-menu parser) and `menu_digit_submits`. |
| `[unattended]` | no | `start_evidence`: the artifact that proves an agent started. Absent means missions will not dispatch to it unattended. |
| `maintenance` | no | Store maintenance the plugin admits, e.g. `sqlite-vacuum`. |
| `[models]` | no | `list` of `{ id, context_window, aliases }`, and `configured_elsewhere` (the engine's model is set in its own configuration, so pickers offer `default` only). Every id and alias names exactly one model, and none may be `default`. A `chat` plugin's list must be empty: its model is your endpoint configuration. |
| `[instructions]` | no | `files`: the workspace-root instruction files the engine reads (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`) — bare names from a fixed set, never a path. Forbidden for `chat`. |
| `[display]` | yes | `name`, `badge` (2–3 lowercase letters), `accent` (a colour **token**, never a hex), `id_prefix` (stripped when an id is shown), and `order` in the roster. |
| `[install]` | no | `kind`, `authority`, `package`, `version`, `digest` (`sha256:…`), `entrypoint`, and optional `platform`. A declared platform must match this host. The installer consumes only a pinned reviewed recipe matching these coordinates. Scoped npm entrypoints admit `@scope` only immediately below `node_modules`. |
| `[signin]` | no | `kind`: `cli-subcommand` takes a closed login `subcommand`; `auth-login` runs fixed `auth login`; `interactive` starts the CLI; `none` offers no sign-in. Executed only by the explicit temporary sign-in operation. |
| `[probe]` | no | `kind`, selecting a built-in fixed conversation mode. Absent means `terminal`. No argv, prompt or permission flags may be supplied. Forbidden for `chat`. |
| `verify` | no | The fixed required checks performed by an explicit verification operation before enable. |

The validator also checks the blocks against each other. For example: `capabilities.new` needs
`launch.new`; a `pin-flag` new session needs `mint = "pinned"`; `owns_transcript` needs a
transcript kind; `sqlite-vacuum` and store admission need `store.paths.db`; an `install` block
needs `binary.env_var`; and `launch.model` needs a non-empty `models.list` and may not be combined
with `configured_elsewhere`.

A field added to the format after your BattleLab was built is an unknown field to it, so an older
build refuses a manifest that uses one rather than skipping it. The `contract` number moves only
when an existing field changes meaning.

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
  verified against.
- **adopted**: a CLI you installed yourself, found through the `*_BIN` override or
  `binary.search_paths`. An in-tree manifest's adopted binary runs on the manifest's own authority.
  A plugin you supplied yourself runs only after you confirm that exact file by path and
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
| `runtime.kind` | `chat`, `pty` |
| `endpoint.kind` (runtime `chat` only) | `openai-chat` |
| `binary.version_flag` | `--version`, `-V`, `-v`, `version` |
| `session_id.mint` | `adopt`, `pinned` |
| `store.layout` | `antigravity-cli`, `battlelab-chat`, `claude-projects`, `codex-rollouts`, `gemini-tmp`, `kimi-code`, `opencode-sqlite`, `shell-records` |
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
| `launch.model.kind` | `flag` |
| `launch.model.flag` | `--model`, `-m` |
| `instructions.files` | `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` |
| `capabilities` | `archive`, `handoff_target`, `new`, `orchestrator_input`, `owns_transcript`, `raw_tty`, `resume`, `seed_start` |
| `transcript.kind` | `antigravity-brain`, `battlelab-chat`, `claude-jsonl`, `codex-rollout`, `gemini-chat`, `kimi-wire`, `none`, `opencode-sqlite` |
| `usage.source` | `manual`, `none`, `plan`, `tokens` |
| `usage.kind` | `agy-cli-probe`, `chat-response-tokens`, `claude-cli-probe`, `codex-app-server-probe`, `codex-rollout-field`, `kimi-web-usage-probe`, `opencode-store-query` |
| `usage.probe_token` | `/usage` |
| `usage.access` | `gemini-acp-auth`, `kimi-wire-auth-error` |
| `terminal.repaint` | `none`, `wipe` |
| `terminal.ready` | `bytes`, `claude-first-paint` |
| `terminal.menu` | `claude-numbered`, `none` |
| `terminal.permission` | `claude-permission`, `none`, `opencode-permission` |
| `unattended.start_evidence` | `claude-sessions`, `none`, `opencode-log` |
| `maintenance` | `sqlite-vacuum` |
| `display.accent` | `amber`, `blue`, `green`, `lime`, `magenta`, `slate`, `teal` |
| `install.kind` → `authority` | `npm-prefix` → `registry.npmjs.org`; `tarball` → `github.com` |
| `signin.kind` → `subcommand` | `auth-login` → none; `cli-subcommand` → `auth`, `login`; `interactive` → none; `none` → none |
| `install.platform` | `linux-x64` |
| `probe.kind` | `exec-readonly`, `print-conversation`, `print-pinned`, `prompt-pinned`, `prompt-session`, `run-session`, `terminal` |
| `verify` | `binary`, `new`, `resume`, `store`, `transcript`, `usage`, `version` |
<!-- END generated:manifest-vocab -->

A value missing from this table is refused. Widening it is a code change, reviewed like any other.

::: info Verified against
Commit `b48d0c4` — `src/agent_sessions/plugins/manifest.py`; `src/agent_sessions/plugins/kinds.py`; `src/agent_sessions/plugins/provenance.py`; `src/agent_sessions/plugins/__init__.py`. The allowed-values table is generated by `scripts/gen-engine-docs`.
:::
