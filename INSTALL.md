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
- The agent CLIs you want to manage (Claude Code, opencode, codex, gemini, antigravity/`agy`, Kimi Code/`kimi`) installed on the host;
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

### Vendored toolchain — what is trusted, and how

The installer prefers what the host already has. Only when there is no usable system Python
(>= 3.11) or Node (>= 20) does it vendor a private one into `~/.local/share/agent-sessions/.toolchain`
— rootless, no system change.

**Both vendored downloads are SHA-256 pinned in `install.sh` and verified before they are
unpacked.** A `curl | sh` install auto-proceeds with no tty, so a mutable release URL protected
by TLS alone is not a sufficient trust root: TLS proves you reached `nodejs.org` / the
`python-build-standalone` release, not that the bytes there are the bytes that were reviewed.
Verification is **fail-closed** at every branch:

| Condition | Behaviour |
|---|---|
| Digest matches the pin | Unpack and use |
| Digest does not match | **Refuse**, delete the download, exit non-zero — before `tar` runs |
| No `sha256sum` / `shasum` on the host | **Refuse** — an unverifiable download is not a fallback |
| No pin for that version/arch | **Refuse** — see below |

The pins live beside the version constants they belong to (`NODE_VERSION`, `PY_VERSION` +
`PBS_TAG`); bump them together. Because a pin is per version, **`AGENT_SESSIONS_NODE_VERSION`
pointing at an unpinned version fails closed** rather than silently skipping verification — an
override that disabled the check would be a hole wide enough to drive the whole attack through.
If you need a different Node, install it on the host: a system Node >= 20 is preferred over
vendoring and never reaches this path.

Beyond the toolchain, an install is a `git clone` of a ref from `AGENT_SESSIONS_REPO` over
HTTPS, and the transport is the trust root for the source itself.

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
| `AGENT_SESSIONS_SCOPE_PROPERTIES` | Per-session systemd scope limits (default `TasksMax=2048`). See below. |

The engine CLI binary paths are recorded automatically by `doctor`; you don't normally set them by
hand.

### Per-session resource limits

On a host with a systemd user manager, each session's agent runs in its own transient scope, so a
runaway session exhausts its **own** budget rather than the host's. The default budget is
`TasksMax=2048`; `AGENT_SESSIONS_SCOPE_PROPERTIES` overrides it with space-separated
`Key=Value` systemd properties:

```sh
# ~/.local/share/agent-sessions/env — then: systemctl --user restart agent-sessions
AGENT_SESSIONS_SCOPE_PROPERTIES=TasksMax=2048 MemoryHigh=8G
```

Two things to know:

- **It replaces the default, it does not merge** — restate `TasksMax=` when adding a property.
- **Upgrades preserve it.** `install.sh` only adds keys that are absent from `env`, so a tuned
  value survives a reinstall.

Memory properties are deliberately not defaulted (agents legitimately run heavy builds); opt in
after checking that your host delegates the `memory` controller to the user manager. Full detail,
including why the task budget is what it is: [`docs/session-handling.md`](docs/session-handling.md).

## Put it behind nginx

The app binds localhost and does **not** terminate TLS or rate-limit. Front it with a reverse
proxy. A worked example with TLS + the required WebSocket upgrade headers for `/ws/` is in
[`deploy/nginx.example.conf`](deploy/nginx.example.conf) — copy it, set your `server_name` and cert
paths, and point `proxy_pass` at the app's bind address. **Rate-limit `/login`** at the proxy.

## Updating

Self-update moves to the **channel's latest** release, flips `current`, restarts, health-checks
`/healthz`, and **rolls back** automatically if the new release fails.

- **In-app:** version + check/apply control on the dashboard, plus **automatic updates**
  (daily) and the release channel under Settings → System → Updates — both persist
  server-side and apply live.
- **CLI:** `agent-sessions autoupdate` (apply only if newer).
- **Re-run the installer:** also upgrades; it keeps your persisted auto-update/channel
  settings. An old `agent-sessions-update.timer` from a pre-Settings install is migrated
  automatically (opt-in preserved, legacy units removed).

### Channels — `stable` is the production one

