#!/usr/bin/env bash
# The stand-in agent for the app-mode E2E stack (#1147).
#
# `appmode.ts` points `AGENT_SESSIONS_CLAUDE_BIN` at this file, so the app resumes the seeded
# session with a harmless fixture instead of the operator's REAL claude from `~/.local/bin`
# (a leak in this harness once left one spinning per run). Adapted from
# `web/visual/fake-agent.sh`: it paints a deterministic screen and holds the PTY, which is
# enough for every app-mode spec — none of them assert real Claude Code behaviour.
#
# The marker is how a spec proves the fixture really launched: an unavailable or refused
# engine must not leave the app-mode specs green with no agent at all.
set -euo pipefail

# The app resolves the entrypoint by path, so a version probe of the fixture must succeed.
if [ "${1:-}" = "--version" ]; then
  printf 'claude 2.1.327 (app-mode e2e stand-in)\n'
  exit 0
fi

if [ -n "${AGENT_SESSIONS_APPMODE_MARKER:-}" ]; then
  printf 'started\n' >"${AGENT_SESSIONS_APPMODE_MARKER}"
fi

printf 'BattleLab app-mode E2E stand-in agent\r\n'
printf '\r\n'
printf '> resumed the seeded session\r\n'
printf '\r\n'

# Hold the PTY open, like a real agent waiting for input. Teardown kills this process tree.
exec sleep 86400
