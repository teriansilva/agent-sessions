#!/bin/sh
# agent-sessions / BattleLab uninstaller — rootless, user-level. Removes everything the
# installer created and leaves your AGENTS' own data (Claude/Codex/opencode/Gemini session
# history) completely untouched.
#
#   curl -fsSL <url>/uninstall.sh | sh
#
# What it removes:
#   * the systemd --user units (agent-sessions.service + the autoupdate service/timer),
#   * any live `dtach` session masters this install spawned,
#   * the install root  ~/.local/share/agent-sessions  (releases, venv, env, pty, 2fa.json,
#     vendored toolchain) — override via AGENT_SESSIONS_HOME,
#   * per-user prefs  ~/.config/agent-sessions,
#   * the cache  ~/.agent-sessions  (scrollback + uploads),
#   * the deploy log.
#
# What it NEVER touches: ~/.claude, ~/.codex, ~/.config/opencode (+ ~/.local/share/opencode),
# ~/.gemini — your agents and their conversation history. Uninstalling BattleLab does not
# delete a single transcript.
#
# It asks for confirmation on a terminal first. Skip the prompt with AGENT_SESSIONS_ASSUME_YES=1.

APP=agent-sessions
PREFIX="${AGENT_SESSIONS_HOME:-$HOME/.local/share/$APP}"
ENVF="$PREFIX/env"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
PREFS_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/$APP"
CACHE_DIR="$HOME/.agent-sessions"
DEPLOY_LOG="$HOME/.local/share/$APP-deploy.log"

log()  { printf '  %s\n' "$*"; }
note() { printf '\n%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }

# Honor an env file that overrides where runtime/cache/prefs live, so a customized install is
# fully cleaned. Read specific keys only (never `source` the secret-bearing file).
_env_val() { [ -f "$ENVF" ] && sed -n "s/^$1=//p" "$ENVF" | tail -1; }
RUNTIME_DIR="$(_env_val AGENT_SESSIONS_RUNTIME_DIR)"; RUNTIME_DIR="${RUNTIME_DIR:-$PREFIX/pty}"
SCROLLBACK_DIR="$(_env_val AGENT_SESSIONS_SCROLLBACK_DIR)"
PREFS_FILE="$(_env_val AGENT_SESSIONS_PREFS)"

_confirm() {  # y/N on the controlling tty (default NO — this is destructive). Env override to skip.
  [ "${AGENT_SESSIONS_ASSUME_YES:-0}" = 1 ] && return 0
  if [ -r /dev/tty ]; then
    printf '%s ' "$1" > /dev/tty
    read ans < /dev/tty 2>/dev/null || ans=""
    case "$ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
  fi
  # No tty to ask on → refuse rather than delete silently (the inverse of install's default).
  printf 'error: no terminal to confirm on; re-run with AGENT_SESSIONS_ASSUME_YES=1\n' >&2
  return 1
}

stop_services() {
  systemctl --user >/dev/null 2>&1 || { log "no systemctl --user session — skipping units"; return 0; }
  for u in "$APP.service" "$APP-update.timer" "$APP-update.service"; do
    systemctl --user disable --now "$u" >/dev/null 2>&1 || true
  done
  for f in "$APP.service" "$APP-update.service" "$APP-update.timer"; do
    rm -f "$UNIT_DIR/$f"
  done
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  log "stopped + removed systemd units"
}

kill_sessions() {
  # Terminate the install's live `dtach` masters (+ their attach clients) by matching the
  # runtime/socket dir in their argv. The agent processes exit with their master; their
  # on-disk transcripts are in the engines' own stores and are NOT removed.
  [ -d "/proc" ] && [ -n "$RUNTIME_DIR" ] || return 0
  killed=0
  for pid in $(ls /proc 2>/dev/null); do
    case "$pid" in *[!0-9]*) continue ;; esac
    if tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null | grep -q "dtach .*$RUNTIME_DIR"; then
      kill "$pid" 2>/dev/null && killed=$((killed + 1))
    fi
  done
  [ "$killed" -gt 0 ] && log "ended $killed live session process(es)"
  return 0
}

main() {
  note "This will UNINSTALL agent-sessions / BattleLab:"
  log  "service + autoupdate units"
  log  "install root   $PREFIX"
  log  "prefs          $PREFS_DIR"
  log  "cache          $CACHE_DIR (scrollback + uploads)"
  note "Your agents' own session history (~/.claude, ~/.codex, ~/.gemini, opencode) is kept."
  _confirm "Proceed? [y/N]" || { note "Aborted — nothing removed."; exit 0; }

  stop_services
  kill_sessions

  # Remove the install + its data. Custom override paths (read from the env file above) are
  # removed too when they live outside the defaults.
  for d in "$PREFIX" "$PREFS_DIR" "$CACHE_DIR" "$RUNTIME_DIR" "$SCROLLBACK_DIR"; do
    [ -n "$d" ] && [ -e "$d" ] && rm -rf "$d" && log "removed $d"
  done
  [ -n "$PREFS_FILE" ] && [ -f "$PREFS_FILE" ] && rm -f "$PREFS_FILE" && log "removed $PREFS_FILE"
  [ -f "$DEPLOY_LOG" ] && rm -f "$DEPLOY_LOG"

  note "BattleLab uninstalled. Your agents and their conversation history were left intact."
}

main "$@"
