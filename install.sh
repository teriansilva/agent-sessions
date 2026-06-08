#!/bin/sh
# agent-sessions installer — rootless, user-level. Idempotent: re-running upgrades in
# place using an atomic release directory + a `current` symlink (one-step rollback).
#
#   curl -fsSL <url>/install.sh | sh
#
# No sudo for the app itself: it installs under ~/.local/share/agent-sessions and runs
# as a `systemctl --user` service. Sudo is used ONLY to install python3-venv if missing
# (Debian/Ubuntu, Fedora), and that step is clearly prompted. Binds 127.0.0.1 by
# default — put a reverse proxy / TLS in front (the installer does not configure nginx).
#
# Overridable via env: AGENT_SESSIONS_REPO, AGENT_SESSIONS_REF, AGENT_SESSIONS_CHANNEL
# (stable|main), AGENT_SESSIONS_HOST, AGENT_SESSIONS_PORT, AGENT_SESSIONS_HOME,
# AGENT_SESSIONS_ORIGIN. AGENT_SESSIONS_NO_SERVICE=1 installs without touching systemd.
set -eu

APP=agent-sessions
REPO_URL="${AGENT_SESSIONS_REPO:-https://github.com/teriansilva/agent-sessions.git}"
REF="${AGENT_SESSIONS_REF:-}"
CHANNEL="${AGENT_SESSIONS_CHANNEL:-stable}"
HOST="${AGENT_SESSIONS_HOST:-127.0.0.1}"
PORT="${AGENT_SESSIONS_PORT:-8765}"
PREFIX="${AGENT_SESSIONS_HOME:-$HOME/.local/share/$APP}"
ORIGIN="${AGENT_SESSIONS_ORIGIN:-http://$HOST:$PORT}"
KEEP_RELEASES=3
# Pinned Node used to build the React UI when the host has no new-enough Node. Vendored
# into $PREFIX/.toolchain (no sudo, self-contained) so the install "just works".
NODE_VERSION="${AGENT_SESSIONS_NODE_VERSION:-22.14.0}"
NODE_MIN_MAJOR=20
NPM=npm  # resolved by ensure_node() to the system npm or the vendored one
NODE_BIN=node  # resolved by ensure_node() to the system OR vendored node — persisted to the env so the
               # VT sidecar can run at runtime even when Node was only vendored to build (Hermes #273)
# Python toolchain. The app needs CPython >= 3.11. ensure_python() resolves $PY to a system
# python, else vendors a pinned, relocatable standalone CPython (python-build-standalone) into
# $PREFIX/.toolchain — the Python analogue of the vendored Node above (no sudo, no system change).
PY=python3  # resolved by ensure_python() to the system OR vendored interpreter
PY_VERSION="${AGENT_SESSIONS_PYTHON_VERSION:-3.12.13}"
PBS_TAG="${AGENT_SESSIONS_PBS_TAG:-20260602}"  # python-build-standalone release tag for PY_VERSION

RELEASES="$PREFIX/releases"
CURRENT="$PREFIX/current"
ENVF="$PREFIX/env"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$APP.service"

