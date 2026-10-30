# Background work frames: Codex 0.164.0 and opencode 1.18.35 (#1724, Phase 1)

Recorded on 2026-10-27 from the **real** vendor binaries (`codex-cli 0.164.0`, `opencode 1.18.35`)
driven over their structured protocols (`codex app-server --listen stdio://`, `opencode acp
--hostname 127.0.0.1 --port 0 --mdns=false`). No operator login or model was used: each run had a
throwaway `HOME` (and `CODEX_HOME` / `XDG_*` under it) whose config pointed the CLI at a local mock
model on `127.0.0.1` (Codex: an OpenAI Responses-API SSE mock, `wire_api = "responses"`; opencode:
an `@ai-sdk/openai-compatible` chat-completions mock). The mock answered the first request with
one shell tool call and the next with the text `done`. The operator's own stores were never read
or written. Each line is `{"t": seconds, "dir": "out"|"in", "frame": …}`; local paths are rewritten
to `/tmp/fixture`, and Codex's `remoteControl/status/changed` host name and installation id are
blanked.

| Fixture | Tool call | What the CLI reported |
|---|---|---|
| `codex_background_terminal.jsonl` | `exec_command {"cmd": "sleep 20; echo bg-done", "yield_time_ms": 1000}` | `item/started` `commandExecution` with `processId` and `status: inProgress`; the turn then **completes with the item still in progress** (no `item/completed`). ~19 s later, after `turn/completed`, `item/commandExecution/outputDelta` and `item/completed` (`status: completed`, `exitCode: 0`) arrive **under the ended turn's `turnId`**. |
| `codex_detached_job.jsonl` | `exec_command {"cmd": "setsid sleep 40 … & echo started"}` | `item/completed` at once (`exitCode: 0`). Nothing further is ever reported, yet `sleep 40` kept running, reparented to the user manager (out of Codex's process tree, still in its cgroup). |
| `opencode_background_job.jsonl` | `bash {"command": "sleep 30 > /dev/null 2>&1 & echo started"}` | `tool_call_update` `status: completed` with `exit: 0`, then `end_turn`. Nothing reports the job, which kept running, reparented out of opencode's tree. |

Also observed, not in a fixture:

* A plain `nohup sleep 40 >/dev/null 2>&1 & echo started` under Codex did **not** survive its
  command: Codex ends the command's process group when the command finishes. Only a job that
  leaves the group (`setsid`, a daemonizing tool) outlives it.
* Codex 0.164.0 starts its own `git fetch` of the plugin marketplace
  (`$CODEX_HOME/.tmp/plugins-clone-*`) in the background shortly after `thread/start`; with a fresh
  `CODEX_HOME` it was still running minutes later. It is a process of the CLI's own, not the
  agent's, but a process floor that runs after readiness cannot tell the two apart.
* The stable v2 schema (`codex app-server generate-json-schema`) has no notification for a
  background terminal: `thread/status/changed` carries only `waitingOnApproval` /
  `waitingOnUserInput` flags, and `process/exited` is for client-spawned `process/spawn`. The
  `--experimental` schema adds a poll, `thread/backgroundTerminals/list` (opt-in experimental API),
  which BattleLab does not use.

**What BattleLab maps (native-runtime.md, "Staying alive while work runs"):**

* Codex: a `commandExecution` item with a `processId` that is still `inProgress` when its turn
  completes becomes an engine-reported background task (task id = item id, description = the
  command); its later `item/completed` (any turn id, same thread) ends it.
* opencode and Copilot report nothing: the process floor alone keeps the worker alive.
* A detached job (Codex `setsid`, opencode `&`) is visible only to the process floor.
