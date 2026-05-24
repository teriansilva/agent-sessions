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

RELEASES="$PREFIX/releases"
CURRENT="$PREFIX/current"
ENVF="$PREFIX/env"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$APP.service"

log()  { printf '  %s\n' "$*"; }
note() { printf '\n%s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

ensure_prereqs() {
  have git || die "git not found — install git and re-run"
  have python3 || die "python3 not found — install python3.11+ and re-run"
  python3 -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' \
    || die "python3 >= 3.11 required"
  if ! python3 -m venv --help >/dev/null 2>&1; then
    log "python venv module missing — attempting to install it (may prompt for sudo)…"
    if   have apt-get; then sudo apt-get update -qq && sudo apt-get install -y python3-venv
    elif have dnf;     then sudo dnf install -y python3
    else die "install the python3 venv module for your distro and re-run"
    fi
  fi
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
EOF
  chmod 600 "$ENVF"
  printf '%s' "$password"
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

manage_service() {
  if [ "${AGENT_SESSIONS_NO_SERVICE:-0}" = 1 ] || ! systemctl --user >/dev/null 2>&1; then
    note "Service not started automatically (no systemctl --user session)."
    log "Start it with:  $CURRENT/venv/bin/agent-sessions serve"
    return 0
  fi
  render_unit
  systemctl --user daemon-reload
  systemctl --user enable "$APP.service" >/dev/null 2>&1 || true
  systemctl --user restart "$APP.service"
  ok=0
  i=0
  while [ "$i" -lt 10 ]; do
    if curl -fsS -m 2 "http://$HOST:$PORT/healthz" >/dev/null 2>&1; then ok=1; break; fi
    i=$((i + 1)); sleep 1
  done
  [ "$ok" = 1 ] || die "service started but /healthz never came up on $HOST:$PORT"
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
  render_unit  # write/refresh the unit before flipping so ExecStart path is valid
  password="$(write_env_if_absent "$rel")"
  # Atomic flip: create the new link beside `current`, then rename(2) it over the old
  # one — atomic on the same filesystem, so a concurrent start/health-check/restart never
  # sees a missing `current` (unlike `ln -sfn`, which unlinks then recreates). Falls back
  # to a plain swap where `mv -T` is unavailable. One-step rollback = re-point to a prior
  # release dir.
  tmp_link="$PREFIX/.current.$$"
  ln -s "$rel" "$tmp_link"
  mv -Tf "$tmp_link" "$CURRENT" 2>/dev/null || { rm -f "$tmp_link"; ln -sfn "$rel" "$CURRENT"; }
  trap - EXIT INT TERM        # release is live; do not clean it up
  prune_releases
  # Discover installed agent CLIs and record their paths in the env (best-effort; also
  # re-runs on every upgrade so newly-installed engines are picked up).
  "$CURRENT/venv/bin/agent-sessions" doctor --env "$ENVF" >/dev/null 2>&1 || true
  manage_service
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
