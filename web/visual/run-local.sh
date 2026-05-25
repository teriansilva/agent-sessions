#!/usr/bin/env bash
# Local visual pipeline (#96, Phase 2): seed a throwaway HOME → build the SPA → run an
# isolated `agent-sessions serve` against it → capture screenshots → tear everything down.
# Proves login + every initial area renders across all formats WITHOUT touching real data.
#
#   web/visual/run-local.sh           # captures all areas → $VISUAL_OUT (default /tmp/visual-out)
#   VISUAL_AREAS=app-home web/visual/run-local.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PORT="${PORT:-8791}"
PASS="${VISUAL_PASS:-visual-Kings-9time}"
VENV="${VENV:-$REPO/venv}"
OUT="${VISUAL_OUT:-/tmp/visual-out}"
HOME_TMP="$(mktemp -d /tmp/visual-home.XXXXXX)"
SRV=""
cleanup() { [ -n "$SRV" ] && kill "$SRV" 2>/dev/null || true; rm -rf "$HOME_TMP"; }
trap cleanup EXIT INT TERM

echo "[run-local] seeding throwaway HOME $HOME_TMP"
python3 "$REPO/web/visual/seed.py" "$HOME_TMP"

echo "[run-local] building web/dist"
( cd "$REPO/web" && npm run build --silent )

py="$VENV/bin/python"
hash="$("$py" -c "from agent_sessions.auth import hash_password; print(hash_password('$PASS'))")"
secret="$("$py" -c "import secrets; print(secrets.token_urlsafe(48))")"

echo "[run-local] starting isolated serve on 127.0.0.1:$PORT (HOME=$HOME_TMP)"
HOME="$HOME_TMP" \
AGENT_SESSIONS_USERNAME=admin \
AGENT_SESSIONS_PASSWORD_HASH="$hash" \
AGENT_SESSIONS_SECRET_KEY="$secret" \
AGENT_SESSIONS_ORIGIN="http://127.0.0.1:$PORT" \
AGENT_SESSIONS_UI=react \
AGENT_SESSIONS_TERMINAL=ws \
AGENT_SESSIONS_WEB_DIST="$REPO/web/dist" \
AGENT_SESSIONS_RUNTIME_DIR="$HOME_TMP/pty" \
  "$VENV/bin/agent-sessions" serve --host 127.0.0.1 --port "$PORT" > "$HOME_TMP/serve.log" 2>&1 &
SRV=$!

for i in $(seq 1 30); do
  curl -fsS -m2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
  [ "$i" = 30 ] && { echo "serve never came up"; cat "$HOME_TMP/serve.log"; exit 1; }
done

echo "[run-local] capturing → $OUT"
( cd "$REPO/web" \
  && E2E_BASE_URL="http://127.0.0.1:$PORT" VISUAL_USER=admin VISUAL_PASS="$PASS" \
     VISUAL_OUT="$OUT" VISUAL_AREAS="${VISUAL_AREAS:-all}" npm run visual )
echo "[run-local] done → $OUT/manifest.json"
