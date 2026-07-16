# Infrastructure & install footprint

What BattleLab (`agent-sessions`) consists of, what the installer puts on your machine, and
exactly which external infrastructure — if any — an installation talks to, in both operating
modes:

- **Self-host** (the default) — everything runs on your machine; you provide reachability.
- **Stream via BattleLab (Home Free)** — opt-in; your machine keeps everything local but holds
  an outbound, end-to-end-encrypted connection to a blind public relay so you can reach it from
  anywhere without opening ports.

Read [`SECURITY.md`](../SECURITY.md) alongside this: BattleLab launches AI coding agents with
permission bypass by design, so every reachability decision below is a security decision.

## The pieces

```
 your machine                                          BattleLab public infra (optional)
┌──────────────────────────────────────────┐          ┌────────────────────────────────┐
│  agent-sessions.service (systemd --user) │          │  battlelab.superstatus.io      │
│  ├─ FastAPI app + React SPA              │          │  ├─ landing page + install.sh  │
│  ├─ ws/dtach terminal bridge             │          │  └─ /connect viewer page       │
│  └─ engine providers (claude, opencode,  │          │                                │
│     codex, gemini, antigravity)          │          │  relay.battlelab.superstatus.io│
│                                          │          │  └─ blind WebSocket relay      │
│  agent-sessions-homefree.service         │──WSS────▶│     (sees ciphertext only)     │
│  (stream mode only, outbound-only)       │          └────────────────────────────────┘
└──────────────────────────────────────────┘
```

The app itself is a single FastAPI process serving the built React SPA, the JSON API, and the
`/ws/term` WebSocket terminal bridge. Agent sessions run under `dtach` masters owned by the app,
so they survive browser disconnects and app restarts. There is no database — session data comes
from the agents' own on-disk history plus small JSON sidecars.

## What the installer does on your machine

The install is **rootless and user-level**: no system daemon, and the app itself installs
nothing system-wide. `sudo` can appear in two ways. When a prerequisite is missing, the
installer attempts a best-effort distro package install (`sudo apt-get` / `dnf` / `pacman`) for
`git` (required), `dtach`, Node/npm, and the `python3-venv` module; a missing Node then falls
back to a vendored self-contained toolchain under the install root, while a missing
Python ≥ 3.11 skips the distro attempt entirely and goes straight to a vendored interpreter
(asked first on a terminal; ~30 MB, no root). Separately, choosing a non-localhost bind offers
to open the firewall port — always explicitly prompted, exact command shown first, default No.
With all prerequisites already present, the only possible `sudo` is that explicitly-accepted
firewall step.

Steps, in order (`install.sh`, idempotent — re-running upgrades in place):

1. **Fresh-vs-upgrade detection** — keyed off a *completed* prior install (a valid `current`
   symlink), so a failed first attempt retries as fresh.
2. **Bind address** — defaults to `127.0.0.1:8765`. An interactive install offers a detected LAN
   IP or all interfaces behind an explicit security warning; a piped `curl | sh` always keeps
   localhost. The choice is persisted and never silently reverted by later re-runs or updates.
3. **Prerequisites** — uses your system `git`/`python3 ≥ 3.11`/Node `≥ 20` when present.
   Missing `git`/`dtach`/Node get a distro package-install attempt (see the `sudo` note above);
   Node then falls back to a vendored toolchain, while a missing Python is vendored directly
   with no distro attempt — both self-contained under the install root (no system change):
   Python from `astral-sh/python-build-standalone` (SHA-256-pinned), Node from `nodejs.org`.
4. **Build a release** — clones the selected ref, builds the SPA and the Python venv into an
   immutable, self-contained release directory.
5. **Credentials** — a fresh install generates the admin password, prints it **once**, and stores
   only the PBKDF2 hash in the `env` file (mode `0600`). First login forces a password change.
6. **Atomic flip** — a `rename(2)` swap of the `current` symlink makes the new release live;
   the newest three releases are retained in total (the live one plus, normally, two prior)
   for one-step rollback.
7. **Engine discovery** — `agent-sessions doctor` records the paths of installed agent CLIs.
8. **Service** — writes and starts the `systemctl --user` unit, health-checks `/healthz`, and
   **rolls back automatically** to the previous release if the new one doesn't come up.
9. **Remote-access choice** — self-host by default; the Home Free stream channel is set up only
   when explicitly chosen (interactive option 2, or `AGENT_SESSIONS_REMOTE=stream`). A
   non-interactive install without that explicit `AGENT_SESSIONS_REMOTE=stream` opt-in never
   contacts a relay.

### On-disk footprint

```
~/.local/share/agent-sessions/
├── releases/<ts>-<sha>/{src,venv}    immutable releases; current → one of them
├── current                           atomic symlink; flip = upgrade / rollback
├── env                               0600 — secret key, admin hash, host/port/origin, engine paths
├── .toolchain/                       vendored Python/Node (only if the host lacked them)
├── pty/                              dtach sockets + runtime state
└── homefree/                         stream mode only — console_name, access_key, identity (0600)
~/.config/systemd/user/agent-sessions.service        (+ agent-sessions-homefree.service)
~/.config/agent-sessions/             per-user prefs (onboarding, UI settings)
~/.agent-sessions/                    cache — scrollback mirrors + uploads
```

