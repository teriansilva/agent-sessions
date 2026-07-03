# Reference

A single place for the operational surface of agent-sessions: the CLI, the HTTP/WS API, the engine
providers, and every environment variable. For the overview + security model see the
[README](../README.md); for a self-host walkthrough see [INSTALL.md](../INSTALL.md).

---

## CLI — `agent-sessions <subcommand>`

The console script installed into the release venv (`…/current/venv/bin/agent-sessions`).

| Subcommand | What it does |
|---|---|
| `serve [--host H] [--port P]` | Run the FastAPI app (the systemd unit calls this). Defaults from `AGENT_SESSIONS_HOST`/`_PORT`, else `127.0.0.1:8765`. |
| `doctor` (alias `discover-engines`) `[--env FILE] [--dry-run]` | Discover installed agent CLIs (claude/codex/opencode/gemini) and record their resolved paths in the env file. Run automatically on every install. |
| `reset-password [--prompt | --stdin]` | Set a new admin password hash in the env. `--prompt` reads interactively (no echo), `--stdin` reads one line; with neither, generates a random password and prints it once. Never pass the password as an argument. |
| `clear-2fa [--file PATH]` | Remove the TOTP secrets file → disables 2FA. The lockout escape hatch (host-only). |
| `autoupdate` | Check the configured channel and apply an update only if newer (the timer entrypoint). |
| `version` | Print the version (from `setuptools_scm`; a release tag → `X.Y.Z`, otherwise a dev version). |

---

## HTTP / WebSocket API

All state-changing routes require the CSRF token **and** an `Origin`/`Referer` equal to
`AGENT_SESSIONS_ORIGIN`. Under `AGENT_SESSIONS_AUTH_MODE=none` the admin session is auto-established
(CSRF + same-origin still enforced).

