# `task` subagent frames: opencode 1.18.35 (#1891)

Recorded on 2026-11-08 by `opencode_subagent_probe.py` from the **real** `opencode 1.18.35` driven
over ACP (`opencode acp --hostname 127.0.0.1 --port 0 --mdns=false`). No operator login, model or
store was used: a throwaway `HOME` (and `XDG_*` under it) whose config pointed the CLI at a
loopback `@ai-sdk/openai-compatible` chat-completions mock. In the parent session the mock calls
`task` (subagent `general`) and then answers `done`. In the child session (first user message
`CHILD: …`) it calls `bash` (in the operator-block runs, the fixture MCP server's `labels`) and answers `child-done`. Each line is
`{"t": seconds, "dir": "out"|"in", "frame": …}`. The throwaway path is rewritten to
`/tmp/fixture`, and session ids to `ses_fixture<last 6>`. Every `session/request_permission`
would have been answered `cancelled`, as BattleLab's worker answers one it can't place. None
arrived in any run.

| Fixture | Config (`engines.opencode.api_config_content`) | What the CLI reported |
|---|---|---|
| `opencode_subagent_ask.jsonl` | bypass agent, but the config-level block `{"*": "ask"}` (what `main` shipped before #1891) | The parent's `task` call: `tool_call` (`title: "task"`, `rawInput: {}`), then `in_progress` twice with `rawInput` `{description, prompt, subagent_type}`. Then **nothing for 30 s**: the child's `bash` asks, and **no `session/request_permission` is ever sent**. The prompt never returns; only a `session/cancel` ends it. |
| `opencode_subagent_bypass.jsonl` | bypass as shipped by #1891 (config-level all-allow) | The same start, then `completed` with `rawOutput.metadata.sessionId` (the child) and the child's answer (`child-done`) in `content`; `end_turn`. Nothing from the child session in between. |
| `opencode_subagent_guarded.jsonl` | guarded as shipped by #1891 (`task: deny`) | `task` is not offered to the model at all. A call the mock makes anyway starts as `tool_call` `title: "task"`, is retitled `invalid` (`kind: other`, `rawInput: {tool: "task", error: …}`) and completes at once; `end_turn`. No subagent, no hang. |
| `opencode_subagent_operator_unrestated.jsonl` | bypass, with the operator's global config declaring `permission: {"*": "ask", "fixture_*": "ask"}` and a fixture MCP server; the child calls `fixture_labels` | opencode merges our block over the operator's key by key and keeps **their** order, so `fixture_*: ask` follows our `*`. A key we name (`bash`) is appended after theirs and is no hole. The child's MCP call asks, nothing is sent, and the prompt never returns. |
| `opencode_subagent_operator_restated.jsonl` | the same, with the worker's probed block passed in (`operator_permission`): every operator key restated as allow | The child's MCP call runs; `completed`, `end_turn`. |

What it settles, and what re-opens it:

* A child session's permission request does not reach an ACP client on 1.18.35, so a subagent
  that asks can never be answered. Guarded sessions deny `task`, and bypass gives subagents the
  config-level all-allow. **Re-run this probe on every opencode bump.** If the `ask` run starts
  receiving a request, the guarded policy can route a subagent's ask to the operator instead of
  denying `task`, and the probe's assertion fails to say so.
* ACP carries a subagent's start, description, type and end, never its progress (Phase 2 of
  #1891).
