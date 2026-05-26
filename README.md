# agent-sessions

Mobile-first AI-coding session organizer behind **https://terminal.example.com**.

A React + Vite SPA. Sidebar: every session from each installed engine — Claude Code (`~/.claude/projects/**/*.jsonl`), opencode (SQLite at `~/.local/share/opencode/opencode.db`, read-only), codex, and gemini — grouped by project, sticky-first then by recency, with a per-row engine badge + agent filter. The open session lives in the URL (`/s/:engine/:id`); clicking a row attaches to it. The embedded terminal is **self-owned** — xterm.js over a websocket (`/ws/term/{sid}`) bridged to a per-session `dtach` PTY that resumes the engine in the right cwd (`claude --resume <uuid>` / `opencode <dir> --session <ses_id>` / …). No ttyd, no Zellij.

Engines live behind a small provider interface (`engines.py`); identity is engine-qualified `<engine>:<native_id>` (e.g. `claude:<uuid>`, `opencode:<ses_id>`). opencode is **read-only with respect to its own DB** — the sidebar never writes `opencode.db`. Archive is refused for opencode ids (our archive physically moves the Claude JSONL, which opencode has no equivalent for); the title/sticky **sidecar** overlay still works for any engine, since that's our `metadata.json`, not opencode's data. Adding an engine = one new provider in the registry. History: `agent-sessions#10` (vision) → `#11` (abstraction) → `#12` (opencode).

## Install & operate

Rootless, user-level — no system daemon, no root. The installer drops everything under `~/.local/share/agent-sessions/`, runs the app as a `systemctl --user` service, and binds `127.0.0.1:8765` (put a reverse proxy / TLS in front yourself — it does **not** configure nginx).

```sh
curl -fsSL https://github.com/teriansilva/agent-sessions/raw/branch/main/install.sh | sh
```

