# Self-hosting agent-sessions (BattleLab)

A rootless, user-level install: no system daemon, no root (only optional, clearly-prompted sudo:
installing the `venv` module if missing, and — if you accept it — adding a firewall rule for a
non-localhost bind). Everything lives under `~/.local/share/agent-sessions/` and runs as a
`systemctl --user` service bound to `127.0.0.1` by default. An **interactive** install offers to
bind a different address (a detected LAN IP, or all interfaces) behind a security warning; a piped
`curl | sh` install always keeps the safe localhost default — see [Bind address](#bind-address).

> **Read the security model first.** agent-sessions launches AI-coding agents with permission
> bypass by design and is a single-admin, non-multi-tenant tool. It **must** sit behind a reverse
> proxy that provides TLS + auth. See the "Security / trust model" section of
> [`README.md`](README.md).

## Prerequisites

- Linux with `systemd` (user services) — the install is rootless and uses `systemctl --user`.
- `git` and `python3 ≥ 3.11`. If the `venv` module is missing, the installer offers to
  `apt-get`/`dnf` install it — one of only two optional, prompted sudo steps (the other is opening
  the firewall port for a non-localhost [bind](#bind-address)).
- A reverse proxy (e.g. nginx) terminating TLS in front of the app.
- The agent CLIs you want to manage (Claude Code, opencode, codex, gemini, antigravity/`agy`) installed on the host;
  the installer's `doctor` step discovers their paths automatically.

## Install

```sh
# Always read a script before piping it to a shell.
curl -fsSL https://battlelab.superstatus.io/install.sh -o install.sh
less install.sh
sh install.sh
```

To install from a fork or mirror, set `AGENT_SESSIONS_REPO=https://github.com/<you>/agent-sessions.git`
before running the script.

On a fresh install the credentials are printed **once** (only the PBKDF2 hash is stored). The
first login forces a password change before anything else is reachable.

## Bind address

By default the app binds `127.0.0.1` and you reach it through a reverse proxy (TLS + auth). When
the installer is run **interactively** (a real terminal — not a piped `curl | sh`) and you didn't
pin `AGENT_SESSIONS_HOST`, it lists this host's addresses and asks where to listen:

```
Where should agent-sessions listen for connections?
  1) 127.0.0.1   localhost only — default, recommended (put a reverse proxy / TLS in front)
  2) 0.0.0.0     all interfaces — reachable from anywhere this host is
  3) 10.0.0.42   this address only
Choose an option [1]:
```

- **Anything other than `127.0.0.1` exposes a shell-equivalent surface** (the app launches agents
  with permission bypass), so the installer warns and asks for an explicit `y` before binding. Only
  expose it on a network you trust (LAN / VPN), and keep TLS + auth in front for anything wider —
  enabling 2FA is recommended once it's reachable beyond localhost.
- Picking an address also sets `AGENT_SESSIONS_ORIGIN` to match, so the same-origin / CSRF checks
  pass when you reach the app over the network. For `0.0.0.0` it uses your **primary (default-route)
  address** — the one another machine actually reaches this host on, so a host with docker bridges
  or a VPN doesn't get handed an unreachable internal IP. If you reach it via a different name,
  re-run with `AGENT_SESSIONS_ORIGIN=http://<that-host>:<port>`.
- After a non-localhost bind the installer **offers to open the port in the host firewall** (`ufw`
  on Debian/Ubuntu, `firewalld` on Fedora/RHEL) — it prints the exact `sudo` command and only runs
  it if you accept (default No); on other firewalls it prints a manual `iptables` rule. Otherwise
  the app binds the address but a firewall can still silently drop connections from other machines.
- The choice is **persisted and kept across upgrades / autoupdate** — a re-run won't silently
  revert it to localhost.
- **Non-interactive installs are unchanged**: with no tty, with `AGENT_SESSIONS_ASSUME_YES=1`, or
  with `AGENT_SESSIONS_HOST` set explicitly, the prompt is skipped and the default/explicit bind is
  used. Set `AGENT_SESSIONS_HOST` (and usually `AGENT_SESSIONS_ORIGIN`) up front to script it.

## What the installer does

- Clones/builds the selected ref into a self-contained, immutable release directory and flips an
  atomic `current` symlink to it:

  ```
  ~/.local/share/agent-sessions/
  ├── releases/<ts>-<sha>/{src,venv}   one self-contained release per build
  ├── current → releases/<ts>-<sha>     atomic symlink (rename(2)); flip = upgrade/rollback
  └── env                               0600; secret + admin hash + host/port/origin
  ~/.config/systemd/user/agent-sessions.service
  ```

- Builds the web SPA and installs the Python venv.
- Writes `~/.config/systemd/user/agent-sessions.service` and starts it (unless
  `AGENT_SESSIONS_NO_SERVICE=1`).
- Runs `agent-sessions doctor` to discover installed agent CLIs and record their paths in `env`.
- Is **idempotent**: re-running builds a new release, flips `current`, keeps the prior releases
  (3 by default) for rollback, and **leaves existing credentials untouched**.

## Configuration (env vars)

Set at install time (persisted into `env`):

| Var | Purpose |
| --- | --- |
| `AGENT_SESSIONS_HOST` / `_PORT` | Bind address/port (default `127.0.0.1:8765`). Setting `_HOST` skips the interactive [bind prompt](#bind-address). |
| `AGENT_SESSIONS_ORIGIN` | Public origin for CSRF / `Origin` checks, e.g. `https://your-domain.example`. |
| `AGENT_SESSIONS_AUTH_MODE` | `single-user` (default — username + password login) or `none` (no login; the admin session is auto-established). **`none` = trust the network: localhost / behind-VPN only.** CSRF + `Origin` checks stay on. |
| `AGENT_SESSIONS_HOME` | Install root (default `~/.local/share/agent-sessions`). |
| `AGENT_SESSIONS_WEB_DIST` | Override the built SPA directory (default the release's `web/dist`). |
| `AGENT_SESSIONS_RUNTIME_DIR` | Runtime/socket dir for the dtach session bridge. |
| `AGENT_SESSIONS_CHANNEL` | `stable` (tags — default) or `main`. |
| `AGENT_SESSIONS_REF` | Pin an exact tag/branch/sha. |
| `AGENT_SESSIONS_REPO` | Source repo URL (for private mirrors). |
| `AGENT_SESSIONS_NO_SERVICE=1` | Install without touching systemd. |
| `AGENT_SESSIONS_AUTOUPDATE=1` | Install the opt-in autoupdate timer (`AGENT_SESSIONS_AUTOUPDATE_ONCALENDAR=daily` to tune cadence). Default off. |

The engine CLI binary paths are recorded automatically by `doctor`; you don't normally set them by
hand.

## Put it behind nginx

The app binds localhost and does **not** terminate TLS or rate-limit. Front it with a reverse
proxy. A worked example with TLS + the required WebSocket upgrade headers for `/ws/` is in
[`deploy/nginx.example.conf`](deploy/nginx.example.conf) — copy it, set your `server_name` and cert
paths, and point `proxy_pass` at the app's bind address. **Rate-limit `/login`** at the proxy.

## Updating

Self-update moves to the **channel's latest** release, flips `current`, restarts, health-checks
`/healthz`, and **rolls back** automatically if the new release fails.

- **In-app:** version + check/apply control on the dashboard.
- **CLI:** `agent-sessions autoupdate` (apply only if newer).
- **Re-run the installer:** also upgrades; without `AGENT_SESSIONS_AUTOUPDATE=1` it tears the
  autoupdate timer back down.

## Rollback & emergency-disable

Releases are immutable; `current` is just a symlink, so rollback is a one-step re-point:

```sh
P=~/.local/share/agent-sessions
ls -1dt $P/releases/*/                       # newest first; pick the known-good one
ln -s $P/releases/<ts>-<sha> $P/.current.rb && mv -Tf $P/.current.rb $P/current
systemctl --user restart agent-sessions.service
```

Emergency-disable:

```sh
systemctl --user stop    agent-sessions.service          # take the app down now
systemctl --user disable agent-sessions.service          # …and keep it down across logins
systemctl --user disable --now agent-sessions-update.timer   # stop autoupdate only
journalctl --user -u agent-sessions.service -f           # logs
```

## Lost the password?

Reset from the host — never pass the password on the command line (it leaks via shell history /
`ps`):

```sh
~/.local/share/agent-sessions/current/venv/bin/agent-sessions reset-password --prompt   # interactive, no echo
… reset-password --stdin    # scriptable: read one line from stdin
… reset-password            # generate a random one and print it once
```