**Never touched:** your agents' own data — `~/.claude`, `~/.codex`, `~/.config/opencode`,
`~/.gemini`, etc. BattleLab reads agent history where needed but installing, upgrading, and
uninstalling never deletes a transcript. `uninstall.sh` removes everything in the list above
(units, releases, env, prefs, cache, live dtach masters) and nothing else, and requires
confirmation on a terminal.

### Updates

Self-update (in-app under Settings → System → Updates, `agent-sessions autoupdate`, or a
re-run of the installer) resolves the channel's latest ref — the highest `v*` tag on `stable`
(default) or the tip of `main` — builds it as a new release, flips `current`, restarts,
health-checks, and rolls back automatically on failure. The update source is the git repo the
install came from (`AGENT_SESSIONS_REPO`, default the public GitHub mirror).

## Mode 1 — self-host (default)

No BattleLab-owned infrastructure is involved at runtime (the full egress picture, including
update checks and the agents' own provider traffic, is in the table below). The app binds
`127.0.0.1:8765` and you
provide reachability: typically a reverse proxy terminating TLS in front of it (a worked nginx
example ships in [`deploy/nginx.example.conf`](../deploy/nginx.example.conf)), or a LAN/VPN bind
chosen at install time (the installer then offers to open the firewall port, printing the exact
`sudo` command first). Login is username + password (optional TOTP 2FA), with the cookie/CSRF/
same-origin enforcement described in `SECURITY.md`.

Outbound connections in this mode — **no BattleLab-owned infrastructure is on the runtime data
path**, but the box is not egress-free. For firewall / privacy planning:

| When | Destination | Purpose |
| --- | --- | --- |
| Install / update | `battlelab.superstatus.io` | fetching `install.sh` (if installed that way) |
| Install / update | the source repo (`AGENT_SESSIONS_REPO`) | `git clone` / `ls-remote` for releases |
| Install only, when needed | `github.com` (python-build-standalone), `nodejs.org` | vendored toolchains, only if the host lacks Python ≥ 3.11 / Node ≥ 20 |
| Runtime — update checks | the source repo | `git ls-remote` on a manual update check, or daily while auto-update is enabled |
| Runtime — optional AI features | the operator-configured OpenAI-compatible endpoint | AI session review / auto-sort / Pulse — only if you configure them |
| Runtime — the agent CLIs themselves | each agent's own model provider (Anthropic, OpenAI, Google, …) | the coding agents BattleLab launches talk to their providers exactly as they would from a plain terminal |

## Mode 2 — stream via BattleLab (Home Free)

Opt-in remote access without port-forwarding, dynamic DNS, or a VPN. The installer:

- generates a **console name** (a random callsign like `viper-8231`) and a **machine-generated
  access key** (≥ 128-bit; never user-chosen, never sent anywhere) into
  `~/.local/share/agent-sessions/homefree/` with `0600` modes;
- requires the app to stay **loopback-bound** (`127.0.0.1`) — stream mode refuses any other bind;
- switches the local app to `AGENT_SESSIONS_AUTH_MODE=none` so the access key is the **single
  gate** (disclosed on the credentials banner at install time — CSRF and Origin checks stay on);
- installs and starts a second user unit, `agent-sessions-homefree.service`.

At runtime the Home Free agent holds one **outbound-only** WebSocket to the relay
(`wss://relay.battlelab.superstatus.io/relay/ws` by default), registers the console name under a
long-term Ed25519 identity key, and redials with exponential backoff when the connection drops.
No inbound port is ever opened.

To connect, you open the public viewer page (`https://battlelab.superstatus.io/connect`), enter
the console name + access key, and the browser and the agent run an end-to-end encrypted
handshake **through** the relay (X25519 ephemeral keys + HKDF-SHA256 with the access key as PSK,
AES-256-GCM transport with strictly-increasing counters — full wire spec in
[`home-free-handshake.md`](home-free-handshake.md)). Properties that matter operationally:

- **The relay is blind.** It forwards opaque binary frames; the access key, your login cookie,
  terminal traffic, and file contents never exist in plaintext outside your machine and your
  browser. A regression test asserts a plaintext search of relayed frames finds ciphertext only.
- **Forward secrecy + replay resistance** — ephemeral keys per session; out-of-order or replayed
  frames are dropped.
- **The tunnel is not generic.** On the agent side each stream is reverse-proxied to a **fixed**
  target — the box's own loopback app. The path/headers a viewer sends can never redirect the
  tunnel at other loopback services.
- **The access key grants full control of the box** (it's the only gate, and the app launches
  agents with permission bypass). Treat it like an SSH private key.

Outbound connections in this mode: everything in the self-host table, plus the persistent WSS
connection to the relay. Self-hosters can point `AGENT_SESSIONS_RELAY_URL` /
`AGENT_SESSIONS_CONNECT_URL` at their own relay + connect page; the protocol does not depend on
BattleLab's public instance.

## Public BattleLab infrastructure (for reference)

The hosted pieces are deliberately small and stateless with respect to your data:

- **`battlelab.superstatus.io`** — the landing page, `install.sh`/`uninstall.sh`, and the
  `/connect` viewer page (a static shell that runs the E2E crypto in your browser via WebCrypto).
- **`relay.battlelab.superstatus.io`** — the blind relay: pairs a console name with viewers and
  forwards encrypted frames. It stores no session content and cannot decrypt any.
- **The public GitHub mirror** (`AGENT_SESSIONS_REPO` default) — the source releases and the
  self-update target.

None of these are on the data path in self-host mode, and in stream mode they see ciphertext
only.
