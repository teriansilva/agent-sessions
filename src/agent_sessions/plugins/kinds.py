"""The closed value space a plugin manifest selects from (#853 §2).

Every set here is **reviewed code**. A manifest may only *choose* a member; it can never add one.
That is the whole security argument of the manifest format: a literal argv list stops shell
parsing, but it does not make attacker-chosen arguments safe — what bounds the argument space is
that every flag, subcommand, layout, parser and probe token a manifest can name is listed here.

A genuinely new *shape* of engine (a store nothing can read, a flag nobody has used) is therefore a
PR that adds a member — rarer by design than the version bumps, model changes, store moves and
entrypoint renames a manifest absorbs without one.

Adding a member here is the ONLY way to widen what a plugin can do. Keep each set tight, and name
the engine that needed a member in a comment beside it, so a later removal knows what it breaks.
"""

from __future__ import annotations

#: Manifest contract versions this build can read. A manifest declaring a HIGHER contract is
#: refused with "needs a newer BattleLab" rather than half-read: an unknown field in a newer
#: contract may be a restriction, and ignoring a restriction widens what the plugin can do. That is
#: also the rollback behaviour — downgrading the app disables such a plugin, it never misreads it.
CONTRACT_CURRENT = 1

#: `identity.kind`. A `terminal` plugin has no agent behind it (the `shell` engine, #636): the
#: validator refuses every capability that would let the app author input for it or rewrite its
#: termios, because typed "continue" into a bare shell is executed as a command.
IDENTITY_KINDS = frozenset({"agent", "terminal"})

#: `runtime.kind` (#853 §7). `pty`: a binary under `dtach`, shown through xterm. `chat` (#853 P9a,
#: #1209): no binary and no terminal — BattleLab sends the conversation to an operator-configured
#: HTTP endpoint and keeps the transcript itself. A chat plugin executes nothing; its only tools are
#: the operator-granted, read-only file tools of #1222 (`chat_tools`), never the manifest's to name.
RUNTIME_KINDS = frozenset({"pty", "chat"})
#: `endpoint.kind` — the wire format a `chat` plugin speaks. The endpoint's URL, key and model are
#: the OPERATOR's configuration, never the manifest's: a manifest can name no authority at all.
ENDPOINT_KINDS = frozenset({"openai-chat"})
#: Blocks that only make sense for a process in a terminal. A `chat` manifest declaring any of them
#: is refused rather than silently ignoring them.
PTY_ONLY_BLOCKS = ("binary", "launch", "terminal", "unattended", "install", "signin", "verify")
#: Capabilities a `chat` plugin may never assert: each presumes a terminal to type into, a TTY to
#: repair, a session to hand off into, or a process that owns its transcript.
PTY_ONLY_CAPABILITIES = frozenset(
    {"seed_start", "orchestrator_input", "raw_tty", "handoff_target", "owns_transcript"}
)

#: `session_id.mint`. `pinned`: BattleLab mints the id before launch (claude, gemini, shell).
#: `adopt`: the engine mints its own and the new-session path reconciles a `new-<uuid>`
#: placeholder to it afterwards (opencode, codex, antigravity, kimi — `RECONCILE_ENGINES`).
MINT_KINDS = frozenset({"pinned", "adopt"})

# --- launch -------------------------------------------------------------------------------------

#: Flags that take the session id as their value on RESUME.
#: claude/gemini `--resume`, antigravity `--conversation`, kimi `-S`, opencode `--session`.
RESUME_FLAGS = frozenset({"--resume", "--conversation", "-S", "--session"})
#: Subcommands that take the session id as their positional argument on resume. codex `resume`.
RESUME_SUBCOMMANDS = frozenset({"resume"})
#: Flags that pin a caller-minted id on a NEW session. claude/gemini `--session-id`.
NEW_PIN_FLAGS = frozenset({"--session-id"})
#: Flags that take the working directory as their value on a new session. codex `--cd`.
CWD_FLAGS = frozenset({"--cd"})
#: Permission-bypass flags. claude/antigravity `--dangerously-skip-permissions`, gemini `--yolo` +
#: `--skip-trust`, kimi `-y`, codex `--dangerously-bypass-approvals-and-sandbox`.
BYPASS_FLAGS = frozenset(
    {
        "--dangerously-skip-permissions",
        "--yolo",
        "--skip-trust",
        "-y",
        "--dangerously-bypass-approvals-and-sandbox",
    }
)
#: Arguments placed straight after argv[0] on every launch. shell `-l` (a login shell).
BASE_ARGS = frozenset({"-l"})
#: Where bypass flags apply. codex has no per-launch bypass on resume, only on new.
BYPASS_ON = frozenset({"both", "new"})

