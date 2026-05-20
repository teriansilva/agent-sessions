# agent-sessions

Mobile-first AI-coding session organizer behind **https://terminal.example.com**.

Sidebar: every Claude Code session on disk (`~/.claude/projects/**/*.jsonl`), grouped by project, sticky-first then by recency. Click a row → opens (or focuses) a Zellij tab with `claude --resume <uuid>` in the right cwd. Embedded terminal pane is ttyd attached to a persistent Zellij session named `agent-main`.

Opencode is a planned second engine source — see [`operator-docs#61`](https://git.example.com/superstatus.io/operator-docs/issues/61).

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

## Conventions

See `CLAUDE.md` (= `AGENTS.md` symlink). Key points:

- **Shell-free** Zellij wrapper. Pinned by tests + a CI grep.
- **Tab lookup by short-UUID prefix** so renames don't duplicate tabs.
- **Scope discipline:** PR 3 ships scan/list + open + auth only. Rename / sticky / archive / close / mobile drawer are PR 4 (see #56).