| Channel | Tracks | Use for |
|---|---|---|
| `stable` (**default**) | the highest `vX.Y.Z` tag on the remote | production |
| `main` | the development branch HEAD | development only |

`main` is **opt-in only** — nothing selects it but an explicit `AGENT_SESSIONS_CHANNEL=main`,
and the installer prints a non-production notice when it is in effect. The distinction is not
cosmetic: on `main` an auto-update takes whatever HEAD says at the moment it runs, so a commit
reaches your running service with no release cut between the two. On `stable` an update only
moves when a version is tagged.

### Release verification on `stable`

A git tag is a **movable pointer**. Anyone able to write to the repo could re-point an existing
release tag at different code, and every install tracking `stable` would take it — no diff, no
review, no version change. `scripts/release-manifest.json` is a committed record of what each
tag pointed at when it was cut: before an update runs, the target tag is re-resolved on the
remote and compared, and **a tag that has moved refuses the update**. The reason is surfaced on
the update status rather than failing silently.

The check and the build name the **same object**. Resolving a tag and cloning it are two
separate lookups of a mutable name, so verifying one and building the other would leave the
gap wide open — a tag moved in between (or a higher tag published in between) would pass the
check and install anyway. The updater therefore hands the installer both the verified tag and
its immutable commit, and the installer refuses to build a clone that resolved to anything
else. Nothing pins a hand-run install, which has no prior verification to bind to.

An **auto-update that cannot obtain both the tag and its commit does not run.** That is the one
place this feature fails closed rather than degrading open, and the asymmetry is deliberate: an
unknown *manifest entry* is structural (a build's manifest can never list a release cut after
it, so refusing would stop every install updating forever), whereas an empty *remote lookup* is
transient and can be induced by whoever can write tags — break the ref, let an unpinned update
spawn, repoint it before the clone. Refusing there costs only a postponed update, and the next
cycle retries; the reason is surfaced on the update status.

A tag the manifest does *not* know about **proceeds**, and that is deliberate. The manifest
ships inside the repo, so a running build's copy can never contain an entry for a release cut
afterwards — failing closed there would not be strict, it would mean no install ever
auto-updates again. Stated plainly:

- **Caught:** retroactive mutation of a release your build already knows about.
- **Not caught:** a brand-new tag published after your build was cut. Vouching for that needs
  signature verification over the tag object, which is not implemented yet.

