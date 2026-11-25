# Captured Claude Code frames for a turn that fails on the agent's login

`claude_auth_failed.jsonl`: one operator message to a CLI with no credentials at all.

- **Captured** 2026-11-03 from `2.1.317 (Claude Code)` with `--print --replay-user-messages
  --output-format stream-json --verbose --input-format stream-json --permission-mode default
  --session-id=<uuid>`, in a fresh `mktemp -d` directory used as both the working directory and
  `HOME`, under `env -i PATH=…` (no `CLAUDE*` / `ANTHROPIC_*` / `AGENT_SESSIONS*` variables).
- **Format.** JSONL: `{"dir": "in" | "out", "frame": …}`. The one `out` frame is the user message
  written to stdin; `in` frames are the CLI's stdout.
- **Sanitized.** `system/init` keeps only `type, subtype, session_id, model, capabilities,
  claude_code_version, uuid, permissionMode`; the `assistant` message keeps `id, role, content,
  model`; the `result` drops `usage, modelUsage, subagent_stats, model_usage_since_last_result,
  permission_denials`. Every other field is verbatim.

What it shows: the CLI answers with a synthetic assistant message (`model: "<synthetic>"`, text
"Not logged in · Please run /login") whose frame carries **`"error": "authentication_failed"`**
and `"is_api_error_message": true`, then a `result` with `is_error: true`, `subtype: "success"` and
`terminal_reason: "api_error"`, then `command_lifecycle cancelled`. The on-disk transcript of a
real expired-OAuth turn carries the same `error` value under different words ("Failed to
authenticate: OAuth session expired …"), which is why `ClaudeCodec` keys the `signin` remedy off
the field and never the text.