### Sessions & projects
| Route | Purpose |
|---|---|
| `GET /api/sessions?limit=&offset=&archived=&q=&project=&engine=` | Flat, newest-first, paginated list: `{sessions, next_offset, total, facets}`. Filters apply before paging; `facets:{projects,engines}` cover the full set. |
| `GET /api/folders` | New-session picker (launch folders): scanned cwds ∪ validated project roots. |
| `GET/POST/PATCH/DELETE /api/projects` | Project entities (#361): `{id,name,color,folders,archived,session_count}`; `?include_archived=1` opts archived in. |
| `PATCH /api/sessions/{sid}/metadata` `{project_id}` | Assign/clear a session's project (sidecar-only write). |
| `POST /api/sessions/{sid}/rename` `{title}` | Persist a title to the metadata sidecar. |
| `POST /api/sessions/{sid}/favorite` · `/unfavorite` | Toggle the sidecar `sticky` flag (#122) → `{id, sticky}`; favorited sessions pin to the top of the list (sidecar-only, engine-agnostic). |
| `POST /api/sessions/{sid}/archive` · `/unarchive` | Move the Claude JSONL between `projects/` and `projects-archive/` (engine-agnostic sidecar flag for non-file engines). |
| `POST /api/sessions/archive-older` | Bulk-archive sessions older than a cutoff. |
| `GET /api/scrollback` · `POST /api/scrollback/clear` | Fetch / clear a session's on-disk scrollback. |
| `POST /api/upload` | Save a pasted/dropped file to the shared uploads dir. |

### Terminal
| Route | Purpose |
|---|---|
| `WS /ws/term/{sid}` | The terminal. Attach to a session's `dtach` PTY, or **launch** with `?new=1&cwd=&bypass=`. One `{engine}:{id}` ⇒ one master ⇒ one writer. |

### System & config
| Route | Purpose |
|---|---|
| `GET /healthz` | Liveness (`{ok:true}`). |
| `GET /api/version` · `GET /api/update/check` · `POST /api/update/apply` | Running version; channel check; spawn the guarded self-update (authed + CSRF + origin). |
| `GET /api/engines` | Every provider + `present` + `supports_new` + resolved `bin`. |
| `GET /api/config` | SPA bootstrap: CSRF, `new_session_engines`, `terminal_backend`, theme, `two_factor_enabled`, … |
| `GET /api/system` | Host/system info (best-effort). |
| `POST /api/prefs` `{theme}` | Per-user UI prefs. |

### Auth
| Route | Purpose |
|---|---|
| `POST /login` → `POST /login/totp` | Password → (if 2FA) a short-lived pre-auth cookie → TOTP/recovery code mints the full session. |
| `POST /logout` · `GET /api/auth-check` | Logout; 204/401 for nginx `auth_request`. |
| `GET`/`POST /change-password` · `POST /api/password` | First-run forced change + change-password. |
| `POST /api/2fa/enroll` → `/confirm` · `/disable` · `/recovery-codes` | TOTP enrollment (secret + recovery codes shown once), enable/disable/regenerate (the last two need a fresh `{code}`/`{password}` proof). |

---

## Engine providers

Each engine implements a small provider (`src/agent_sessions/engines/<engine>.py`), registered in
`registry.py`. Identity is engine-qualified: `<engine>:<native_id>`.

**Contract** (`engines/base.py`):

| Member | Meaning |
|---|---|
| `engine_id` / `id_pattern` | Engine key + the native-id shape. |
| `is_present()` | Binary on PATH/known dirs, or a readable data store. |
| `scan()` | All sessions for this engine on this host → `Session` rows. |
| `launch_argv(native_id, *, cwd, bypass)` | Resume argv for the PTY bridge. |
| `supports_new` | Whether "New session" is offered. |
| `new_launch_argv(...)` | Fresh-session launch argv (if `supports_new`). |
| `new_session_reconciles` | The engine mints its own id → launch under a `new-<uuid>` placeholder + reconcile (opencode, codex). |
| `snapshot_session_ids(cwd)` / `reconcile_new_session(cwd, snapshot)` | The pre-launch snapshot + post-launch diff that adopts the real id (for reconciling engines). |
| `archive` / `unarchive` | Move the store (claude) or set the sidecar flag. |

**Per engine:**

| Engine | Store | New session | Transcript scroll-up |
|---|---|---|---|
| **claude** | `~/.claude/projects/**/*.jsonl` | ✅ pins a caller id (`--session-id`) | ✅ JSONL |
| **codex** | `~/.codex/sessions/**/rollout-*.jsonl` | ✅ launch-then-reconcile (`--cd`) | ✅ rollout JSONL |
| **opencode** | `~/.local/share/opencode/opencode.db` (read-only) | ✅ launch-then-reconcile | ✅ SQLite `message`/`part` |
| **gemini** | `~/.gemini/tmp/<hash>/chats/session-*.jsonl` | ✗ (resume-only) | ✅ chat JSONL (text; gemini logs no tool calls) |

All store locations are env-overridable (`AGENT_SESSIONS_CODEX_SESSIONS_DIR`, `_OPENCODE_DB`,
`_GEMINI_TMP_DIR`) and the same path drives both the sidebar **and** the scroll-up transcript.

---

## Environment variables

### Identity & auth
| Var | Default | Notes |
|---|---|---|
| `AGENT_SESSIONS_USERNAME` | `admin` | Single admin account. |
| `AGENT_SESSIONS_PASSWORD_HASH` | — | PBKDF2 hash (set by the installer / `reset-password`). |
| `AGENT_SESSIONS_SECRET_KEY` | — | Cookie/CSRF signing secret. |
| `AGENT_SESSIONS_AUTH_MODE` | `single-user` | `single-user` (login) or `none` (trusted-network, no login). |
| `AGENT_SESSIONS_2FA_FILE` *(`…_2FA_FILE`)* | `<env-dir>/2fa.json` | TOTP secret + recovery-code hashes (`0600`). |
| `AGENT_SESSIONS_FORCE_PASSWORD_CHANGE` | `0` | First-run forced change (set once by a fresh install). |

### Network
| Var | Default | Notes |
|---|---|---|
| `AGENT_SESSIONS_HOST` / `_PORT` | `127.0.0.1` / `8765` | Bind address — keep it on loopback behind a proxy. |
| `AGENT_SESSIONS_ORIGIN` | — | Public origin; `Origin`/`Referer` must match it. |

### Install / update
| Var | Default | Notes |
|---|---|---|
| `AGENT_SESSIONS_REPO` | `https://github.com/teriansilva/agent-sessions.git` | Source to clone/update from (override for a fork/mirror). |
| `AGENT_SESSIONS_REF` | — | Pin an exact tag/branch/sha (one-shot; self-update never inherits it). |
| `AGENT_SESSIONS_CHANNEL` | `stable` | `stable` (highest `v*` tag) or `main` (bleeding edge). |
| `AGENT_SESSIONS_AUTOUPDATE` / `_AUTOUPDATE_ONCALENDAR` | `0` / `daily` | Opt-in self-update timer + its schedule. |
| `AGENT_SESSIONS_NO_SERVICE` | `0` | Install without touching systemd. |
| `AGENT_SESSIONS_HOME` | `~/.local/share/agent-sessions` | Install root. |
| `AGENT_SESSIONS_SKIP_WEB_BUILD` / `_NODE_VERSION` | — | Build knobs. |

### Engine discovery & stores
| Var | Notes |
|---|---|
| `AGENT_SESSIONS_{CLAUDE,CODEX,OPENCODE,GEMINI}_BIN` | Pin an engine CLI path (else PATH/known dirs). |
| `AGENT_SESSIONS_CODEX_SESSIONS_DIR` / `_OPENCODE_DB` / `_GEMINI_TMP_DIR` | Override each engine's store location. |
| `AGENT_SESSIONS_DTACH_BIN` | Pin the `dtach` binary. |

### Runtime / storage
| Var | Notes |
|---|---|
| `AGENT_SESSIONS_RUNTIME_DIR` | dtach socket dir (per-session PTY sockets). |
| `AGENT_SESSIONS_SCROLLBACK_DIR` | On-disk scrollback mirror. |
| `AGENT_SESSIONS_SCROLLBACK_BYTES` | Per-session raw-byte replay-ring cap (live scroll-up depth). Default 8 MiB; floored at 256 KiB (smaller values are ignored). |
| `AGENT_SESSIONS_METADATA` / `_PREFS` / `_ENV_FILE` / `_LOCK_DIR` | Sidecar JSON, per-user prefs, env-file path, single-writer locks. |
| `AGENT_SESSIONS_WEB_DIST` | Built SPA dir (`current/src/web/dist`). |
| `AGENT_SESSIONS_TRANSCRIPT_SCROLLBACK` | Enable the semantic console-style scroll-up. |
| `AGENT_SESSIONS_TRANSCRIPT_MAX_LINES` | Transcript scroll-up render cap in lines (#348). Default `20000`; non-numeric/garbage falls back to the default, values are floored at `1`. |
| `AGENT_SESSIONS_TRANSCRIPT_MAX_MESSAGES` | Max conversation messages read for the transcript render (#348). Default `2000`; same fallback/floor rules. |
| `AGENT_SESSIONS_TRANSCRIPT_TAIL_BYTES` | How much of the engine's session log tail is parsed for the transcript (#348). Default `8388608` (8 MiB); same fallback/floor rules. |
| `AGENT_SESSIONS_AI_REVIEW_TIMEOUT` | Review completion call timeout in seconds (#391). Default `120` — sized for slow local models; floored at 10. The Settings value (`ai_review.request_timeout`, 10–600 s) takes precedence when set; the env var is the fallback. `/models` keeps its own short budget. |
| `AGENT_SESSIONS_HISTORY_PAGE_TURNS` | Turns per scroll-up history page (#348 Phase 3) — the **width-independent cursor step**: a page always consumes exactly this many turns, so the same cursor selects the same turn window at any terminal width. Default `50`; floored at `1`. |
| `AGENT_SESSIONS_HISTORY_PAGE_LINES` | Rendered-lines cap per history page. Render-output cap ONLY: truncates the page's rendered text oldest-first, never moves the cursor. Default `500`. |
| `AGENT_SESSIONS_HISTORY_PAGE_BYTES` | Rendered-bytes cap per history page. Same render-only truncation rule. Default `524288` (512 KiB). |
| `AGENT_SESSIONS_TAKEOVER` | Single-active-viewer take-over for a live session. |
| `AGENT_SESSIONS_PROJECT_ROOTS` | `os.pathsep`-separated base dirs under which the new-session UI may create a project folder (#335). Empty/unset ⇒ the "New folder" feature is OFF (the `POST /api/folders/mkdir` endpoint is disabled). Folder creation is `realpath`-contained strictly under a listed root. |
| `AGENT_SESSIONS_SESSION_TTL` · `_REAP_*` | Idle-session reaper tunables. |
| `AGENT_SESSIONS_AI_REVIEW_LOOP` | Kill-switch for the periodic AI review loop (#356). `0` ⇒ the background task is never started, overriding the Settings `enabled` toggle; any other value (default) arms the loop, which still only reviews while AI review is enabled + configured in Settings. Manual "Review now" is unaffected. |
| `AGENT_SESSIONS_INSTANCE` | Label for running multiple instances on one host. |

> Source of truth is the code — `grep -rhoE 'AGENT_SESSIONS_[A-Z_]+' src/`. The installer seeds the
> load-bearing ones into `<home>/env` (`0600`) and refreshes engine paths via `doctor`.
