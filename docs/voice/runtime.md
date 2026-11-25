# The call worker's runtime (#1397)

A call runs its speech pipeline in a separate worker process that needs **Pipecat**. BattleLab's
normal install doesn't include it, and calls show "the voice runtime is not installed" until one
of these is in place.

## A separate voice environment (survives upgrades)

Each release builds a fresh venv, so the runtime lives **outside** the release tree. The app
points the worker at it with `AGENT_SESSIONS_VOICE_PYTHON`, set in BattleLab's env file. For each
call, the worker gets a private shim that puts the *running* release's `agent_sessions` package
first on `PYTHONPATH`. The voice environment therefore needs only Pipecat and its server stack,
never the app itself, and it keeps working after every upgrade.

```bash
P=~/.local/share/agent-sessions            # or your AGENT_SESSIONS_HOME
python3 -m venv "$P/voice-runtime"
"$P/voice-runtime/bin/pip" install "pipecat-ai==1.13.0" "fastapi==0.161.1" \
    "uvicorn==0.54.0" "websockets==17.3" "httpx==0.28.1"
echo "AGENT_SESSIONS_VOICE_PYTHON=$P/voice-runtime/bin/python" >> "$P/env"
systemctl --user restart agent-sessions
```

Settings → HQ calls then reports calls as available, once the speech endpoints and voices are
set. P4 (#1625) replaces these steps with `agent-sessions voice setup`, which installs the same
pins hash-locked and keeps a rollback generation.

## Or: the app's own environment

`pip install 'agent-sessions[voice]'` into the app's venv also works (the `voice` extra is in
`pyproject.toml` and `uv.lock`). An installed release's venv is rebuilt on every upgrade without
the extra, so use this only for a development checkout.

## How it is checked

- `voice_calls.runtime_ready()` starts the configured interpreter with the shim and imports
  `pipecat` and `agent_sessions.voice_worker`. The result is cached for a minute and shown as
  the reason in `/api/voice/capabilities`.
- Tested in CI: an interpreter that doesn't have the app imports this release's package through
  the shim (`test_the_worker_imports_this_release_through_its_shim`).
- Exercised live on 2026-10-23: a Pipecat-only venv (no `agent-sessions` installed) passed
  `runtime_ready()` through the shim and ran the real calls described in the ADR.