#: `launch.resume.kind` → the argv it assembles after argv[0] + base args:
#:   flag            [flag, <id>]
#:   subcommand      [subcommand, <id>]
#:   positional-dir  [<cwd>, flag, <id>]        (opencode)
#:   fresh           []   — a relaunch is a fresh process; nothing to resume (shell)
RESUME_KINDS = frozenset({"flag", "subcommand", "positional-dir", "fresh"})
#: `launch.new.kind` → the argv it assembles after argv[0] + base args:
#:   pin-flag        [flag, <id>]
#:   cwd-flag        [flag, <cwd>]
#:   positional-dir  [<cwd>]
#:   bare            []
NEW_KINDS = frozenset({"pin-flag", "cwd-flag", "positional-dir", "bare"})

#: `launch.admission`. `sqlite-store-shared`: the engine's store has maintenance that must fence
#: launches (opencode, #993 — `opencode_admission`).
ADMISSION_KINDS = frozenset({"none", "sqlite-store-shared"})

#: `binary.version_flag`.
VERSION_FLAGS = frozenset({"--version", "-V", "-v", "version"})

# --- store / transcript / usage ------------------------------------------------------------------

#: `store.layout` — each names a built-in reader. One per store shape that exists today.
STORE_LAYOUTS = frozenset(
    {
        "claude-projects",
        "opencode-sqlite",
        "codex-rollouts",
        "gemini-tmp",
        "antigravity-cli",
        "kimi-code",
        "shell-records",
        "battlelab-chat",  # chat runtime: BattleLab's own per-session JSONL (#1209)
    }
)
#: Named auxiliary paths a store may declare under its root (`store.paths`).
STORE_PATH_NAMES = frozenset({"db", "log", "sessions", "archive", "index"})

#: `transcript.kind` — each names a built-in adapter in `transcript.py`.
TRANSCRIPT_KINDS = frozenset(
    {
        "claude-jsonl",
        "codex-rollout",
        "kimi-wire",
        "opencode-sqlite",
        "gemini-chat",
        "antigravity-brain",
        "battlelab-chat",  # chat runtime (#1209)
        "none",
    }
)

#: `usage.source` — the four answers `agent_usage` distinguishes. `none` is not 0 %.
USAGE_SOURCES = frozenset({"plan", "tokens", "manual", "none"})
#: `usage.kind` — each names a built-in reporter. Required for `plan`/`tokens`, forbidden otherwise.
USAGE_KINDS = frozenset(
    {
        "claude-cli-probe",
        "agy-cli-probe",
        "codex-app-server-probe",
        "codex-rollout-field",
        "opencode-store-query",
        "chat-response-tokens",  # chat runtime: token counts recorded from each response (#1209)
    }
)
#: `usage.access` — how an engine's ACCOUNT ACCESS is observed (#1167), independent of `source`:
#: whether the vendor refuses this login at all. Each names a built-in check in `agent_usage`.
USAGE_ACCESS_KINDS = frozenset({"gemini-acp-auth", "kimi-wire-auth-error"})
#: The only token a `*-cli-probe` may send the vendor CLI (`agent_usage`: "the literal string
#: `/usage` and nothing else").
USAGE_PROBE_TOKENS = frozenset({"/usage"})
_CLI_PROBE_KINDS = frozenset({"claude-cli-probe", "agy-cli-probe"})

# --- terminal / unattended / maintenance ---------------------------------------------------------

#: `terminal.repaint`. `wipe`: the TUI wipes and repaints scrollback (codex, kimi — #969).
REPAINT_KINDS = frozenset({"none", "wipe"})
#: `terminal.ready` — the first-paint rule the seed path waits on (`headless_seed._PAINTED`).
READY_KINDS = frozenset({"bytes", "claude-first-paint"})
#: `terminal.menu` — the numbered-menu parser (`screen_menus._PARSERS`, #1075).
MENU_KINDS = frozenset({"none", "claude-numbered"})
#: `terminal.permission` — the tool-permission dialog parser AND its key recipe
#: (`permission_prompts._KINDS`, #1213). Operator-only: no autonomous path reads it.
PERMISSION_KINDS = frozenset({"none", "claude-permission", "opencode-permission"})
#: `unattended.start_evidence` — the artifact that answers "did an agent start" (#989/#1050).
START_EVIDENCE_KINDS = frozenset({"none", "claude-sessions", "opencode-log"})
#: `maintenance` — store maintenance a plugin admits (#993).
MAINTENANCE_KINDS = frozenset({"sqlite-vacuum"})

# --- display -------------------------------------------------------------------------------------

#: `display.accent` — a token name, never a hex. The web maps each to its HUD colour; a manifest
#: cannot introduce a colour the design system does not have (status colours stay off-limits).
ACCENT_TOKENS = frozenset({"amber", "teal", "green", "blue", "lime", "magenta", "slate"})

