# Plan and tool frames: Codex 0.164.0, Claude Code 2.1.312, opencode 1.18.35 (#1726, Phase 2)

Recorded on 2026-10-29 from the **real** vendor binaries over their structured protocols, with the
method of #1724 (`native_background.PROVENANCE.md`): a throwaway `HOME` (and `CODEX_HOME` /
`XDG_*` under it) under `/tmp`, whose config points the CLI at a local mock model on `127.0.0.1`.
No operator login, no inference spend, and the operator's own stores were never read or written
(the roots sit under `/tmp` so no client walks up into the operator's home for project skills).

| Client | Mock | Driven by |
|---|---|---|
| Codex | OpenAI Responses-API SSE (`wire_api = "responses"`), `[tools.update_plan] enabled = true` | `codex app-server --listen stdio://`, BattleLab's handshake |
| Claude | Anthropic Messages-API SSE (`ANTHROPIC_BASE_URL`, a fake key) | `ClaudeCodec.argv(...)` — the client's own argv, `--mcp-config` with two servers |
| opencode | `@ai-sdk/openai-compatible` chat-completions SSE | `opencode acp --hostname 127.0.0.1 --port 0 --mdns=false` with `api_config_content(..., bypass=True)` |

Each run configured one working stdio MCP server (`fixture`, `plan_environment_recorders/
mcp_fixture.py`) and one that cannot start (`broken`; opencode: a disabled remote `remote-off`),
one skill (`release-notes`) and one command (`triage`). The recorders and the sanitizer are in
`plan_environment_recorders/`; rerun them to refresh a fixture. Each line is
`{"t": seconds, "dir": "out"|"in", "frame": …}`; local paths read `/tmp/fixture`; rate-limit,
token-usage, `stream_event` and thinking-token frames are dropped; Codex's remote-control host
name and installation id are blanked.

| Fixture | Scenario | What the CLI reported |
|---|---|---|
| `codex_plan_environment.jsonl` | `thread/start`, `mcpServerStatus/list` (one server per page), `skills/list`, a turn calling `update_plan`, a turn calling the MCP tool | `mcpServer/startupStatus/updated` per server (`starting` → `ready` / `failed`, `threadId` set) right after `thread/start`; the paged list repeats them as `runtimeStatus` (`failed` / `connected`) **with the whole tool inventory per server** (why BattleLab asks one server per page); `skills/list` returns the user skill plus Codex's bundled system skills, each with its `SKILL.md` path; `turn/plan/updated` `{explanation, plan: [{step, status: pending\|inProgress\|completed}]}` — **no item** for `update_plan`; `mcpToolCall` items name their `server`. |
| `codex_plan_item.jsonl` | `initialize` with `capabilities.experimentalApi`, a turn in `collaborationMode: plan` whose reply holds a `<proposed_plan>` block | A `plan` item (`item/started` empty, `item/plan/delta`, `item/completed` with the document as `text`). BattleLab never opts into the experimental API, so its sessions do not receive this; the codec reads the settled item if one comes. |
| `claude_plan_environment.jsonl` | `TaskCreate` ×2, `TaskUpdate` ×2, `TaskList`; `EnterPlanMode` then `ExitPlanMode`; `Skill`; the MCP tool | `system/init` opens **every** turn with `mcp_servers: [{name, status: connected\|failed, source}]`, `skills: [names]`, `slash_commands: [names]` (bundled skills included). The Task tools' `user` frames carry a structured `tool_use_result` (`{task: {id, subject}}`, `{success, taskId, statusChange}`, `{tasks: [{id, subject, status}]}`); ids are `"1"`, `"2"`, …. `ExitPlanMode`'s `tool_use` input holds `plan`, its permission request's input is `{}` and its result names a plan file path (never read). `Skill`'s input is `{skill}`. The MCP call is `mcp__fixture__labels`, and its permission request names `mcp_server`. |
| `claude_todowrite_disabled.jsonl` | A `TodoWrite` call | 2.1.312 refuses it: "TodoWrite is disabled for this session" — the Task tools replaced it. The codec still reads a `TodoWrite` input as the checklist (older CLIs, or where it is enabled). |
| `opencode_plan_environment.jsonl` | `todowrite`, `skill`, the MCP tool (`fixture_labels`) | **No `plan` session update**: 1.18.35 emits only `agent_message_chunk`, `agent_thought_chunk`, `available_commands_update`, `config_option_update`, `tool_call`, `tool_call_update` and `usage_update` (checked in the binary too). The checklist is the `todowrite` call's `rawInput.todos` (`{content, status, priority, id}`), on the `in_progress` update; the completed one retitles it ("2 todos"). `available_commands_update` arrives right after the mode pin, **outside any turn**, listing commands and skills alike (no separate skill list). A `skill` call's `rawInput` is `{name}`. MCP tools are titled `<server>_<tool>`; ACP reports no MCP server or status. |

Also observed, not in a fixture:

* `opencode debug config` (the worker's spawn-time probe) lists `mcp.<name>` with `type`,
  `command`/`url` and `enabled`. BattleLab takes the **names** only (status `configured`, or
  `disabled` when `enabled: false`) and nothing else from an entry.
* Copilot could not be recorded: it needs a real GitHub login. Its shapes are taken from its own
  ACP adapter (`@github/copilot/app.js`): `plan` updates `{entries: [{content, priority, status:
  pending|in_progress|completed}]}` and `available_commands_update` `{availableCommands: [{name,
  description, input?}]}` — the same paths as opencode's codec.

**What BattleLab maps (structured-clients.md, "The plan and the tools"):** `plan` and
`environment` events, and the `call` / `mcp_server` / `skill` facts on tool events. Pinned by
`tests/test_native_plan_environment.py`.
