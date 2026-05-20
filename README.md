# agent-sessions

Mobile-first AI-coding session organizer behind **https://terminal.example.com**.

Sidebar: every session from each installed engine — Claude Code (`~/.claude/projects/**/*.jsonl`) and opencode (SQLite at `~/.local/share/opencode/opencode.db`, read-only) — grouped by project, sticky-first then by recency, with a per-row engine badge + agent filter. Click a row → opens (or focuses) a Zellij tab resuming that session in the right cwd (`claude --resume <uuid>` / `opencode <dir> --session <ses_id>`). Embedded terminal pane is ttyd attached to a persistent Zellij session named `agent-main`.

Engines live behind a small provider interface (`engines.py`); identity is engine-qualified `<engine>:<native_id>` (e.g. `claude:<uuid>`, `opencode:<ses_id>`). opencode is **read-only with respect to its own DB** — the sidebar never writes `opencode.db`. Archive is refused for opencode ids (our archive physically moves the Claude JSONL, which opencode has no equivalent for); the title/sticky **sidecar** overlay still works for any engine, since that's our `metadata.json`, not opencode's data. Adding an engine = one new provider in the registry. History: `agent-sessions#10` (vision) → `#11` (abstraction) → `#12` (opencode).

## Where things live

- **App code:** here (`src/agent_sessions/`)
- **Operator-facing docs + runbook + nginx vhost + the Zellij/ttyd pieces of the install:** [`operator-docs/example-infrastructure/agent-sessions/`](https://git.example.com/superstatus.io/operator-docs/src/branch/main/example-infrastructure/agent-sessions)
- **Design issue + Hermes review chain:** [`operator-docs#56`](https://git.example.com/superstatus.io/operator-docs/issues/56) (PR 3 of 4)
- **Deploy target:** example-host (127.0.0.1:3402), behind example-proxy at terminal.example.com
- **CI/CD:** `.forgejo/workflows/pr-validate.yml` (ruff + pytest + shell-free grep) and `.forgejo/workflows/deploy.yml` (mirrors `deploy-dashboard.yml`, targets the `[self-hosted, host]` runner on example-host)

## Layout

```
agent-sessions/
├── pyproject.toml
├── src/agent_sessions/
│   ├── main.py        FastAPI app factory
│   ├── scanner.py     read ~/.claude/projects/ (live + archive)
│   ├── metadata.py    sidecar JSON with fcntl.flock; title/sticky/sort_key/project_alias
│   ├── zellij.py      open-or-switch wrapper. Shell-free; short-UUID prefix lookup
│   ├── auth.py        cookie + CSRF + Origin + /api/auth-check for nginx auth_request
│   └── templates/     index.html (Alpine) + login.html
├── tests/             pytest; subprocess.run stubbed; covers shell-free, CSRF, lock, lookup
├── deploy/
│   └── agent-sessions.service     systemd-user unit (port 3402)
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

This is acceptable **only** because agent-sessions is a single-user tool, on the operator's own host, behind two auth layers (nginx basic auth + the FastAPI app cookie). The toggle lets you turn bypass off per new session. The flag is asserted by `tests/test_zellij.py` so it can't silently change. Treat the whole surface as equivalent to "a shell as user" — the same trust boundary as SSH.

## API surface

- `GET /api/sessions?limit=20&offset=0&archived=0` — flat, newest-first, paginated (`{sessions, next_offset, total, facets}`).
  Optional filters: `q` (case-insensitive title substring; trimmed, empty = no filter), `project` (exact key), `engine` (exact; `claude` / `opencode`). Filters are applied **before** `limit`/`offset` so `total` and "load more" describe the filtered set. `facets: {projects, engines}` are the distinct values over the full archived-scoped set (computed pre-filter) so the sidebar dropdowns list every option, including rows past the first page.
- `GET /api/projects` — new-session picker: scanned cwds ∪ validated `~/claude/*` (distinct from the session-list facets above)
- `POST /api/sessions/{uuid}/open` — open-or-switch (resume, bypass on)
- `POST /api/sessions/{uuid}/rename` `{title}` — persists to the sidecar
- `POST /api/sessions/{uuid}/archive` · `/unarchive` — move the JSONL between `projects/` and `projects-archive/`
- `POST /api/projects/new` `{cwd, name, bypass_permissions}` — spawn a fresh session
- `GET /api/auth-check` — 204/401 for nginx `auth_request`; `POST /login` · `/logout`

All state-changing routes require the CSRF token + an Origin/Referer matching `AGENT_SESSIONS_ORIGIN`.

## Conventions

See `CLAUDE.md` (= `AGENTS.md` symlink). Key points:

- **Shell-free** Zellij wrapper. Pinned by tests + a CI grep.
- **Tab lookup by short-UUID prefix** so renames don't duplicate tabs.
- **Scope discipline:** PR 3 ships scan/list + open + auth only. Rename / sticky / archive / close / mobile drawer are PR 4 (see #56).
