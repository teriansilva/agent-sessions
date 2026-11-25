#!/usr/bin/env bash
# Fake agent CLI for the visual-capture instance (#211). The capture serves agent-sessions
# with AGENT_SESSIONS_CLAUDE_BIN pointed here, so resuming the seeded session
# (`claude --resume <uuid>`) execs this instead of the real Claude Code. It prints a canned,
# deterministic transcript so the terminal pane renders representative output for the
# `session-view` screenshot, then holds the PTY open (sleep) so the screen stays static.
# NEVER wired into a real install — only run-local.sh sets the bin to this path.
set -euo pipefail

printf '\033[38;5;208m>\033[0m BattleLab session — seeded transcript (visual capture)\r\n'
printf '\r\n'
printf '\033[2m~/seed/alpha · claude\033[0m\r\n'
printf '\r\n'
printf '\033[38;5;250m> investigate the failing build and propose a fix\033[0m\r\n'
printf '\r\n'
printf 'Reading the CI logs and the last green commit…\r\n'
printf '  \033[38;5;42m✓\033[0m pulled pr-validate run for the head SHA\r\n'
printf '  \033[38;5;42m✓\033[0m diffed against origin/main — 3 files changed\r\n'
printf '\r\n'
printf 'The break is a pinned ruff version mismatch (0.8.4 in CI vs local).\r\n'
printf 'Proposed fix:\r\n'
printf '\r\n'
printf '  \033[38;5;208m- \033[0minstall ruff==0.8.4 in the dev venv\r\n'
printf '  \033[38;5;208m- \033[0mre-run `ruff format` and push\r\n'
printf '\r\n'
printf '\033[2mRan 2 tools · 14.2s\033[0m\r\n'
printf '\r\n'
printf '\033[38;5;208m>\033[0m \033[5m▍\033[0m\r\n'

# Hold the PTY so the rendered transcript stays on screen for the capture.
exec sleep 86400