# --- install / sign-in / verify (validated in P1, executed only by P5) ----------------------------

#: `install.kind`. `npm-prefix`, not `npm-global`: the artifact is staged into the plugin's OWN
#: root so §2b can bind the entrypoint to it — a global install leaves nothing to bind.
INSTALL_KINDS = frozenset({"npm-prefix", "tarball"})
#: The registry / download authorities an install may name, per kind.
INSTALL_AUTHORITIES = {
    "npm-prefix": frozenset({"registry.npmjs.org"}),
    "tarball": frozenset({"github.com"}),
}
#: `signin.kind` → the login invocations it may run (a subcommand token list, chosen, not written).
SIGNIN_KINDS = {
    "none": frozenset(),
    "cli-subcommand": frozenset({"login", "auth"}),
}
#: `verify` — the fixed check list (#853 §3). A manifest selects a subset; it never writes a check.
VERIFY_CHECKS = frozenset({"binary", "version", "store", "new", "resume", "transcript", "usage"})

#: Capability booleans, all default-deny.
CAPABILITIES = (
    "resume",
    "new",
    "archive",
    "handoff_target",
    "seed_start",
    "orchestrator_input",
    "raw_tty",
    "owns_transcript",
)
#: Capabilities a `terminal` (agentless) plugin may never assert — each one would let the app
#: author input for, or rewrite the terminal of, a bare shell.
AGENT_ONLY_CAPABILITIES = frozenset(
    {"seed_start", "orchestrator_input", "raw_tty", "owns_transcript"}
)

# --- §2b: the closed entrypoint vocabulary -------------------------------------------------------

#: Basenames a PLUGIN-SUPPLIED manifest's entrypoint may never be or resolve to: shells,
#: interpreters, and anything that runs another program with different privileges or arguments.
#: A plugin's entrypoint is a vendor CLI; any of these would turn "launch the agent" into "run
#: whatever the manifest's arguments say". First-party manifests are exempt BY PROVENANCE (the
#: in-tree `shell` engine legitimately is `bash`), never by name.
FORBIDDEN_ENTRYPOINTS = frozenset(
    {
        "sh",
        "bash",
        "dash",
        "zsh",
        "ksh",
        "mksh",
        "csh",
        "tcsh",
        "fish",
        "ash",
        "busybox",
        "env",
        "sudo",
        "doas",
        "su",
        "pkexec",
        "runuser",
        "setpriv",
        "chroot",
        "nsenter",
        "unshare",
        "setsid",
        "nohup",
        "nice",
        "ionice",
        "timeout",
        "stdbuf",
        "xargs",
        "exec",
        "python",
        "node",
        "nodejs",
        "deno",
        "bun",
        "perl",
        "ruby",
        "php",
        "lua",
        "luajit",
        "tclsh",
        "wish",
        "awk",
        "gawk",
        "mawk",
        "sed",
        "osascript",
        "pwsh",
        "powershell",
        "ssh",
        "scp",
        "rsync",
        "curl",
        "wget",
        "nc",
        "ncat",
        "socat",
        "dtach",
        "screen",
        "tmux",
        "script",
        "expect",
        "gdb",
        "strace",
        "ltrace",
        "systemd-run",
    }
)
#: Prefix families for versioned interpreters (`python3.12`, `perl5.36`, `ruby3.2`, `node20`).
FORBIDDEN_ENTRYPOINT_PREFIXES = ("python", "perl", "ruby", "node", "php", "lua", "pypy")


def is_forbidden_entrypoint(basename: str) -> bool:
    """Is `basename` — or a versioned spelling of it (`tclsh8.6`, `bash5`, `busybox.static`) — a
    shell, interpreter or privilege tool? The version-suffix rule applies to EVERY listed name."""
    b = basename.lower()
    for p in (*FORBIDDEN_ENTRYPOINTS, *FORBIDDEN_ENTRYPOINT_PREFIXES):
        if b == p:
            return True
        rest = b[len(p) :]
        if b.startswith(p) and rest and (rest[0].isdigit() or rest[0] in ".-_"):
            return True
    return False


#: Engine ids that belong to the in-tree roster. A local manifest may never take one — checked
#: against this fixed set, not against whichever in-tree manifests happened to load, so a missing
#: or broken in-tree manifest cannot let a local plugin claim `claude` (independent review of
#: PR #1112). The loader also reserves every directory under `plugins/first_party/`.
RESERVED_IDS = frozenset(
    {"claude", "opencode", "codex", "gemini", "antigravity", "kimi", "shell", "apichat"}
)


def is_cli_probe(kind: str) -> bool:
    return kind in _CLI_PROBE_KINDS