Prereqs: `git` and `python3 ≥ 3.11`. If the `venv` module is missing the installer offers to `apt-get`/`dnf` install it (the **only** sudo step, and it's prompted). On a fresh install it prints the generated admin credentials **once**:

```
agent-sessions 0.3.1 installed.
  URL:      http://127.0.0.1:8765
  username: admin
  password: <generated>            ← shown once; only the PBKDF2 hash is stored
```

**First login** forces a password change before anything else is reachable. Lost it? Reset from the host (never pass the password on the command line — it leaks via shell history/`ps`):

```sh
~/.local/share/agent-sessions/current/venv/bin/agent-sessions reset-password --prompt   # interactive, no echo
… reset-password --stdin    # scriptable: read one line from stdin
… reset-password            # generate a random one and print it once
```

### What the installer creates

```
~/.local/share/agent-sessions/
├── releases/<ts>-<sha>/{src,venv}   one self-contained release per build
├── current → releases/<ts>-<sha>     atomic symlink (rename(2)); flip = upgrade/rollback
└── env                               0600; secret + admin hash + host/port/origin
~/.config/systemd/user/agent-sessions.service
```

Re-running the installer is **idempotent**: it builds a new release dir, flips `current`, keeps the prior releases (3 by default) for rollback, and **leaves existing credentials untouched**. It also runs `agent-sessions doctor` each time to (re)discover installed agent CLIs (claude/opencode/codex) and record their paths in `env`.

Install-time knobs (env vars): `AGENT_SESSIONS_CHANNEL` (`stable` tags — default — or `main`), `AGENT_SESSIONS_HOST`/`_PORT`/`_ORIGIN`, `AGENT_SESSIONS_HOME`, `AGENT_SESSIONS_REF` (pin an exact tag/branch/sha), `AGENT_SESSIONS_NO_SERVICE=1` (install without touching systemd).

### Updating

Self-update never runs arbitrary input — it only moves to the **channel's latest** release, flips `current`, restarts, health-checks `/healthz`, and **rolls back** to the prior release if the new one fails.

- **In-app:** the dashboard shows the version + a check/apply control (`/api/version`, `/api/update/check`, `/api/update/apply` — authed + CSRF + origin-gated).
- **CLI:** `agent-sessions autoupdate` (check the channel, apply only if newer).
- **Opt-in autoupdate timer:** pass `AGENT_SESSIONS_AUTOUPDATE=1` at install (optionally `AGENT_SESSIONS_AUTOUPDATE_ONCALENDAR=daily`) and a `agent-sessions-update.timer` runs the same guarded apply on a schedule. Default **off**; re-running the installer without the flag tears the timer back down.

### Rollback & emergency-disable

Releases are immutable directories; `current` is just a symlink, so rollback is a one-step re-point — no rebuild:

```sh
P=~/.local/share/agent-sessions
ls -1dt $P/releases/*/                       # newest first; pick the known-good one
# atomic re-point — same temp-link + rename(2) the installer uses, so a concurrent
# start/health-check never sees a missing `current` (bare `ln -sfn` unlinks first):
ln -s $P/releases/<ts>-<sha> $P/.current.rb && mv -Tf $P/.current.rb $P/current
systemctl --user restart agent-sessions.service
```

(A failed self-/auto-update already rolls back automatically; this is the manual path.)

**Emergency-disable** — stop serving and/or stop auto-updating:

```sh
systemctl --user stop    agent-sessions.service          # take the app down now
systemctl --user disable agent-sessions.service          # …and keep it down across logins
systemctl --user disable --now agent-sessions-update.timer   # stop autoupdate only
journalctl --user -u agent-sessions.service -f           # logs
```

> RO deployment note: the live `terminal.example.com` host predates this installer and is deployed via the app repo's `deploy.yml` (CI) — its host-specific rollback/ops runbook is in [`operator-docs/example-infrastructure/agent-sessions/`](https://git.example.com/superstatus.io/operator-docs/src/branch/main/example-infrastructure/agent-sessions).

## Where things live

- **App code:** here (`src/agent_sessions/`)
- **Deploy unit (ships with the code):** [`deploy/`](deploy/) — `agent-sessions.service` (the FastAPI app; the installer / `deploy.yml` manages it). The terminal is in-process (the ws bridge), so there's no separate terminal unit anymore.
- **Operator-facing docs + runbook + nginx vhost + our specific as-built:** [`operator-docs/example-infrastructure/agent-sessions/`](https://git.example.com/superstatus.io/operator-docs/src/branch/main/example-infrastructure/agent-sessions)
- **Design issue + Hermes review chain:** [`operator-docs#56`](https://git.example.com/superstatus.io/operator-docs/issues/56) (PR 3 of 4)
- **Deploy target:** example-host (127.0.0.1:3402), behind example-proxy at terminal.example.com
- **CI/CD:** `.forgejo/workflows/pr-validate.yml` (ruff + pytest + shell-free grep) and `.forgejo/workflows/deploy.yml` (mirrors `deploy-dashboard.yml`, targets the `[self-hosted, host]` runner on example-host)

## Layout

```
agent-sessions/
├── pyproject.toml
├── src/agent_sessions/
│   ├── main.py        FastAPI app factory (serves the React SPA + the API + ws terminal)
│   ├── scanner.py     read ~/.claude/projects/ (live + archive)
│   ├── metadata.py    sidecar JSON with fcntl.flock; title/sticky/sort_key/project_alias
│   ├── engines.py     per-engine providers (scan + launch_argv); claude/opencode/codex/gemini
│   ├── webterm.py + ptybridge.py   the ws↔PTY bridge (xterm.js over /ws/term, dtach-backed)
│   ├── auth.py        cookie + CSRF + Origin + /api/auth-check for nginx auth_request
│   └── templates/     login.html + change_password.html (the only server-rendered pages)
├── web/               React + Vite + TS SPA (built to web/dist, served by main.py)
├── tests/             pytest; subprocess.run stubbed; covers shell-free, CSRF, lock, lookup
├── deploy/
│   └── agent-sessions.service     systemd-user unit, FastAPI app (port 3402; installed by deploy.yml)
└── .forgejo/workflows/
    ├── pr-validate.yml
    └── deploy.yml
```

## Running locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Bring up the app against a fake home (no real Claude sessions touched):

```bash
export AGENT_SESSIONS_USERNAME=marcus
export AGENT_SESSIONS_PASSWORD_HASH=$(python -c "from agent_sessions.auth import hash_password; print(hash_password('hunter2'))")
export AGENT_SESSIONS_SECRET_KEY=$(python -c "import secrets; print(secrets.token_urlsafe(48))")
export AGENT_SESSIONS_ORIGIN=http://localhost:3402
uvicorn --app-dir src --host 127.0.0.1 --port 3402 agent_sessions.main:app
```

## Permission bypass (`--dangerously-skip-permissions`)

The sidebar's **New session** modal has a "bypass permissions" toggle that is **on by default**, and **resuming** a session also passes `--dangerously-skip-permissions`. This is deliberate (agent-sessions#4): it skips Claude Code's workspace-trust prompt so a session opens straight into its already-used folder, and skips per-tool permission prompts for new sessions.

This is acceptable **only** because agent-sessions is a single-user tool, on the operator's own host, behind two auth layers (nginx basic auth + the FastAPI app cookie). The toggle lets you turn bypass off per new session. The flag is asserted by the provider `launch_argv` tests (`tests/test_engines.py`) so it can't silently change. Treat the whole surface as equivalent to "a shell as user" — the same trust boundary as SSH.

## API surface

- `GET /api/sessions?limit=20&offset=0&archived=0` — flat, newest-first, paginated (`{sessions, next_offset, total, facets}`).
  Optional filters: `q` (case-insensitive title substring; trimmed, empty = no filter), `project` (exact key), `engine` (exact; `claude` / `opencode`). Filters are applied **before** `limit`/`offset` so `total` and "load more" describe the filtered set. `facets: {projects, engines}` are the distinct values over the full archived-scoped set (computed pre-filter) so the sidebar dropdowns list every option, including rows past the first page.
- `GET /api/projects` — new-session picker: scanned cwds ∪ validated `~/claude/*` (distinct from the session-list facets above)
- `POST /api/sessions/{uuid}/rename` `{title}` — persists to the sidecar
- `POST /api/sessions/{uuid}/archive` · `/unarchive` — move the JSONL between `projects/` and `projects-archive/`
- `GET /api/config` — SPA bootstrap (CSRF, `new_session_engines`, `terminal_backend`, theme); `POST /api/prefs` `{theme}`
- `WS /ws/term/{sid}` — the terminal: attach to (or, with `?new=1&cwd=&bypass=`, launch) a session's dtach PTY
- `GET /api/auth-check` — 204/401 for nginx `auth_request`; `POST /login` · `/logout`

All state-changing routes require the CSRF token + an Origin/Referer matching `AGENT_SESSIONS_ORIGIN`.

## Conventions

See `CLAUDE.md` (= `AGENTS.md` symlink). Key points:

- **Shell-free** engine launchers — providers build argv lists; the ws bridge runs them under `dtach`. Pinned by tests + a CI grep.
- **Session = URL = socket identity:** one `{engine}:{id}` ⇒ one dtach master ⇒ one writer; attach, never relaunch.