log()  { printf '  %s\n' "$*"; }
note() { printf '\n%s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
_sha256() {  # print the hex SHA-256 of file $1 using whatever tool exists (empty if none)
  if   have sha256sum; then sha256sum "$1" | awk '{print $1}'
  elif have shasum;    then shasum -a 256 "$1" | awk '{print $1}'
  else echo ""
  fi
}

# --- prerequisites: auto-install everything we can, vendor what we can't ----------
# Goal: a self-contained install that "just works". A package the operator can't get
# any other way (a too-old / missing Node) is vendored into $PREFIX with no sudo.

_pkg_install() {  # best-effort distro install of the named packages; returns nonzero if it can't
  if   have apt-get; then sudo apt-get update -qq && sudo apt-get install -y "$@"
  elif have dnf;     then sudo dnf install -y "$@"
  elif have pacman;  then sudo pacman -Sy --noconfirm "$@"
  else return 1
  fi
}

ensure_node() {
  # Resolve $NPM to a Node >= $NODE_MIN_MAJOR. Order: a new-enough system Node > a distro
  # install > a vendored static Node (downloaded into $PREFIX/.toolchain, no sudo).
  _node_ok() { have node && [ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" -ge "$NODE_MIN_MAJOR" ]; }
  if _node_ok && have npm; then NPM=npm; NODE_BIN="$(command -v node)"; return; fi
  log "Node >= $NODE_MIN_MAJOR not found — trying to install it…"
  _pkg_install nodejs npm >/dev/null 2>&1 || true
  if _node_ok && have npm; then NPM=npm; NODE_BIN="$(command -v node)"; return; fi
  # Vendor a pinned static Node — fully self-contained, no sudo, no system change.
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) na=x64 ;;
    aarch64|arm64) na=arm64 ;;
    *) die "no prebuilt Node for arch '$arch' — install Node >= $NODE_MIN_MAJOR and re-run" ;;
  esac
  tdir="$PREFIX/.toolchain"
  ndir="$tdir/node-v$NODE_VERSION-linux-$na"
  if [ ! -x "$ndir/bin/npm" ]; then
    mkdir -p "$tdir"
    log "fetching a self-contained Node $NODE_VERSION ($na) for the UI build…"
    curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-$na.tar.gz" \
      -o "$tdir/node.tar.gz" || die "could not download Node $NODE_VERSION"
    tar -xzf "$tdir/node.tar.gz" -C "$tdir" || die "could not unpack Node"
    rm -f "$tdir/node.tar.gz"
  fi
  PATH="$ndir/bin:$PATH"; export PATH   # so the vendored node + vite are found by npm
  NPM="$ndir/bin/npm"
  NODE_BIN="$ndir/bin/node"
}

_confirm() {  # y/n on the controlling tty. Default Yes. Auto-yes via AGENT_SESSIONS_ASSUME_YES=1;
              # no tty to ask on (a non-interactive pipe) → proceed, like the vendored-Node path.
  [ "${AGENT_SESSIONS_ASSUME_YES:-0}" = 1 ] && return 0
  if [ -r /dev/tty ]; then
    printf '%s ' "$1" > /dev/tty
    read _ans < /dev/tty 2>/dev/null || _ans=""
    case "$_ans" in [Nn]*) return 1 ;; *) return 0 ;; esac
  fi
  return 0
}

_ensure_venv_module() {  # Debian/Ubuntu split venv into python3-venv; a vendored standalone python
                         # already ships it, so this is a no-op for the vendored interpreter.
  "$PY" -m venv --help >/dev/null 2>&1 && return
  log "python venv module missing — installing…"
  _pkg_install python3-venv >/dev/null 2>&1 || _pkg_install python3 >/dev/null 2>&1 || true
  "$PY" -m venv --help >/dev/null 2>&1 \
    || die "install the python3 venv module for your distro (e.g. python3-venv) and re-run"
}

