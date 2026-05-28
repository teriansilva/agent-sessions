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

RELEASES="$PREFIX/releases"
CURRENT="$PREFIX/current"
ENVF="$PREFIX/env"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$APP.service"

log()  { printf '  %s\n' "$*"; }
note() { printf '\n%s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

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
  if _node_ok && have npm; then NPM=npm; return; fi
  log "Node >= $NODE_MIN_MAJOR not found — trying to install it…"
  _pkg_install nodejs npm >/dev/null 2>&1 || true
  if _node_ok && have npm; then NPM=npm; return; fi
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
}

ensure_prereqs() {
  have curl || die "curl not found — install curl and re-run"
  have git || { log "git missing — installing…"; _pkg_install git || die "install git and re-run"; }
  have python3 || die "python3 not found — install python3.11+ and re-run"
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' \
    || die "python3 >= 3.11 required"
  if ! python3 -m venv --help >/dev/null 2>&1; then
    log "python venv module missing — installing…"
    _pkg_install python3-venv >/dev/null 2>&1 || _pkg_install python3 >/dev/null 2>&1 \
      || die "install the python3 venv module for your distro and re-run"
  fi
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
  log "  python3  $(command -v python3 || echo MISSING) ($(python3 -V 2>&1 | awk '{print $2}'))"
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
  python3 -m venv "$rel/venv"   # built at its final path → valid shebangs
  "$rel/venv/bin/pip" install --quiet --upgrade pip
  "$rel/venv/bin/pip" install --quiet "$rel/src"
  build_web "$rel"
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