Regenerate after cutting a release with `python3 scripts/gen-release-manifest`. A stale manifest
is safe — unknown tags pass, so it simply verifies less.

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
journalctl --user -u agent-sessions.service -f           # logs
```

To stop **automatic updates only**, turn the toggle off under Settings → System → Updates,
or set `AGENT_SESSIONS_AUTOUPDATE=0` in `~/.local/share/agent-sessions/env`.

## Home Free access key — rotate, show, disable

If you enabled streamed remote access (`AGENT_SESSIONS_REMOTE=stream`), the **access key is
the only gate on the box** — enabling stream mode sets the app to `AGENT_SESSIONS_AUTH_MODE=none`
behind a loopback bind, so anyone holding the console name + key has full control. Treat it
like a root password. Three maintenance commands manage its life; each acts on the existing
install, is safe to re-run, and **never** performs an install:

```sh
sh install.sh --homefree-show-credentials   # print the current console name + access key
sh install.sh --homefree-rotate-key         # issue a new key (console name unchanged)
sh install.sh --homefree-disable            # stop streaming; take the key out of live config
```

- **Rotate** when a key may have been seen by anyone else. The new key is generated,
  written `0600` and validated **before** the old one stops being live, so a rotation that
  fails leaves the working credential in place rather than an unreachable box. The
  superseded key is kept at `homefree/access_key.prev` (`0600`) so you can roll back;
  devices holding the old key must be re-entered with the new one.
- **Disable** stops and disables `agent-sessions-homefree.service` and moves the key to
  `homefree/access_key.disabled`. It touches nothing else — `agent-sessions.service`,
  your sessions, transcripts and engine data are all left alone. Re-enable by re-running
  the installer with `AGENT_SESSIONS_REMOTE=stream`, which issues a **fresh** key.
- **They run one at a time.** Rotate and disable take a lock (`homefree/.lifecycle.lock`) for
  the whole operation — state inspection, key files, service action and the final message.
  Individually careful steps still compose badly: a disable and a rotate running together can
  leave the box streaming under a fresh key moments after reporting streaming off, and two
  rotations can leave `access_key.prev` naming a key that was never live. A second command
  refuses immediately and changes nothing. Enabling streaming takes the same lock, since it
  writes the key and starts the unit too. A lock left by a crashed run is taken over only once
  its owner is provably gone — one that names *no* owner is refused rather than taken, because
  "still starting up" and "crashed" are indistinguishable from outside.

  The lock is held around the Home Free **setup step**, not the whole installer: holding it
  across the package and web builds would turn an unrelated slow step into a lockout of these
  commands, and nothing else the installer does touches Home Free key or service state.
- **Rotation will not proceed without a working way back.** The roll-back copy of the current
  key is written, verified and installed *before* the live key is replaced; if any of that
  fails the rotation aborts with the old key still live, rather than replacing it and
  reporting a roll-back that does not exist.
- **Both refuse rather than half-work when the agent cannot be reached.** Rotation and
  disable only take effect once the running agent restarts or stops — it reads the access key
  at startup and holds it in memory, so changing the file underneath a live agent revokes
  nothing. Where `systemctl --user` manages the unit, a failed `stop`/`disable`/`restart` fails
  the whole command. Where there is **no user systemd** (a container, a non-service install, an
  agent you started by hand), the command looks for a running agent first and refuses *before*
  touching the key if one is found, telling you how to stop it. If neither `pgrep` nor `ps` can
  answer, it also refuses — an unverifiable claim of revocation is not a revocation. On a box
  where you can confirm by hand that no agent is running, re-run with
  `AGENT_SESSIONS_HOMEFREE_AGENT_STOPPED=1`.
- **Disable does not restore password auth.** Flipping `AGENT_SESSIONS_AUTH_MODE=none` back
  would lock out an operator who has no password set, so the command leaves app auth as it
  found it and says so. The app stays loopback-bound (unreachable off-box), but any *local*
  account can reach it without a password. To restore password auth: remove that line from
  `~/.local/share/agent-sessions/env`, set a password, and restart the service.

## Release signing

<!-- signing-enforcement: off -->
> **Status: not active yet.** This release ships the trust root only. Nothing verifies anything
> today — `install.sh` carries `RELEASE_SIGNERS` as a constant and has no signature check, and no
> release has been signed yet. Signing arrives in #832 Phase 1 and the installer starts enforcing
> in Phase 2. Until then this section describes what is being built, so the mechanism can be
> reviewed before it has teeth.

Once active, release **tags** will be signed and the installer will verify the signature before
building anything.

The reason for a signature rather than a checksum list is that **one key covers every future
release**. A list must name each release, so a copy installed months ago can only vouch for
releases *older* than itself — exactly backwards for an updater. A key does not expire that way.

The trust root will be read from the copy you already trust — the `install.sh` you ran, or the
release currently installed — and never from the code being verified. A signer list taken from
the thing it vouches for proves nothing.

Enforcement starts at a recorded cutover (`scripts/release-trust.json`), so existing unsigned
releases keep working; only releases newer than that require a signature.

Inspect the trust root shipped with this release:

```sh
cat scripts/release-signers
```

and, once signed releases exist, check one by hand:

```sh
git -c gpg.ssh.allowedSignersFile=scripts/release-signers verify-tag <tag>
```

Exit `0` plus `Good "git" signature for release@agent-sessions` is the only success. `Good "git"
signature` *without* a named principal means the signature is valid but the key is untrusted — a
check grepping for `Good.*signature` would accept anyone's key.

**What signing will and will not cover.** It proves the release tag was created by a holder of the
release key. It says nothing about the dependencies, the toolchain, or the machine that built the
release. Verification requires `ssh-keygen` (`openssh-client`).

## Lost the password?

Reset from the host — never pass the password on the command line (it leaks via shell history /
`ps`):

```sh
~/.local/share/agent-sessions/current/venv/bin/agent-sessions reset-password --prompt   # interactive, no echo
… reset-password --stdin    # scriptable: read one line from stdin
… reset-password            # generate a random one and print it once
```