ensure_python() {
  # Resolve $PY to a CPython >= 3.11. Order: an explicit override > a new-enough system python
  # (newest name first, so a python3.12 beside an old default python3 wins) > a distro install >
  # a vendored standalone CPython downloaded into $PREFIX/.toolchain (no sudo, no system change) —
  # the Python analogue of ensure_node's vendored Node. The vendor step ASKS first on a terminal
  # (the operator's machine, a ~30 MB download); AGENT_SESSIONS_ASSUME_YES=1 / no tty → proceed.
  _py_ok() { [ -n "$1" ] && "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' >/dev/null 2>&1; }

  if [ -n "${AGENT_SESSIONS_PYTHON:-}" ]; then
    _py_ok "$AGENT_SESSIONS_PYTHON" \
      || die "AGENT_SESSIONS_PYTHON=$AGENT_SESSIONS_PYTHON is not a python >= 3.11"
    PY="$AGENT_SESSIONS_PYTHON"; _ensure_venv_module; return
  fi
  for cand in python3.13 python3.12 python3.11 python3 python; do
    if have "$cand" && _py_ok "$(command -v "$cand")"; then
      PY="$(command -v "$cand")"; _ensure_venv_module; return
    fi
  done
  # No system Python >= 3.11. We deliberately DON'T try a distro `python3` install here: on the
  # stale distros that land here (e.g. Ubuntu whose python3 is 3.10) it can't supply >= 3.11 and
  # would only burn a pointless sudo prompt right before the download. Go straight to vendoring a
  # pinned standalone CPython (relocatable, no root) — ask first.
  _confirm "No system Python >= 3.11 found. Download a private one (~30 MB, no root) into $PREFIX/.toolchain? [Y/n]" \
    || die "Python >= 3.11 required. Install it (e.g. your distro's python3.12 + python3.12-venv), set AGENT_SESSIONS_PYTHON=/path/to/python3.12, or re-run and accept the download."
  os="$(uname -s)"; arch="$(uname -m)"
  case "$os" in
    Linux)  plat=unknown-linux-gnu ;;
    Darwin) plat=apple-darwin ;;
    *) die "no prebuilt Python for OS '$os' — install python3.11+ and re-run" ;;
  esac
  case "$arch" in
    x86_64|amd64) pa=x86_64 ;;
    aarch64|arm64) pa=aarch64 ;;
    *) die "no prebuilt Python for arch '$arch' — install python3.11+ and re-run" ;;
  esac
  asset="cpython-${PY_VERSION}+${PBS_TAG}-${pa}-${plat}-install_only.tar.gz"
  url="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${asset}"
  # Supply-chain pin: the expected SHA-256 per supported asset, from the release's SHA256SUMS.
  # A `curl | sh` install (esp. the no-tty auto-proceed) must NOT trust a mutable release URL on
  # TLS alone — we verify the tarball against this digest before unpacking and refuse on mismatch.
  # These pins are tied to PY_VERSION+PBS_TAG above; bump all four together when those change.
  case "${pa}-${plat}" in
    x86_64-unknown-linux-gnu)  want_sha=9be5c21b78dbc371e739bc7faf3b007b8e607335f780bdd2e0dd44a6e3580d76 ;;
    aarch64-unknown-linux-gnu) want_sha=f0c9ea0022b2dfdf0a4733e962ba8cc883c45d26df26116b9802b658240a25d7 ;;
    x86_64-apple-darwin)       want_sha=e6776f05a160f9d44f9c2bc8bd1e252037856808528bf910dea791bdf70a7224 ;;
    aarch64-apple-darwin)      want_sha=0c21806e8690e4b20a6c2e9dc662f46196c5ba719686e8dd60f00af6ff409a75 ;;
    *) die "no pinned checksum for ${pa}-${plat} at Python ${PY_VERSION} — install python3.11+ and re-run" ;;
  esac
  tdir="$PREFIX/.toolchain"; pdir="$tdir/cpython-${PY_VERSION}"
  if [ ! -x "$pdir/bin/python3" ]; then
    mkdir -p "$tdir"
    log "fetching a self-contained Python ${PY_VERSION} (${pa}/${plat})…"
    curl -fsSL "$url" -o "$tdir/python.tar.gz" || die "could not download standalone Python ${PY_VERSION}"
    got_sha="$(_sha256 "$tdir/python.tar.gz")"
    [ -n "$got_sha" ] || die "no sha256 tool (sha256sum/shasum) to verify the Python download — install one and re-run"
    [ "$got_sha" = "$want_sha" ] \
      || { rm -f "$tdir/python.tar.gz"; die "Python download checksum mismatch (expected $want_sha, got $got_sha) — refusing to use it"; }
    rm -rf "$tdir/python"
    tar -xzf "$tdir/python.tar.gz" -C "$tdir" || die "could not unpack standalone Python"
    rm -rf "$pdir"; mv "$tdir/python" "$pdir"   # the install_only tarball extracts to ./python
    rm -f "$tdir/python.tar.gz"
  fi
  PY="$pdir/bin/python3"
  _py_ok "$PY" || die "the vendored Python looks broken — set AGENT_SESSIONS_PYTHON to a python >= 3.11"
}

