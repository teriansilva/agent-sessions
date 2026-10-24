# Captured Claude Code frames for Send now and agent-run turns (#1644)

Four frame logs from the real CLI, driven with the client's own argv
(`ClaudeCodec.argv(..., bypass=True, model="haiku")`: `--print --permission-prompts host
--replay-user-messages --output-format stream-json --verbose --input-format stream-json
--permission-prompt-tool stdio --permission-mode bypassPermissions --include-partial-messages`).

- **Captured** 2026-10-24 from `2.1.309 (Claude Code)`, model `claude-haiku-5-5`, in a fresh
  `mktemp -d` working directory with the `CLAUDE*` / `ANTHROPIC_*` / `AGENT_SESSIONS*` environment
  removed.
- **Format.** JSONL: `{"t": seconds since start, "dir": "in" | "out", "frame": …}`. `out` frames
  are what the client wrote: `user` messages, and `control_request` `initialize` / `interrupt`
  (`send_now: true, message_uuid: …`). `in` frames are the CLI's stdout, verbatim except as follows.
- **Sanitized.**
  - `stream_event`, `rate_limit_event` and `system/thinking_tokens` frames are omitted.
  - `system/init` keeps only `type, subtype, session_id, model, capabilities, claude_code_version,
    uuid, permissionMode`.
  - `control_response` bodies keep only `still_queued` / `send_now`. This drops the `initialize`
    reply, which carries the account and command lists.
  - `result` frames drop the usage / cost-breakdown objects; `assistant` messages keep
    `id, role, content, model`.
  - The working directory is replaced by `/tmp/fixture-cwd` and the user id in `/tmp/claude-<uid>/`
    by `uid`.

| File | Scenario | What it shows |
|---|---|---|
| `claude_send_now_agent_turn_bash.jsonl` | Two background Bash tasks; the first completes, a task notification starts an agent-run turn running a long foreground command; a message is written and `interrupt send_now` names it. | A notification-driven turn has no `command_lifecycle` and ends with a result whose `origin.kind` is `task-notification` and which names no input. It does **not** distrust the fence. `send_now` cuts the agent turn (`error_during_execution`, its foreground task `stopped`) and the second background task keeps running. |
| `claude_send_now_agent_turn_monitor.jsonl` | A Monitor started, stopped with TaskStop, started again; an appended line wakes an agent-run turn; Send now cuts it. | Same as above for a Monitor: the persistent monitor survives the cut. |
| `claude_notification_inside_operator_turn.jsonl` | A background task completes while an operator turn runs a long foreground command. | The notification is absorbed into the operator turn; its result still names the operator's input, in the usual order (`result`, then `completed`). |
| `claude_message_taken_into_agent_turn.jsonl` | A plain message (no Send now) written while an agent-run turn runs. | **The #1644 root cause.** The CLI queues the message, then continues the SAME turn with it (`started`, no new `system/init`), and sends `command_lifecycle completed` for it **before** the result. That result names the message (`user_message_uuid`) but carries the notification's `origin`. The client used to end the input as "without its result" and then distrust the fence twice. |