ensure_prereqs() {
  have curl || die "curl not found — install curl and re-run"
  have git || { log "git missing — installing…"; _pkg_install git || die "install git and re-run"; }
  ensure_python
  # The ws terminal attaches agents under a persistent dtach master.
  have dtach || { log "dtach missing (terminal pane) — installing…"; _pkg_install dtach >/dev/null 2>&1 \
    || log "could not auto-install dtach — install it so the terminal pane works"; }
  # The React UI is built from source (Vite) at install time; skip resolving Node when
  # the build is explicitly skipped (CI / bring-your-own-dist).
  [ "${AGENT_SESSIONS_SKIP_WEB_BUILD:-0}" = 1 ] || ensure_node
  preflight_report
}

preflight_report() {
  log "prerequisites:"
  log "  git      $(command -v git || echo MISSING)"
  log "  python   ${PY:-MISSING} ($("${PY:-python3}" -V 2>&1 | awk '{print $2}'))"
  log "  node     $(command -v node || echo '(vendored)') ($(node -v 2>/dev/null || echo "v$NODE_VERSION vendored"))"
  log "  dtach    $(command -v dtach || echo 'MISSING — terminal pane degraded')"
}

resolve_ref() {
  if [ -n "$REF" ]; then printf '%s' "$REF"; return; fi
  if [ "$CHANNEL" = main ]; then printf 'main'; return; fi
  # stable = the highest semver-ish vX.Y.Z tag on the remote (empty → default branch).
  git ls-remote --tags --refs "$REPO_URL" 'v*' 2>/dev/null | sed 's#.*/##' | sort -V | tail -1
}

build_release() {
  # Clone at the ref, then build the venv + pip-install AT the final release path and
  # set $rel (global). The venv is built in place — venv console-script shebangs are
  # absolute, so a venv must never be moved after creation. Only the plain source tree
  # is relocated. `current` is flipped to $rel by the caller after a full build.
  ref="$1"
  tmp="$(mktemp -d "$PREFIX/.clone.XXXXXX")"
  if [ -n "$ref" ]; then
    git clone -q --depth 1 --branch "$ref" "$REPO_URL" "$tmp/src" 2>/dev/null \
      || { git clone -q "$REPO_URL" "$tmp/src"; git -C "$tmp/src" checkout -q "$ref"; }
  else
    git clone -q --depth 1 "$REPO_URL" "$tmp/src"
  fi
  sha="$(git -C "$tmp/src" rev-parse --short HEAD)"
  mkdir -p "$RELEASES"
  rel="$RELEASES/$(date +%Y%m%d-%H%M%S)-$sha"
  rm -rf "$rel"
  mkdir -p "$rel"
  mv "$tmp/src" "$rel/src"   # source is plain files — safe to relocate
  rm -rf "$tmp"
  "$PY" -m venv "$rel/venv"   # built at its final path (with the resolved python) → valid shebangs
  "$rel/venv/bin/pip" install --quiet --upgrade pip
  "$rel/venv/bin/pip" install --quiet "$rel/src"
  build_web "$rel"
  build_sidecar "$rel"
}

build_web() {
  # Build the React SPA (Vite) into <rel>/src/web/dist. The app serves it when the env
  # points AGENT_SESSIONS_WEB_DIST here (set by write_env_if_absent, stable via `current`).
  # web/dist is git-ignored, so every release builds its own — no stale artifact.
  rel="$1"
  if [ "${AGENT_SESSIONS_SKIP_WEB_BUILD:-0}" = 1 ]; then
    log "skipping UI build (AGENT_SESSIONS_SKIP_WEB_BUILD=1)"; return 0
  fi
  [ -f "$rel/src/web/package.json" ] || { log "no web/ in this release — skipping UI build"; return 0; }
  log "building the React UI (this can take a minute)…"
  ( cd "$rel/src/web" && "$NPM" ci --no-audit --no-fund --silent && "$NPM" run build --silent ) \
    || die "UI build failed — see the npm output above"
  [ -f "$rel/src/web/dist/index.html" ] || die "UI build produced no dist/index.html"
}

build_sidecar() {
  # Build the VT scrollback sidecar (#273) into <rel>/src/vt-sidecar/dist/server.mjs — a small Node
  # esbuild bundle the app spawns when AGENT_SESSIONS_VT_SCROLLBACK=1. Built in EVERY release so the
  # faithful-console scroll-up is ready the moment the flag is flipped; dist is git-ignored (no stale
  # artifact). Same vendored-Node toolchain as the UI build. Graceful: skip when absent, so an older
  # release (no vt-sidecar/) still installs. NOTE: enabling the flag also needs `node` on the runtime
  # PATH — without it the app fails safe to the transcript scroll-up (the build here is harmless).
  rel="$1"
  if [ "${AGENT_SESSIONS_SKIP_WEB_BUILD:-0}" = 1 ]; then
    log "skipping sidecar build (AGENT_SESSIONS_SKIP_WEB_BUILD=1)"; return 0
  fi
  [ -f "$rel/src/vt-sidecar/package.json" ] || { log "no vt-sidecar/ in this release — skipping"; return 0; }
  log "building the VT scrollback sidecar…"
  ( cd "$rel/src/vt-sidecar" && "$NPM" ci --no-audit --no-fund --silent && "$NPM" run build --silent ) \
    || die "vt-sidecar build failed — see the npm output above"
  [ -f "$rel/src/vt-sidecar/dist/server.mjs" ] || die "vt-sidecar build produced no dist/server.mjs"
}

write_env_if_absent() {
  # First install only: generate a signing secret + admin credentials. The plaintext
  # password is returned on stdout (printed to the console ONCE by the caller); only the
  # hash is persisted. On upgrade the existing env is left untouched.
  rel="$1"
  [ -f "$ENVF" ] && { printf ''; return; }
  py="$rel/venv/bin/python"
  secret="$("$py" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  password="$("$py" -c 'import secrets; print(secrets.token_urlsafe(18))')"
  hash="$("$py" -c 'import sys; from agent_sessions.auth import hash_password; print(hash_password(sys.argv[1]))' "$password")"
  umask 077
  cat > "$ENVF" <<EOF
AGENT_SESSIONS_USERNAME=admin
AGENT_SESSIONS_PASSWORD_HASH=$hash
AGENT_SESSIONS_SECRET_KEY=$secret
AGENT_SESSIONS_ORIGIN=$ORIGIN
AGENT_SESSIONS_HOST=$HOST
AGENT_SESSIONS_PORT=$PORT
AGENT_SESSIONS_ENV_FILE=$ENVF
AGENT_SESSIONS_FORCE_PASSWORD_CHANGE=1
EOF
  chmod 600 "$ENVF"
  printf '%s' "$password"
}

_env_has() { grep -q "^$1=" "$ENVF" 2>/dev/null; }
_env_set_if_absent() {
  # Append KEY=VAL only if KEY is absent — preserves operator overrides + existing
  # secrets/credentials (we never rewrite the lines already in the file). 0600 is kept
  # because we only append to an already-0600 file.
  _env_has "$1" || printf '%s=%s\n' "$1" "$2" >> "$ENVF"
}

migrate_env() {
  # Bring an env file (fresh OR pre-existing from an older install) up to the current
  # serving contract: the shipped product is the React UI + ws-PTY terminal, the only
  # UI/terminal there is (the app no longer reads a UI/terminal selector env var).
  # Idempotent and non-destructive — existing keys win.
  [ -f "$ENVF" ] || return 0
  mkdir -p "$PREFIX/pty"   # ws-PTY dtach sockets live here
  umask 077
  _env_set_if_absent AGENT_SESSIONS_WEB_DIST "$CURRENT/src/web/dist"
  _env_set_if_absent AGENT_SESSIONS_RUNTIME_DIR "$PREFIX/pty"
  # Point the VT sidecar (#273) at the built bundle via the stable `current` symlink, so flipping
  # AGENT_SESSIONS_VT_SCROLLBACK=1 later is a one-liner (the prod pip install is non-editable, so the
  # package can't resolve vt-sidecar relative to itself). The flag itself is intentionally NOT set
  # here — VT ships OFF (byte-identical to the transcript scroll-up) until explicitly enabled.
  _env_set_if_absent AGENT_SESSIONS_VT_SIDECAR_JS "$CURRENT/src/vt-sidecar/dist/server.mjs"
  # Persist the Node binary the installer resolved (system OR the vendored static Node under
  # $PREFIX/.toolchain). The runtime unit's PATH may not include the vendored toolchain, and the app
  # spawns the sidecar by this path — so the flag flip "just works" even on a host with no system
  # Node (Hermes #273). "node" when the build was skipped (CI) → app falls back to `node` on PATH.
  _env_set_if_absent AGENT_SESSIONS_VT_SIDECAR_NODE "$NODE_BIN"
}

render_unit() {
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT" <<EOF
[Unit]
Description=agent-sessions
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# #165: only SIGTERM the broker's main PID on stop; leave dtach + the agent processes
# alone so a service restart (every deploy) does NOT kill the user's live session. The
# new broker rediscovers the still-alive masters via the existing sock files.
KillMode=process
# Put ~/.local/bin first so sessions spawned by the app (claude/opencode/codex/gemini,
# which commonly live there) are on PATH — otherwise the claude CLI nags
# "Native installation exists but ~/.local/bin is not in your PATH". Before EnvironmentFile
# so an explicit PATH in the env file still wins. %h = the service user's home dir.
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=$ENVF
ExecStart=$CURRENT/venv/bin/agent-sessions serve --host $HOST --port $PORT
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF
}

prune_releases() {
  # Keep the newest $KEEP_RELEASES (plus whatever `current` points at) for rollback.
  [ -d "$RELEASES" ] || return 0
  cur="$(readlink "$CURRENT" 2>/dev/null || true)"
  # shellcheck disable=SC2012
  ls -1dt "$RELEASES"/*/ 2>/dev/null | tail -n +"$((KEEP_RELEASES + 1))" | while read -r d; do
    [ "${d%/}" = "$cur" ] && continue
    rm -rf "$d"
  done
}

_healthcheck() {
  i=0
  while [ "$i" -lt 10 ]; do
    if curl -fsS -m 2 "http://$HOST:$PORT/healthz" >/dev/null 2>&1; then return 0; fi
    i=$((i + 1))
    sleep 1
  done
  return 1
}

manage_service() {
  prev="$1"  # the release `current` pointed at before this flip (rollback target)
  if [ "${AGENT_SESSIONS_NO_SERVICE:-0}" = 1 ] || ! systemctl --user >/dev/null 2>&1; then
    note "Service not started automatically (no systemctl --user session)."
    log "Start it with:  $CURRENT/venv/bin/agent-sessions serve"
    return 0
  fi
  render_unit
  systemctl --user daemon-reload
  systemctl --user enable "$APP.service" >/dev/null 2>&1 || true
  systemctl --user restart "$APP.service"
  _healthcheck && return 0
  # Unhealthy. Roll back to the previous release if there is one (self-update safety):
  # re-point `current` (atomic) + restart so a bad update can't leave the host down.
  if [ -n "$prev" ] && [ "$prev" != "$rel" ] && [ -d "$prev" ]; then
    log "new release failed /healthz — rolling back to $(basename "$prev")"
    rb="$PREFIX/.current.rb.$$"
    ln -s "$prev" "$rb"
    mv -Tf "$rb" "$CURRENT" 2>/dev/null || { rm -f "$rb"; ln -sfn "$prev" "$CURRENT"; }
    systemctl --user restart "$APP.service"
    _healthcheck && die "update failed health check — rolled back to the previous release"
    die "update failed and the rollback release is also unhealthy"
  fi
  die "service started but /healthz never came up on $HOST:$PORT"
}

manage_autoupdate() {
  # Opt-in (AGENT_SESSIONS_AUTOUPDATE=1): a user timer that periodically runs
  # `agent-sessions autoupdate` (check the channel + apply via the same rollback-guarded
  # installer). Disabled (and torn down on re-run) by default.
  systemctl --user >/dev/null 2>&1 || return 0
  case "${AGENT_SESSIONS_AUTOUPDATE:-}" in
    1 | true | yes)
      mkdir -p "$UNIT_DIR"
      cat > "$UNIT_DIR/$APP-update.service" <<EOF
[Unit]
Description=agent-sessions autoupdate
[Service]
Type=oneshot
EnvironmentFile=$ENVF
# Carry the opt-in + channel + repo explicitly: the timer runs detached from the install
# shell, and the re-run installer must see AGENT_SESSIONS_AUTOUPDATE (so it keeps the
# timer) and the channel/repo (so the update targets the right ref) — none of which live
# in the env file. (Non-secret values only.)
Environment=AGENT_SESSIONS_AUTOUPDATE=1
Environment=AGENT_SESSIONS_CHANNEL=$CHANNEL
Environment=AGENT_SESSIONS_REPO=$REPO_URL
ExecStart=$CURRENT/venv/bin/agent-sessions autoupdate
EOF
      cat > "$UNIT_DIR/$APP-update.timer" <<EOF
[Unit]
Description=agent-sessions autoupdate timer
[Timer]
OnCalendar=${AGENT_SESSIONS_AUTOUPDATE_ONCALENDAR:-daily}
Persistent=true
[Install]
WantedBy=timers.target
EOF
      systemctl --user daemon-reload
      systemctl --user enable --now "$APP-update.timer" >/dev/null 2>&1 || true
      log "autoupdate enabled ($CHANNEL channel)"
      ;;
    *)
      systemctl --user disable --now "$APP-update.timer" >/dev/null 2>&1 || true
      ;;
  esac
}

main() {
  mkdir -p "$PREFIX"
  ensure_prereqs
  ref="$(resolve_ref)"
  log "installing $APP (${ref:-default branch}) into $PREFIX …"
  rel=""
  # Remove a half-built release on any failure before `current` is flipped — the prior
  # release keeps serving (rollback-safe). Cleared once the flip succeeds.
  trap 'rm -rf "$rel"' EXIT INT TERM
  build_release "$ref"  # sets $rel
  # Write/refresh the unit before flipping so the ExecStart path is valid — but ONLY when
  # we'll actually manage the service. Under NO_SERVICE we must not touch the host's
  # systemd unit at all (it's a per-user, not per-HOME, path — otherwise a scratch/test
  # install would clobber the real unit).
  [ "${AGENT_SESSIONS_NO_SERVICE:-0}" = 1 ] || render_unit
  password="$(write_env_if_absent "$rel")"
  # Bring the env up to the current serving contract (React UI + ws terminal). Runs for
  # BOTH a fresh install and an upgrade of an older env — idempotent + non-destructive,
  # so re-running the installer actually cuts an existing deployment over to the React UI.
  migrate_env
  # Atomic flip: create the new link beside `current`, then rename(2) it over the old
  # one — atomic on the same filesystem, so a concurrent start/health-check/restart never
  # sees a missing `current` (unlike `ln -sfn`, which unlinks then recreates). Falls back
  # to a plain swap where `mv -T` is unavailable. One-step rollback = re-point to a prior
  # release dir.
  prev_target="$(readlink "$CURRENT" 2>/dev/null || true)"  # for rollback on a bad update
  tmp_link="$PREFIX/.current.$$"
  ln -s "$rel" "$tmp_link"
  mv -Tf "$tmp_link" "$CURRENT" 2>/dev/null || { rm -f "$tmp_link"; ln -sfn "$rel" "$CURRENT"; }
  trap - EXIT INT TERM        # release is live; do not clean it up
  prune_releases
  # Discover installed agent CLIs and record their paths in the env (best-effort; also
  # re-runs on every upgrade so newly-installed engines are picked up).
  "$CURRENT/venv/bin/agent-sessions" doctor --env "$ENVF" >/dev/null 2>&1 || true
  manage_service "$prev_target"
  manage_autoupdate
  version="$("$CURRENT/venv/bin/agent-sessions" version 2>/dev/null || echo '?')"

  note "agent-sessions $version installed."
  log "URL:     $ORIGIN"
  if [ -n "$password" ]; then
    log "username: admin"
    log "password: $password"
    note "Save the password now — it is shown ONCE and only the hash is stored."
  else
    log "(existing credentials kept; upgrade in place)"
  fi
}

main "$@"
